"""The public retirement explanation observes one job and never approves removal."""
import json
import re
import socket
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


class RetirementExplanation(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text("private-customer-cache.bin\n")
        (self.root / "private-source.py").write_text("name = 'sensitive client value'\n")
        self.git("add", ".gitignore", "private-source.py")
        self.git("commit", "-qm", "base")
        self.wt = self.root / ".worktrees" / JOB_ID
        self.branch = f"factory/{JOB_ID}"
        self.git("worktree", "add", "-qb", self.branch, str(self.wt))
        self.sha = self.git("rev-parse", self.branch).stdout.strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.write_job(stage="merged", title="private project title")
        (self.wt / "private-customer-cache.bin").write_bytes(b"sensitive client value")
        (self.wt / "private-working-notes.txt").write_text("sensitive client value")
        (self.job_dir / "round-1").mkdir()
        (self.job_dir / "round-1/checks.md").write_text(
            f"GATE: PASS\nCANDIDATE: {self.sha}\nBASE: {self.sha}\nSPEC-DIGEST: {'a' * 64}\n\nsensitive client value\n")
        (self.job_dir / "final-checks.md").write_text("private final evidence line\n")

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")}

    def assert_private(self, result):
        encoded = json.dumps(result)
        for secret in (str(self.root), "private-source.py", "private-customer-cache.bin",
                       "private-working-notes.txt", "sensitive client value", "private final evidence line",
                       "private project title", "round-1/checks.md", "final-checks.md"):
            self.assertNotIn(secret, encoded)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", encoded))
        self.assertNotIn(f"2@{self.sha}", encoded)
        for key in ("confirm_pr", "discard_generated", "manifest", "digest"):
            self.assertNotIn(f'"{key}"', encoded)

    def assert_advisory(self, result):
        self.assertEqual(result["status"], "advisory")
        self.assertEqual(result["assessment"], "eligibility not evaluated")
        checkout = result["checkout"]
        self.assertEqual(checkout["coverage"], "complete")
        self.assertEqual(checkout["observed"]["unclassified_roots_or_entries"], 2)
        evidence = result["evidence"]
        self.assertEqual(evidence["native_headers"], 1)
        self.assertEqual(evidence["candidate_matched_headers"], 1)
        self.assertEqual(evidence["candidate_mismatched_headers"], 0)
        self.assertEqual(evidence["slots"]["final_gate"], "non_native")
        self.assertEqual(evidence["confidence"], "header_observation_only")
        self.assertIsNone(evidence["confirmed_retention_receipts"])
        self.assert_private(result)

    def test_default_preview_discovers_explanation_and_cli_matches_read_only_api(self):
        before, refs = self.snapshot(), self.git("show-ref").stdout
        preview = json.loads(self.cli("retire-worktree", JOB_ID).stdout)
        self.assertEqual(preview["status"], "screened locally")
        self.assertEqual(preview["detail_option"], "--explain")
        self.assertNotIn("checkout", preview)
        self.assertNotIn("evidence", preview)

        start_process = subprocess.Popen

        def local_git(command, *args, **kwargs):
            self.assertEqual(command[0], "git")
            self.assertTrue({"rev-parse", "ls-tree", "ls-files", "cat-file", "check-ignore"}.intersection(command))
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            return start_process(command, *args, **kwargs)

        with ExitStack() as guards:
            guards.enter_context(mock.patch.object(subprocess, "Popen", side_effect=local_git))
            for module, names in (
                (socket.socket, ("connect", "connect_ex")),
                (retirement.fcntl, ("flock",)),
                (retirement, ("_inventory", "_worker_markers", "_idle_processes", "_write")),
                (retirement.retirement_git, ("verify_merge", "validate", "validate_parked", "inspect_generated")),
                (retirement.retired_evidence, ("capture",)),
                (retirement.F, ("atomic_write", "gh_api")),
            ):
                for name in names:
                    guards.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
            actual = retirement.run(self.root, JOB_ID, explain=True)
        self.assert_advisory(actual)
        command = self.cli("retire-worktree", JOB_ID, "--explain")
        self.assertEqual(command.stderr, "")
        self.assertEqual(json.loads(command.stdout), actual)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertEqual(self.snapshot(), before)

    def test_worker_admitted_while_checkout_explanation_is_paused(self):
        import retirement_checkout_advisory as checkout
        worker = self.factory / "jobs/095-active-worker"
        worker.mkdir()
        active = retirement.F.new_job(worker.name, 95, "Active worker")
        active["stage"] = "build"
        retirement.F.write_job(worker, active)
        entered, release = threading.Event(), threading.Event()
        observe = checkout.observe_checkout

        def pause(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("worker could not complete during explanation")
            return observe(*args, **kwargs)

        with mock.patch.object(checkout, "observe_checkout", side_effect=pause), ThreadPoolExecutor(1) as pool:
            explaining = pool.submit(retirement.run, self.root, JOB_ID, explain=True)
            try:
                self.assertTrue(entered.wait(timeout=5), "checkout observation was never reached")
                worker_process = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import sys\nfrom pathlib import Path\nsys.path.insert(0, 'factory/lib')\n"
                     "import factory as F\nd = Path(sys.argv[1])\n"
                     "with F.job_mutex(d):\n    F.require_admission(d)\n", str(worker)],
                    cwd=self.root, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(worker_process.returncode, 0, worker_process.stderr)
                self.assertFalse(explaining.done())
            finally:
                release.set()
            self.assert_advisory(explaining.result(timeout=5))

    def test_job_changed_during_checkout_observation_is_inconclusive(self):
        import retirement_checkout_advisory as checkout
        observe = checkout.observe_checkout

        def replace_job(*args, **kwargs):
            summary = observe(*args, **kwargs)
            self.write_job(stage="merged", pr=3)
            return summary

        with mock.patch.object(checkout, "observe_checkout", side_effect=replace_job):
            result = retirement.run(self.root, JOB_ID, explain=True)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["assessment"], "eligibility not evaluated")
        self.assert_private(result)

    def test_gate_replaced_during_checkout_scan_withholds_evidence_counts(self):
        import retirement_checkout_advisory as checkout
        gate = self.job_dir / "final-checks.md"
        native = (self.job_dir / "round-1/checks.md").read_bytes()
        original_job = self.state.read_bytes()
        scan = checkout._scan_checkout
        for initial in (gate.read_bytes(), native):
            with self.subTest(initially_native=initial == native):
                gate.write_bytes(initial)

                def publish_during_scan(*args, **kwargs):
                    staged = gate.with_name("private-pending-gate.md")
                    staged.write_bytes(native)
                    staged.replace(gate)
                    return scan(*args, **kwargs)

                with mock.patch.object(checkout, "_scan_checkout", side_effect=publish_during_scan) as observed:
                    result = retirement.run(self.root, JOB_ID, explain=True)
                observed.assert_called_once()
                self.assertEqual(self.state.read_bytes(), original_job)
                self.assertEqual(result["status"], "inconclusive")
                self.assertEqual(result["coverage"], "unresolved")
                self.assertNotIn("evidence", result)
                self.assertNotIn("native_headers", json.dumps(result))
                self.assert_private(result)

    def test_rejects_explanation_with_mutation_inventory_or_approval_flags(self):
        before = self.snapshot()
        for option, value in (("apply", True), ("recover", True), ("inventory_generated", True),
                              ("confirm_pr", f"2@{self.sha}"), ("discard_generated", "e" * 64)):
            flag = "--" + option.replace("_", "-")
            flags = [flag] if value is True else [flag, value]
            with self.subTest(flags=flags):
                with self.assertRaises(retirement.F.FactoryError):
                    retirement.run(self.root, JOB_ID, explain=True, **{option: value})
                result = self.cli("retire-worktree", JOB_ID, "--explain", *flags, success=False)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)
