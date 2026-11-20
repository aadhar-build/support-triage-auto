"""Exercise the retirement transaction with real disposable linked worktrees."""
import fcntl
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
import worktree_retirement as R


class RetirementTransaction(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.job_dir = self.root / "factory/jobs" / JOB_ID
        self.state = self.job_dir / "job.json"
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("switch", "-C", "main")
        (self.root / "tracked").write_text("base\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "base")
        self.wt = self.root / ".worktrees" / JOB_ID
        self.branch = f"factory/{JOB_ID}"
        self.git("worktree", "add", "-qb", self.branch, str(self.wt))
        (self.wt / "tracked").write_text("candidate\n")
        self.git("-C", str(self.wt), "commit", "-qam", "candidate")
        self.sha = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.git("merge", "-q", "--no-ff", "-m", "integrate", self.branch)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("update-ref", f"refs/remotes/origin/{self.branch}", self.sha)
        self.write_job(stage="merged")
        self.record = self.root / ".git/factory-retirements" / (JOB_ID + ".json")
        # Fresh authenticated GitHub response validation has separate offline
        # fixture tests. The transaction still uses real Git and evidence checks.
        self.merge = mock.patch.object(R.retirement_git, "verify_merge", side_effect=self.proof).start()
        self.idle = mock.patch.object(R, "_idle_processes").start()
        self.addCleanup(mock.patch.stopall)

    def proof(self, root, job, cfg, local):
        return {"repository": local["repository"], "repository_id": 17, "pr": 2, "pr_id": 23,
                "base_ref": "main", "candidate_sha": self.sha, "base_sha": local["base_sha"],
                "merge_commit_sha": local["base_sha"], "job_pr_association": "verified", "candidate_merge_identity": "exact"}

    def retire(self, **kw):
        return R.run(self.root, JOB_ID, **kw)

    def test_preview_then_apply_then_retry_preserves_evidence_and_all_refs(self):
        before = self.state.read_bytes()
        refs = self.git("show-ref").stdout
        self.assertEqual(self.retire()["status"], "screened locally")
        self.assertFalse(self.record.parent.exists())
        self.assertTrue(self.wt.is_dir())
        self.assertEqual(self.retire(apply=True)["status"], "retired")
        self.assertEqual(self.retire(apply=True)["status"], "already retired")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertFalse(os.path.lexists(self.wt))
        self.assertEqual(R.read_retirement(self.root, JOB_ID)["status"], "retired")

    def test_empty_generated_inventory_supplies_no_discard_approval(self):
        result = self.retire(inventory_generated=True)
        self.assertEqual(result, {"job_id": JOB_ID, "status": "no generated files", "file_count": 0,
                                  "assessment": "eligibility not evaluated"})
        with self.assertRaises(R.F.FactoryError):
            self.retire(apply=True, discard_generated="0" * 64)
        self.assertTrue(self.wt.exists())
        self.assertFalse(self.record.exists())
        self.merge.assert_not_called()

    def test_legacy_requires_exact_second_confirmation(self):
        self.merge.side_effect = lambda *args: {**self.proof(*args), "job_pr_association": "local_only"}
        self.assertNotIn("confirm_pr", self.retire())
        for token in (None, "3@" + self.sha, "2@" + self.sha[:8]):
            with self.assertRaisesRegex(R.F.FactoryError, "historical"):
                self.retire(apply=True, confirm_pr=token)
        self.assertFalse(self.record.parent.exists())
        self.assertEqual(self.retire(apply=True, confirm_pr="2@" + self.sha)["status"], "retired")

    def test_rewritten_merge_requires_confirmation_even_with_verified_association(self):
        self.merge.side_effect = lambda *args: {**self.proof(*args), "candidate_merge_identity": "content_equivalent"}
        preview = self.retire()
        self.assertEqual(preview["status"], "screened locally")
        self.assertNotIn("confirm_pr", preview)
        self.merge.assert_not_called()
        with self.assertRaisesRegex(R.F.FactoryError, "equivalent changes"):
            self.retire(apply=True)
        self.assertEqual(self.retire(apply=True, confirm_pr="2@" + self.sha)["status"], "retired")

    def test_command_line_refuses_active_job_with_foreign_git_environment(self):
        self.write_job(stage="build")
        for args in ((JOB_ID,), (JOB_ID, "--apply")):
            result = self.cli("retire-worktree", *args, success=False,
                              env={**os.environ, "GIT_DIR": "/absent/foreign-repo.git"})
            self.assertEqual(result.returncode, 1)
            reply = json.loads(result.stdout)
            self.assertEqual((reply["status"], reply["job_id"], reply["reason"]), ("blocked", JOB_ID, "job stage is build"))
        self.assertTrue(self.wt.is_dir())
        self.assertFalse(self.record.parent.exists())

    def test_command_line_previews_have_a_deadline_during_repository_discovery(self):
        import subprocess
        import time
        tools = self.root / "slow-git"
        tools.mkdir()
        fake = tools / "git"
        fake.write_text("#!/bin/sh\nexec sleep 60\n")
        fake.chmod(0o700)
        before = time.monotonic()
        result = subprocess.run([self.factory / "bin/retire-worktree", JOB_ID], cwd=self.root, capture_output=True,
                                env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}, timeout=8)
        self.assertLess(time.monotonic() - before, 7)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "inconclusive")
        self.assertFalse(self.record.parent.exists())

    def test_dependency_and_in_progress_jobs_are_retained(self):
        self.write_job(stage="build")
        with self.assertRaisesRegex(R.F.FactoryError, "stage is build"):
            self.retire(apply=True)
        self.write_job(stage="merged")
        other = self.job_dir.parent / "002-stacked"
        other.mkdir()
        (other / "job.json").write_text(json.dumps({"id": other.name, "stage": "review", "stacked_on": JOB_ID}))
        with self.assertRaisesRegex(R.F.FactoryError, "stacked"):
            self.retire(apply=True)
        self.assertTrue(self.wt.is_dir())
        self.merge.assert_not_called()

    def test_worker_marker_and_contending_checkout_lock_block_apply(self):
        review = self.job_dir / "round-1"
        review.mkdir()
        lock = review / "worker-reviewer-code.json.replacing"
        lock.touch()
        with self.assertRaisesRegex(R.F.FactoryError, "interrupted"):
            self.retire(apply=True)
        lock.unlink()
        fd = os.open(self.wt, os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(R.F.FactoryBusy):
                self.retire(apply=True)
        finally:
            os.close(fd)
        self.merge.assert_not_called()

    def test_job_change_while_awaiting_merge_lookup_is_retained(self):
        def changed(*args):
            self.write_job(stage="merged", revision=6)
            return self.proof(*args)
        self.merge.side_effect = changed
        with self.assertRaisesRegex(R.F.FactoryError, "state changed"):
            self.retire(apply=True)
        self.assertTrue(self.wt.is_dir())
        self.assertFalse(self.record.exists())

    def test_recovery_before_during_and_after_git_removal(self):
        real = R._git
        for verb in ("move", "remove"):
            def fail(root, *args):
                if args[:2] == ("worktree", verb):
                    raise RuntimeError("injected interruption")
                return real(root, *args)
            with mock.patch.object(R, "_git", side_effect=fail), self.assertRaisesRegex(RuntimeError, "interruption"):
                self.retire(apply=True)
            self.assertEqual(R.read_retirement(self.root, JOB_ID)["status"], "prepared")
            with self.assertRaisesRegex(R.F.FactoryError, "explicit --recover"):
                self.retire(apply=True)
            self.assertEqual(self.retire(recover=True)["status"], "cancelled")
            self.assertTrue(self.wt.is_dir())
            self.assertFalse(self.record.exists())
        with mock.patch.object(R, "_complete", side_effect=RuntimeError("interruption")), self.assertRaises(RuntimeError):
            self.retire(apply=True)
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.retire(recover=True)["status"], "retired")

    def test_ignored_file_created_before_move_is_preserved_in_parking(self):
        real = R._git
        def inject(root, *args):
            if args[:2] == ("worktree", "move"):
                (self.wt / "private.data").write_text("must survive")
            return real(root, *args)
        (self.root / ".git/info/exclude").write_text("private.data\n")
        with mock.patch.object(R, "_git", side_effect=inject), self.assertRaises(R.F.FactoryError):
            self.retire(apply=True)
        record = R.read_retirement(self.root, JOB_ID)
        self.assertEqual(record["status"], "prepared")
        self.assertEqual((self.record.parent / record["parked"] / "private.data").read_text(), "must survive")

    def test_late_ignored_file_written_while_scanning_processes_is_preserved(self):
        def late_file(root, job, local, *, parked=None):
            if parked:
                (parked / "late-cache").write_text("late writer")
        self.idle.side_effect = late_file
        (self.root / ".git/info/exclude").write_text("late-cache\n")
        with self.assertRaises(R.F.FactoryError):
            self.retire(apply=True)
        record = R.read_retirement(self.root, JOB_ID)
        self.assertEqual(record["status"], "prepared")
        self.assertEqual((self.record.parent / record["parked"] / "late-cache").read_text(), "late writer")

    def test_spliced_retirement_fields_are_rejected(self):
        self.retire(apply=True)
        original = self.record.read_bytes()
        for key, field, wrong in (("merge", "pr", 99), ("evidence", "candidate_sha", "7" * 40),
                                  ("local", "admin_identity", [-1, -1])):
            with self.subTest(field=field):
                record = json.loads(original)
                record[key][field] = wrong
                self.record.write_text(json.dumps(record))
                with self.assertRaises(R.F.FactoryError):
                    self.retire(recover=True)

    def test_malformed_journal_cannot_authorize_completion(self):
        self.assertEqual(self.retire(apply=True)["status"], "retired")
        raw = self.record.read_text()
        self.record.write_text(raw.replace('"schema_version": 1', '"schema_version": 9, "schema_version": 1', 1))
        with self.assertRaisesRegex(R.F.FactoryError, "malformed"):
            self.retire(recover=True)


