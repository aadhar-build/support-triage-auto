"""Gate receipts bind the exact input bytes and commits used for scope checks."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "factory" / "lib"))
import factory as F  # noqa: E402


class TestGateReceiptBinding(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Gate Test",
            "GIT_AUTHOR_EMAIL": "gate@example.test",
            "GIT_COMMITTER_NAME": "Gate Test",
            "GIT_COMMITTER_EMAIL": "gate@example.test",
        }
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(self.root))
        (self.root / "code.txt").write_text("base\n")
        (self.root / "protected.txt").write_text("protected content\n")
        self.git("add", "code.txt", "protected.txt")
        self.git("commit", "-qm", "initial")
        self.base = self.git("rev-parse", "HEAD")

        self.job = F.new_job("001-gate", 1, "gate test")
        self.job.update(stage="gate", round=1)
        self.wt = self.root / self.job["worktree"]
        self.git("worktree", "add", "-qb", self.job["branch"], str(self.wt))
        (self.wt / "code.txt").write_text("candidate\n")
        self.git("commit", "-qam", "job change", cwd=self.wt)
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.wt)

        factory = self.root / "factory"
        self.d = factory / "jobs" / self.job["id"]
        self.d.mkdir(parents=True)
        F.write_job(self.d, self.job)
        self.spec = self.d / "spec.md"
        self.spec_bytes = "## Summary\r\nCafé\r\n\r\n## Files\r\n- code.txt\r\n".encode("utf-8")
        self.spec.write_bytes(self.spec_bytes)
        self.config = factory / "config.json"
        self.config.write_text(json.dumps({
            "base_branch": "main",
            "protected_paths": ["protected.txt"],
            "max_rounds": 2,
        }))
        self.checks = factory / "checks.sh"
        self.checks.write_text("exit 0\n")
        self.round_receipt = self.d / "round-1" / "checks.md"
        self.final_receipt = self.d / "final-checks.md"

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.root, env=self.env, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def run_gate(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO / "factory" / "bin" / "gate"), self.job["id"], *args],
            cwd=self.root, env=self.env, capture_output=True, text=True,
        )

    def assert_receipt(self, path, base, candidate):
        self.assertEqual(F.parse_header(path.read_text()), {
            "GATE": "PASS",
            "CANDIDATE": candidate,
            "BASE": base,
            "SPEC-DIGEST": hashlib.sha256(self.spec_bytes).hexdigest(),
        })

    def test_round_receipt_binds_scope_inputs(self):
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(F.read_job(self.d)["stage"], "review")
        self.assert_receipt(self.round_receipt, self.base, self.candidate)

    def test_final_receipt_binds_scope_inputs(self):
        run = self.run_gate("--final")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(F.read_job(self.d)["stage"], "gate")
        self.assert_receipt(self.final_receipt, self.base, self.candidate)

    def test_stacked_gate_uses_and_records_the_upstream_branch(self):
        (self.wt / "protected.txt").write_text("upstream change\n")
        self.git("add", "protected.txt", cwd=self.wt)
        self.git("commit", "-qm", "upstream change", cwd=self.wt)
        self.git("branch", "factory/upstream", cwd=self.wt)
        upstream = self.git("rev-parse", "HEAD", cwd=self.wt)
        (self.wt / "code.txt").write_text("stacked candidate\n")
        self.git("commit", "-qam", "stacked job change", cwd=self.wt)
        candidate = self.git("rev-parse", "HEAD", cwd=self.wt)
        self.job["stacked_on_branch"] = "factory/upstream"
        F.write_job(self.d, self.job)

        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_receipt(self.round_receipt, upstream, candidate)

    def test_rename_from_protected_path_to_allowed_path_fails(self):
        self.git("config", "diff.renames", "true")
        self.spec.write_text("## Files\n- code.txt\n- allowed.txt\n")
        self.git("mv", "protected.txt", "allowed.txt", cwd=self.wt)
        self.git("commit", "-qm", "rename", cwd=self.wt)

        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = self.round_receipt.read_text()
        self.assertEqual(F.parse_header(report)["GATE"], "FAIL")
        self.assertIn("(only the owner may change these): protected.txt", report)
        self.assertIn("\n- allowed.txt\n", report)

    def test_protected_path_with_line_breaks_is_matched_exactly(self):
        name = "new\rfile\nname.txt"
        self.spec.write_text("## Files\n- *\n")
        config = json.loads(self.config.read_text())
        config["protected_paths"] = [name]
        self.config.write_text(json.dumps(config))
        (self.wt / name).write_text("protected content\n")
        self.git("add", "--", name, cwd=self.wt)
        self.git("commit", "-qm", "add protected path", cwd=self.wt)

        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = self.round_receipt.read_text()
        self.assertEqual(F.parse_header(report)["GATE"], "FAIL")
        self.assertIn("Protected paths changed", report)
        self.assertNotIn("\nname.txt", report)

    def test_allowed_directory_accepts_paths_with_whitespace_and_unicode(self):
        names = ["docs/\t", "docs/Café\r\nnotes.txt"]
        self.spec_bytes = b"## Files\n- code.txt\n- docs/\n"
        self.spec.write_bytes(self.spec_bytes)
        (self.wt / "docs").mkdir()
        for name in names:
            (self.wt / name).write_text("notes\n")
        self.git("add", "--", *names, cwd=self.wt)
        self.git("commit", "-qm", "add docs", cwd=self.wt)
        candidate = self.git("rev-parse", "HEAD", cwd=self.wt)

        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_receipt(self.round_receipt, self.base, candidate)
        self.assertIn("3 file(s) changed", self.round_receipt.read_text())

    def test_base_change_during_checks_refuses_round_receipt(self):
        self.checks.write_text(f"git update-ref refs/remotes/origin/main {self.candidate}\n")

        run = self.run_gate()
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertFalse(self.round_receipt.exists())
        self.assertEqual(F.read_job(self.d)["stage"], "gate")

    def test_spec_change_during_checks_refuses_final_receipt(self):
        self.checks.write_text(f"printf '\\n' >> {shlex.quote(str(self.spec))}\n")

        run = self.run_gate("--final")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertFalse(self.final_receipt.exists())

    def test_candidate_change_during_checks_refuses_round_receipt(self):
        self.checks.write_text("git commit -qm extra --allow-empty\n")

        run = self.run_gate()
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertFalse(self.round_receipt.exists())
        self.assertEqual(F.read_job(self.d)["stage"], "gate")

    def test_missing_spec_refuses_receipt(self):
        self.spec.unlink()

        run = self.run_gate()
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertFalse(self.round_receipt.exists())

    def test_delayed_gate_preserves_an_independent_stage_transition(self):
        # The checks child can run only after gate captured its initial job.
        # Run an actual independent CLI and require it to finish before resuming.
        result = self.root / "advance-output"
        job_command = [sys.executable, str(REPO / "factory/bin/job"), "advance", self.job["id"], "recovery"]
        self.checks.write_text(shlex.join(job_command) + " > " + shlex.quote(str(result)) + "\n")
        self.config.write_text(json.dumps({**json.loads(self.config.read_text()), "checks_timeout_s": 8}))
        finished = self.run_gate()
        self.assertIn("-> recovery", result.read_text())
        self.assertEqual(F.read_job(self.d)["stage"], "recovery", finished.stdout)
        self.assertNotEqual(finished.returncode, 0)
        self.assertFalse(self.round_receipt.exists())

    def test_authorized_nested_final_gate_publishes_and_returns_to_merge(self):
        with F.job_operation(self.d, "merge") as token:
            result = self.run_gate("--merge-token=" + token, "--final")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_receipt(self.final_receipt, self.base, self.candidate)
            self.assertTrue((self.d / F.OPERATION).is_file())
        self.assertFalse((self.d / F.OPERATION).exists())
