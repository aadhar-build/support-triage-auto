"""A local retirement screen must leave the running factory available."""

import json
import os
import runpy
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
import worktree_retirement as retirement


class RetirementPreview(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "tracked").write_text("preview fixture\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "fixture")
        self.branch = f"factory/{JOB_ID}"
        self.worktree = self.root / ".worktrees" / JOB_ID
        self.git("worktree", "add", "-qb", self.branch, str(self.worktree))
        self.sha = self.git("rev-parse", self.branch).stdout.strip()
        self.write_job(stage="merged")

    def assert_screened(self, result):
        self.assertEqual(result["status"], "screened locally")
        self.assertEqual(result["assessment"], "eligibility not evaluated")
        self.assertEqual(result["job_id"], JOB_ID)
        self.assertEqual(result["pr"], 2)
        self.assertEqual(result["candidate_sha"], self.sha)
        self.assertNotIn("confirm_pr", result)

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")}

    def test_screen_is_read_only_and_skips_locks_network_and_deletion_checks(self):
        before, refs = self.snapshot(), self.git("show-ref").stdout
        run_command = subprocess.run

        def local_git(command, **options):
            self.assertEqual(command[0], "git")
            self.assertIn("rev-parse", command)
            self.assertIn("--no-optional-locks", command)
            self.assertIn("protocol.allow=never", command)
            self.assertEqual(options["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertGreater(options["timeout"], 0)
            self.assertLessEqual(options["timeout"], 5)
            return run_command(command, **options)

        with ExitStack() as guards:
            guards.enter_context(mock.patch.object(subprocess, "run", side_effect=local_git))
            for module, names in (
                (retirement.fcntl, ("flock",)),
                (os, ("walk", "fwalk", "scandir")),
                (retirement, ("_inventory", "_worker_markers", "_idle_processes", "_write")),
                (retirement.retirement_git, ("validate", "verify_merge", "validate_parked")),
                (retirement.retired_evidence, ("capture",)),
                (retirement.F, ("atomic_write", "gh_api")),
            ):
                for name in names:
                    guards.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
            self.assert_screened(retirement.run(self.root, JOB_ID))
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertEqual(self.snapshot(), before)

    def test_active_and_oversized_records_never_offer_confirmation(self):
        for stage, title, status in (("build", "active", "blocked"), ("merged", "x" * 65536, "inconclusive")):
            with self.subTest(status=status):
                self.write_job(stage=stage, title=title)
                command = self.cli("retire-worktree", JOB_ID, success=False)
                self.assertEqual(command.returncode, 1)
                result = json.loads(command.stdout)
                self.assertEqual(result["status"], status)
                self.assertNotIn("confirm_pr", result)

    def test_changing_job_during_head_lookup_is_inconclusive(self):
        lookup = retirement._preview_head

        def change_job(root, branch):
            head = lookup(root, branch)
            self.write_job(stage="merged", pr=3)
            return head

        with mock.patch.object(retirement, "_preview_head", side_effect=change_job):
            result = retirement.run(self.root, JOB_ID)
        self.assertEqual(result["status"], "inconclusive")
        self.assertNotIn("confirm_pr", result)

    def test_worker_admission_and_new_job_creation_complete_while_preview_is_paused(self):
        worker = self.factory / "jobs/095-active-worker"
        worker.mkdir()
        worker_state = retirement.F.new_job(worker.name, 95, "Active worker")
        worker_state["stage"] = "build"
        retirement.F.write_job(worker, worker_state)
        worker_before = (worker / "job.json").read_bytes()
        candidate_before, refs = self.state.read_bytes(), self.git("show-ref").stdout
        with mock.patch.object(sys, "path", list(sys.path)):
            cmd_create = runpy.run_path(str(self.factory / "bin/job"))["cmd_create"]
        issue = {"state": "open", "labels": [{"name": "factory"}], "title": "New job", "user": {"login": "fixture"}}
        get_responses = {"issues/96": issue, "issues/96/comments?per_page=100": []}

        def fake_get(root, method, path):
            self.assertEqual(method, "GET")
            return get_responses[path]

        entered, release = threading.Event(), threading.Event()
        lookup = retirement._preview_head

        def pause(root, branch):
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("worker admission or job creation did not complete during preview")
            return lookup(root, branch)

        with mock.patch.object(retirement, "_preview_head", side_effect=pause), ThreadPoolExecutor(1) as pool:
            preview = pool.submit(retirement.run, self.root, JOB_ID)
            try:
                self.assertTrue(entered.wait(timeout=5), "preview did not reach branch lookup")
                # Use another process so its directory flock competes exactly as a worker's would.
                admission = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import sys\nfrom pathlib import Path\nsys.path.insert(0, 'factory/lib')\n"
                     "import factory as F\nd = Path(sys.argv[1])\n"
                     "with F.job_mutex(d):\n    F.require_admission(d)\n", str(worker)],
                    cwd=self.root, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(admission.returncode, 0, admission.stderr)
                with mock.patch.object(retirement.F, "gh_api", side_effect=fake_get), \
                     mock.patch.object(retirement.F, "gh_user", return_value="fixture"):
                    created = cmd_create(self.root, retirement.F.load_config(self.root), ["96"])
                self.assertEqual(created, "created 096-new-job")
                self.assertEqual(json.loads((worker.parent / "096-new-job/job.json").read_bytes())["issue"], 96)
                self.assertFalse(preview.done())
            finally:
                release.set()
            self.assert_screened(preview.result(timeout=5))
        self.assertEqual(self.state.read_bytes(), candidate_before)
        self.assertEqual((worker / "job.json").read_bytes(), worker_before)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertFalse((self.root / ".git/factory-retirements").exists())