class RetirementProcessVisibility(unittest.TestCase):
    def test_linux_activity_via_cwd_fd_mapping_or_arguments(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / ".git").mkdir()
            (root / "factory/jobs").mkdir(parents=True)
            proc = root / "proc"
            ours = proc / str(os.getpid())
            ours.mkdir(parents=True)
            (ours / "status").write_text("PPid:\t0\nCapEff:\t00200000\n")
            (ours / "mountinfo").write_bytes(b"2 0 8:1 / / rw - ext4 root rw\n1 0 0:1 / /proc rw,relatime - proc proc rw\n")
            (proc / "self").symlink_to(ours)
            init = proc / "1"
            for location in (ours, init):
                (location / "ns").mkdir(parents=True)
                (location / "ns/pid").symlink_to("pid:[4026531836]")
                (location / "ns/mnt").symlink_to("mnt:[123]")
                (location / "ns/user").symlink_to("user:[4026531837]")
            kernel = init / "task/1"
            kernel.mkdir(parents=True)
            (kernel / "cmdline").touch()
            (kernel / "status").write_bytes(b"Kthread:\t1\n")
            process = proc / str(os.getpid() + 10000)
            other = process / "task" / str(os.getpid() + 10001)
            other.mkdir(parents=True)
            (other / "fd").mkdir()
            (other / "cmdline").write_bytes(b"editor\0")
            (other / "maps").touch()
            (other / "cwd").symlink_to(root)
            wt = root / ".worktrees" / JOB_ID
            wt.mkdir(parents=True)
            (wt / "held").write_bytes(b"fixture")
            alias = root / "alternate-file-name"
            os.link(wt / "held", alias)
            job = {"id": JOB_ID, "worktree": f".worktrees/{JOB_ID}"}
            def scan():
                return R._idle_processes(root, job, {"admin": str(root / ".git/worktrees/test")}, proc)
            scan()
            fd = other / "fd/11"
            fd.symlink_to(alias)
            with self.assertRaisesRegex(R.F.FactoryError, "uses"):
                scan()
            fd.unlink()
            (other / "maps").write_bytes(b"0000-0009 r-xp 000001 00:00 993 " + os.fsencode(wt / "bin") + b"\n")
            with self.assertRaisesRegex(R.F.FactoryError, "maps"):
                scan()
            (other / "maps").write_text("")
            (other / "cmdline").write_bytes(b"worker\0" + JOB_ID.encode() + b"\0")
            with self.assertRaisesRegex(R.F.FactoryError, "uses"):
                scan()
            (other / "cmdline").write_bytes(b"editor\0")
            (other / "cwd").unlink()
            (other / "cwd").symlink_to(wt)
            with self.assertRaisesRegex(R.F.FactoryError, "uses"):
                scan()
            (other / "cwd").unlink()
            (other / "cwd").symlink_to(root)
            (ours / "mountinfo").write_bytes(b"2 0 8:1 / / rw - ext4 root rw\n1 0 0:1 / /proc rw - proc proc rw,hidepid=2\n")
            with self.assertRaisesRegex(R.F.FactoryError, "hides"):
                scan()
            (ours / "mountinfo").write_bytes(b"2 0 8:1 / / rw - ext4 root rw\n1 0 0:1 / /proc rw - proc proc rw\n")
            (ours / "status").write_text("PPid:\t0\nCapEff:\t00000000\n")
            with self.assertRaisesRegex(R.F.FactoryError, "restricted Linux extended attributes"):
                scan()
            (ours / "status").write_text("PPid:\t0\nCapEff:\t00200000\n")
            (ours / "ns/pid").unlink()
            (ours / "ns/pid").symlink_to("pid:[4026599999]")
            with self.assertRaisesRegex(R.F.FactoryError, "namespace"):
                scan()
