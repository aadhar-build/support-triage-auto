"""Retirement never upgrades a local archive into authenticated technical evidence."""
import hashlib
import os
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory" / "lib"))
import evidence_adapter as adapter
import retired_evidence


class RetirementEvidenceTests(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.job_dir = self.root / "factory" / "jobs" / JOB_ID
        self.state = self.job_dir / "job.json"
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/remotes/origin/main", self.head)
        self.wt = self.root / ".worktrees" / JOB_ID
        self.git("worktree", "add", "-qb", f"factory/{JOB_ID}", str(self.wt))
        self.write_job(stage="merged", history=[
            {"stage": "build", "at": "2020-01-01T00:00:00Z"},
            {"stage": "gate", "at": "2020-01-01T01:00:00Z"},
            {"stage": "review", "note": "gate passed", "at": "2020-01-01T02:00:00Z"},
            {"stage": "merged", "at": "2020-01-01T03:00:00Z"},
        ])
        self.job = json.loads(self.state.read_bytes())
        (self.job_dir / "round-1").mkdir()
        self.checks = self.job_dir / "round-1/checks.md"
        self.checks.write_text("legacy unbound gate note\n")

    def capture(self):
        return retired_evidence.capture(self.root, self.job_dir, self.job)

    def valid_detached(self, evidence):
        return retired_evidence.valid_detached(self.root, self.job_dir, self.job, evidence)

    def test_capture_refuses_to_erase_verified_gate_coverage(self):
        (self.job_dir / "spec.md").write_bytes(b"spec")
        digest = hashlib.sha256(b"spec").hexdigest()
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.head}\nSPEC-DIGEST: {digest}\n\n")
        record = adapter.reporting_record(self.root, adapter.F.load_config(self.root), self.job_dir)
        self.assertEqual(record["first_gate"]["status"], "pass")
        with self.assertRaisesRegex(adapter.F.FactoryError, "receipt|evidence"):
            self.capture()

    def test_legacy_checkout_absence_retained_branch_and_artifacts_are_required(self):
        evidence = self.capture()
        self.assertEqual(evidence, self.capture())
        self.assertFalse(self.valid_detached(evidence))
        self.git("worktree", "remove", str(self.wt))
        self.assertTrue(self.valid_detached(evidence))
        self.assertFalse(self.valid_detached({**evidence, "verdict": "pass"}))
        self.wt.symlink_to("missing")
        self.assertFalse(self.valid_detached(evidence))
        self.wt.unlink()
        self.checks.write_text("legacy gate note changed\n")
        self.assertFalse(self.valid_detached(evidence))
        self.checks.write_text("legacy unbound gate note\n")
        self.git("commit", "--allow-empty", "-qm", "advanced")
        self.git("update-ref", f"refs/heads/factory/{JOB_ID}", "HEAD")
        self.assertFalse(self.valid_detached(evidence))

    def test_detached_check_requires_no_git_registration_even_at_another_path(self):
        evidence = self.capture()
        self.git("worktree", "move", str(self.wt), "relocated")
        self.assertFalse(self.valid_detached(evidence))

    def test_missing_spec_allowed_but_dirty_checkout_and_unsafe_receipts_refused(self):
        (self.wt / "untracked").write_text("local edits")
        with self.assertRaises(adapter.F.FactoryError):
            self.capture()
        (self.wt / "untracked").unlink()
        self.checks.unlink()
        self.checks.symlink_to(self.state)
        with self.assertRaises(adapter.F.FactoryError):
            self.capture()

    def test_reports_retired_history_without_promoting_unbound_gate_or_breaking_owner_coverage(self):
        def report(year=2099):
            return adapter.reporting_record(self.root, adapter.F.load_config(self.root), self.job_dir,
                                            as_of=datetime(year, 1, 1, tzinfo=timezone.utc))

        original = report()
        self.assertNotIn("retirement", original["guardrails"])
        evidence = self.capture()
        from retirement_git import validate
        local = validate(self.root, self.job_dir, self.job, adapter.F.load_config(self.root))
        self.git("worktree", "remove", str(self.wt))
        directory = self.root / ".git/factory-retirements"
        directory.mkdir(mode=0o700)
        path = directory / f"{JOB_ID}.json"
        record = {"schema_version": 1, "status": "retired", "job_id": JOB_ID, "candidate_sha": self.head,
                  "prepared_at": "2026-01-01T00:00:00Z", "retired_at": "2026-01-01T00:00:01Z",
                  "parked": "1" * 32 + ".checkout", "owner_confirmed_pr": None, "local": local,
                  "merge": {"candidate_sha": self.head, "repository": local["repository"], "pr": 2,
                            "repository_id": 12, "pr_id": 17, "base_ref": "main", "base_sha": local["base_sha"],
                            "merge_commit_sha": self.head, "job_pr_association": "verified",
                            "candidate_merge_identity": "exact"},
                  "evidence": evidence}
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        self.assertNotIn("retirement", report(2025)["guardrails"])
        after = report()
        self.assertIsNone(after["first_gate"])
        self.assertEqual(after["guardrails"].get("retirement", {}).get("status"), "retired")
        self.assertEqual(after["guardrails"]["retirement"]["verification"], "local_archive")
        self.assertEqual(after["guardrails"]["requirements"], original["guardrails"]["requirements"])
        self.assertEqual(after["guardrails"]["coverage"], original["guardrails"]["coverage"])
        self.assertEqual(after["diagnostics"], original["diagnostics"])
        for group, changes in (("evidence", {"verified_native_receipts": ["pass"]}),
                               ("merge", {"pr": 3}), ("evidence", {"candidate_sha": "a" * 40})):
            with self.subTest(group=group, changes=changes):
                path.write_text(json.dumps({**record, group: {**record[group], **changes}}))
                damaged = report()
                self.assertEqual(damaged["guardrails"]["retirement"]["status"], "unverified")
                self.assertEqual(damaged["guardrails"]["coverage"]["covered"], 0)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory search permissions")
    def test_inaccessible_checkout_parent_cannot_prove_absence(self):
        evidence = self.capture()
        self.git("worktree", "remove", str(self.wt))
        self.wt.parent.chmod(0)
        try:
            self.assertFalse(self.valid_detached(evidence))
        finally:
            self.wt.parent.chmod(0o700)

    def test_changing_native_review_input_invalidates_archived_evidence(self):
        build = self.job_dir / "build.md"
        build.write_text("first build")
        evidence = self.capture()
        self.git("worktree", "remove", str(self.wt))
        build.write_text("amended build")
        self.assertFalse(self.valid_detached(evidence))

    def test_replaced_identical_job_directory_fails_snapshot_and_detached_validation(self):
        import shutil
        evidence = self.capture()
        self.git("worktree", "remove", str(self.wt))
        old = self.job_dir.with_name("stored-job")
        self.job_dir.rename(old)
        shutil.copytree(old, self.job_dir)
        self.assertFalse(self.valid_detached(evidence))

    def test_unavailable_verifier_cannot_treat_bound_receipt_as_zero_coverage(self):
        from unittest.mock import patch
        (self.job_dir / "spec.md").write_bytes(b"spec")
        digest = hashlib.sha256(b"spec").hexdigest()
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.head}\nSPEC-DIGEST: {digest}\n\n")
        with patch.object(adapter, "_installed_validator", return_value=None):
            with self.assertRaisesRegex(adapter.F.FactoryError, "verifier|receipt"):
                self.capture()

    def test_bound_but_unverified_gate_projection_survives_removal(self):
        import evidence_bundle as bundle
        spec = b"## Files\n- code.txt\n"
        (self.job_dir / "spec.md").write_bytes(spec)
        self.job["history"][2]["note"] = "unpublished"
        self.state.write_text(json.dumps(self.job))
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.head}\n"
                               f"SPEC-DIGEST: {hashlib.sha256(spec).hexdigest()}\n\n## Summary\nlegacy output\n")
        selection = bundle.ARTIFACT_SELECTION.fullmatch(
            "round-1/checks.md@" + hashlib.sha256(self.checks.read_bytes()).hexdigest())
        cutoff = datetime.now(timezone.utc)
        before = bundle.project_native_receipt(self.root, JOB_ID, selection, cutoff)
        self.assertEqual(before[1], "projected")
        self.assertEqual(before[0]["claim_verification"], "unverified")
        evidence = self.capture()
        self.git("worktree", "move", str(self.wt), str(self.root / "parked"))
        self.assertTrue(retired_evidence.matches_snapshot(self.root, self.job_dir, self.job, evidence))
        self.git("worktree", "remove", str(self.root / "parked"))
        self.assertEqual(bundle.project_native_receipt(self.root, JOB_ID, selection, cutoff)[:3], before[:3])

    def test_retain_gate_that_an_as_of_report_can_verify(self):
        self.job["history"][2]["at"] = "2097-01-01T00:00:00Z"
        self.job["history"][3]["at"] = "2097-01-02T00:00:00Z"
        self.state.write_text(json.dumps(self.job))
        (self.job_dir / "spec.md").write_bytes(b"spec")
        digest = hashlib.sha256(b"spec").hexdigest()
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.head}\nSPEC-DIGEST: {digest}\n\n")
        report = adapter.reporting_record(self.root, adapter.F.load_config(self.root), self.job_dir,
                                          as_of=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(report["first_gate"]["status"], "pass")
        with self.assertRaises(adapter.F.FactoryError):
            self.capture()

    def test_redirected_git_directory_cannot_hide_a_verified_gate(self):
        from unittest.mock import patch
        (self.job_dir / "spec.md").write_bytes(b"spec")
        digest = hashlib.sha256(b"spec").hexdigest()
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.head}\nSPEC-DIGEST: {digest}\n\n")
        # This second repository presents the same clean tree and candidate but a different reporting base.
        alternate = self.root / "other"
        self.git("clone", "-q", "--no-local", str(self.root), str(alternate))
        self.git("-C", str(alternate), "switch", "-qc", f"factory/{JOB_ID}", self.head)
        self.git("-C", str(alternate), "-c", "user.name=Other", "-c", "user.email=other@example.invalid",
                 "commit", "--allow-empty", "-qm", "other base")
        self.git("-C", str(alternate), "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("-C", str(alternate), "update-ref", f"refs/heads/factory/{JOB_ID}", self.head)
        with patch.dict(os.environ, GIT_DIR=str(alternate / ".git"), GIT_WORK_TREE=str(self.wt)):
            with self.assertRaises(adapter.F.FactoryError):
                self.capture()
