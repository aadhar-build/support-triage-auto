"""Run the job consumer against Git refs and worker evidence on disk."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_factory_phases import F, _load_script


class TestGateReceiptConsumption(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.id = "007-receipt"
        self.job = F.new_job(self.id, 7, "test receipts")
        self.job.update(stage="review", round=1, pr=42, risk="low", reviewers={"code": "sonnet"})
        self.d = F.jobs_dir(self.root) / self.id
        self.rd = self.d / "round-1"
        self.rd.mkdir(parents=True)
        self.wt = self.root / self.job["worktree"]
        self.wt.mkdir(parents=True)
        self.remote = self.root / "remote"
        self.git("init", "--quiet", "-b", self.job["branch"])
        self.git("config", "user.name", "Factory Test")
        self.git("config", "user.email", "factory@example.test")
        self.git("commit", "--quiet", "--allow-empty", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("commit", "--quiet", "--allow-empty", "-m", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.git("init", "--bare", "--quiet", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "origin", f"{self.base}:refs/heads/main")
        self.cfg = {"base_branch": "main", "labels": {"needs_human": "needs-human"}}
        (self.root / "factory" / "config.json").write_text(json.dumps(self.cfg))
        self.cli = _load_script("job")
        self.spec = b"## Files\n- code.txt\n"
        (self.d / "spec.md").write_bytes(self.spec)
        for name in ("build.md", "issue.md", "intake.md"):
            (self.d / name).write_text("evidence\n")
        self.checks = self.rd / "checks.md"
        self.checks.write_bytes(self.receipt())
        F.write_job(self.d, self.job)

    def git(self, *args):
        return F.run(["git", *args], cwd=self.wt).strip()

    def receipt(self):
        digest = hashlib.sha256(self.spec).hexdigest()
        return f"GATE: PASS\nCANDIDATE: {self.head}\nBASE: {self.base}\nSPEC-DIGEST: {digest}\n".encode()

    def cmd(self, name):
        return self.cli.COMMANDS[name][0](self.root, self.cfg, [self.id])

    def assert_not_pushed(self):
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{self.job['branch']}"), "")

    def audit(self, role, artifact, relative, *additional):
        inputs = ["spec.md", "build.md", "issue.md", "intake.md", "round-1/checks.md", *additional]

        def digest(name):
            return hashlib.sha256((self.d / name).read_bytes()).hexdigest()

        payload = {
            "evidence_version": 2, "role": role, "candidate_sha": self.head, "base_sha": self.base,
            "inputs": {name: digest(name) for name in inputs},
            "artifact_sha256": digest(artifact),
        }
        payload["inputs"]["job.json"] = self.cli._job_input_digest(self.job)
        (self.d / relative).write_text(json.dumps(payload))

    def reviewer_fixture(self):
        review = "round-1/review-code.md"
        audit = "round-1/worker-reviewer-code.json"
        (self.d / review).write_text("VERDICT: PASS\n")
        self.audit("reviewer-code", review, audit)
        return review, audit

    def approver_fixture(self, *, old_tally=False):
        review, audit = self.reviewer_fixture()
        if old_tally:
            self.job.update(stage="approve", review_evidence={
                "candidate_sha": self.head, "base_sha": self.base,
                "checks_sha256": hashlib.sha256(self.checks.read_bytes()).hexdigest(),
                "reviewers": {"code": {
                    "artifact_sha256": hashlib.sha256((self.d / review).read_bytes()).hexdigest(),
                    "audit_sha256": hashlib.sha256((self.d / audit).read_bytes()).hexdigest(),
                }},
            })
            F.write_job(self.d, self.job)
        else:
            self.cmd("tally")
        (self.d / "decision.md").write_text("APPROVE\n")
        self.audit("approver", "decision.md", "worker-round-1-approver.json", review, audit)

    def test_worker_pr_refuses_a_receipt_for_an_earlier_base(self):
        audit = self.d / "worker-round-1-builder.json"
        audit.write_text(json.dumps({"candidate_sha": self.head}))
        self.git("update-ref", "refs/remotes/origin/main", self.head)

        with self.assertRaisesRegex(F.FactoryError, "job re-review"):
            self.cmd("pr")
        self.assert_not_pushed()

    def test_review_pr_without_builder_audit_still_checks_spec_bytes(self):
        (self.d / "spec.md").write_bytes(self.spec.replace(b"\n", b"\r\n"))

        with self.assertRaisesRegex(F.FactoryError, "job re-review"):
            self.cmd("pr")
        self.assert_not_pushed()

    def test_review_pr_without_builder_audit_still_requires_a_receipt(self):
        self.checks.unlink()

        with self.assertRaisesRegex(F.FactoryError, "job re-review"):
            self.cmd("pr")
        self.assert_not_pushed()

    def test_tally_rejects_a_current_worker_audit_for_a_stale_gate(self):
        (self.d / "spec.md").write_bytes(self.spec.replace(b"\n", b"\r\n"))
        self.reviewer_fixture()

        with self.assertRaisesRegex(F.FactoryError, "job re-review"):
            self.cmd("tally")
        self.assertEqual(F.read_job(self.d)["stage"], "review")

    def test_decide_rejects_a_legacy_receipt_even_after_a_valid_old_tally(self):
        self.checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\n")
        self.approver_fixture(old_tally=True)

        with self.assertRaisesRegex(F.FactoryError, "job re-review"):
            self.cmd("decide")
        self.assertEqual(F.read_job(self.d)["stage"], "approve")

    def test_merge_refuses_legacy_or_stale_final_receipts(self):
        self.approver_fixture()
        self.cmd("decide")
        final_cases = {
            "legacy": f"GATE: PASS\nCANDIDATE: {self.head}\n".encode(),
            "wrong base": self.receipt().replace(f"BASE: {self.base}".encode(), f"BASE: {self.head}".encode()),
            "wrong spec": self.receipt().replace(hashlib.sha256(self.spec).hexdigest().encode(), b"0" * 64),
        }
        run = F.run

        for name, payload in final_cases.items():
            with self.subTest(name=name):
                def git_or_gate(cmd, **kwargs):
                    if cmd[-1] == "--final":
                        (self.d / "final-checks.md").write_bytes(payload)
                        return f"{self.id} final gate PASS\n"
                    return run(cmd, **kwargs)

                with patch.object(F, "run", side_effect=git_or_gate), patch.object(F, "gh_api") as gh:
                    with self.assertRaisesRegex(F.FactoryError, "job re-review"):
                        self.cmd("merge")
                gh.assert_not_called()
                self.assertEqual(F.read_job(self.d)["stage"], "merging")

    def test_merge_rechecks_final_receipt_at_handoff(self):
        self.approver_fixture()
        self.cmd("decide")
        final_checks = self.d / "final-checks.md"
        run = F.run

        def git_or_gate(cmd, **kwargs):
            if cmd[-1] == "--final":
                final_checks.write_bytes(self.receipt())
                return f"{self.id} final gate PASS\n"
            return run(cmd, **kwargs)

        def github(root, method, path, fields=None, check=True):
            self.assertEqual((method, path), ("GET", "pulls/42"))
            final_checks.write_text(f"GATE: PASS\nCANDIDATE: {self.head}\n")
            return {"head": {"sha": self.head}, "base": {"ref": "main", "sha": self.base}}

        with patch.object(F, "run", side_effect=git_or_gate), patch.object(F, "gh_api", side_effect=github):
            with self.assertRaisesRegex(F.FactoryError, "job re-review"):
                self.cmd("merge")
        self.assertEqual(F.read_job(self.d)["stage"], "merging")


if __name__ == "__main__":
    unittest.main()
