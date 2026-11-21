"""Exercise approved generated-file retirement through real linked Git worktrees."""

import hashlib
import json
import os
import py_compile
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
import worktree_retirement as retirement


class GeneratedFileRetirement(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("switch", "-C", "main")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "base")
        self.wt = self.root / ".worktrees" / JOB_ID
        self.branch = "factory/" + JOB_ID
        self.git("worktree", "add", "-qb", self.branch, str(self.wt))
        for name in ("example.py", "other.py"):
            (self.wt / name).write_text('def greeting():\n    return "hello"\n')
        self.git("-C", str(self.wt), "add", "example.py", "other.py")
        self.git("-C", str(self.wt), "commit", "-qm", "candidate")
        self.git("merge", "--ff-only", self.branch)
        self.sha = self.git("rev-parse", self.branch).stdout.strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.write_job(stage="merged")
        self.record = self.root / ".git/factory-retirements" / (JOB_ID + ".json")
        self.pyc = self.compile("example.py")
        # Git, file classification, job state, evidence and the journal are real.
        proof = {"repository": "fixture/software-factory", "repository_id": 17, "pr": 2,
                 "pr_id": 23, "base_ref": "main", "base_sha": self.sha, "merge_commit_sha": self.sha,
                 "candidate_sha": self.sha, "job_pr_association": "verified", "candidate_merge_identity": "exact"}
        merge = mock.patch.object(retirement.retirement_git, "verify_merge", return_value=proof)
        processes = mock.patch.object(retirement, "_idle_processes")
        self.merge = merge.start()
        self.addCleanup(merge.stop)
        self.idle = processes.start()
        self.addCleanup(processes.stop)

    def compile(self, name):
        return Path(py_compile.compile(str(self.wt / name), doraise=True,
                                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))

    def run_retirement(self, **options):
        return retirement.run(self.root, JOB_ID, **options)

    def manifest(self):
        result = self.run_retirement(inventory_generated=True)
        self.assertEqual(result["status"], "generated inventory")
        self.assertEqual(result["assessment"], "eligibility not evaluated")
        manifest = result["manifest"]
        self.assertEqual(manifest["job_id"], JOB_ID)
        self.assertEqual(manifest["candidate_sha"], self.sha)
        self.assertEqual(type(manifest["schema_version"]), int)
        self.assertRegex(manifest["digest"], r"^[0-9a-f]{64}$")
        self.assertIn(self.pyc.relative_to(self.wt).as_posix(), [row["path"] for row in manifest["entries"]])
        return manifest

    def assert_refused(self, **options):
        try:
            result = self.run_retirement(**options)
        except retirement.F.FactoryError:
            return
        self.assertIn(result["status"], ("blocked", "inconclusive"), result)

    def test_cli_inventory_returns_exact_manifest_without_changing_checkout_or_job(self):
        bytecode = [self.pyc, self.compile("other.py")]
        expected_files = {path.relative_to(self.wt).as_posix():
                          (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
                          for path in bytecode}

        def snapshot():
            return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                    for folder in (self.wt, self.state.parent.resolve()) for path in folder.rglob("*")}

        before, refs = snapshot(), self.git("show-ref").stdout
        self.assertFalse(self.record.parent.exists())
        expected = self.manifest()
        command = self.cli("retire-worktree", JOB_ID, "--inventory-generated")
        output = json.loads(command.stdout)
        self.assertEqual(output["job_id"], JOB_ID)
        self.assertEqual(output["status"], "generated inventory")
        self.assertEqual(output["assessment"], "eligibility not evaluated")
        self.assertEqual(output["manifest"], expected)
        files = {row["path"]: (row["sha256"], row["size"])
                 for row in output["manifest"]["entries"] if row["kind"] == "file"}
        self.assertEqual(files, expected_files)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertFalse(self.record.parent.exists())

    def test_default_retains_bytecode_and_matching_approval_preserves_job_and_refs(self):
        before = self.pyc.read_bytes()
        state = self.state.read_bytes()
        refs = self.git("show-ref").stdout
        self.assert_refused(apply=True)
        manifest = self.manifest()
        self.assertEqual(self.manifest(), manifest)
        self.assertEqual(self.pyc.read_bytes(), before)
        self.assertFalse(self.record.exists())
        result = self.run_retirement(apply=True, discard_generated=manifest["digest"])
        self.assertEqual(result["status"], "retired")
        self.assertFalse(os.path.lexists(self.wt))
        self.assertEqual(self.state.read_bytes(), state)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertEqual(retirement.read_retirement(self.root, JOB_ID)["status"], "retired")

    def test_new_reproducible_bytecode_invalidates_approved_inventory(self):
        approved = self.manifest()["digest"]
        second = self.compile("other.py")
        self.assertNotEqual(self.manifest()["digest"], approved)
        self.assert_refused(apply=True, discard_generated=approved)
        self.assertTrue(self.pyc.is_file() and second.is_file())
        self.assertFalse(self.record.exists())

    def test_permissions_change_and_unknown_ignored_file_block_approved_discard(self):
        approved = self.manifest()["digest"]
        initial_mode = self.pyc.stat().st_mode & 0o7777
        self.pyc.chmod(initial_mode | 0o100)
        self.assert_refused(inventory_generated=True)
        self.assert_refused(apply=True, discard_generated=approved)
        self.assertTrue(self.pyc.is_file())
        self.pyc.chmod(initial_mode)
        unknown = self.pyc.with_name("private.cache")
        unknown.write_bytes(b"local-only cache notes")
        self.assert_refused(inventory_generated=True)
        self.assert_refused(apply=True, discard_generated=approved)
        self.assertEqual(unknown.read_bytes(), b"local-only cache notes")
        self.assertFalse(self.record.exists())

    def test_discard_digest_is_valid_only_for_an_explicit_apply(self):
        well_formed_digest = "a" * 64
        before = self.pyc.read_bytes()
        for options in ({}, {"inventory_generated": True}, {"recover": True}):
            with self.subTest(options=options):
                self.assert_refused(discard_generated=well_formed_digest, **options)
        self.assertEqual(self.pyc.read_bytes(), before)
        self.assertFalse(self.record.exists())

    def test_generated_approval_cannot_confirm_uncertain_pr_or_rewritten_merge(self):
        approved = self.manifest()["digest"]
        proof = dict(self.merge.return_value)
        for field, uncertain in (("job_pr_association", "local_only"),
                                 ("candidate_merge_identity", "content_equivalent")):
            with self.subTest(field=field):
                self.merge.return_value = {**proof, field: uncertain}
                self.assert_refused(apply=True, discard_generated=approved)
                self.assertTrue(self.pyc.is_file())
                self.assertFalse(self.record.exists())
        result = self.run_retirement(apply=True, discard_generated=approved, confirm_pr=f"2@{self.sha}")
        self.assertEqual(result["status"], "retired")

    def test_replaced_bytecode_after_move_stays_parked_with_its_new_contents(self):
        approved = self.manifest()["digest"]
        real_git = retirement._git
        relative_path = self.pyc.relative_to(self.wt)
        private = b"local-only contents written after parking"

        def replace_after_move(root, *args):
            output = real_git(root, *args)
            if args[:2] == ("worktree", "move"):
                destination = Path(args[-1]) / relative_path
                replacement = destination.with_name("replacement")
                replacement.write_bytes(private)
                replacement.replace(destination)
            return output

        with mock.patch.object(retirement, "_git", side_effect=replace_after_move):
            self.assert_refused(apply=True, discard_generated=approved)
        record = retirement.read_retirement(self.root, JOB_ID)
        self.assertEqual(record["status"], "prepared")
        parked = self.record.parent / record["parked"]
        self.assertEqual((parked / relative_path).read_bytes(), private)

    def test_recovery_restores_unchanged_parked_bytecode_without_replaying_discard(self):
        approved = self.manifest()["digest"]
        before = self.pyc.read_bytes()

        def interrupt_parked_scan(root, job, local, *, parked=None):
            if parked is not None:
                raise RuntimeError("interrupted after move")

        self.idle.side_effect = interrupt_parked_scan
        with self.assertRaisesRegex(RuntimeError, "interrupted after move"):
            self.run_retirement(apply=True, discard_generated=approved)
        self.idle.side_effect = None
        self.assertFalse(os.path.lexists(self.wt))
        self.assertEqual(self.run_retirement(recover=True)["status"], "cancelled")
        self.assertEqual(self.pyc.read_bytes(), before)
        self.assertEqual(self.git("rev-parse", self.branch).stdout.strip(), self.sha)
