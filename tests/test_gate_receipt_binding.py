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

    def test_color_only_output_is_readable_and_remains_a_bound_pass(self):
        self.checks.write_text(r"printf '\033[1;32mPASS\033[0m: Café ✓\n'")
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_receipt(self.round_receipt, self.base, self.candidate)
        report = self.round_receipt.read_text()
        self.assertNotIn("\x1b", report)
        self.assertIn("\n    PASS: Café ✓\n", report)

    def test_deceptive_output_cannot_overwrite_the_gate_failure_or_escape_the_excerpt(self):
        self.checks.write_text(
            r"printf 'original failure\rGATE: PASS\b\000\177\033[2K\033]0;title\007\205\302\205\342\200\256\342\200\250\377\n'"
            "\n" + r"printf '```\nGATE: PASS\n## Summary\nliteral:\\x1b\n' >&2" + "\nexit 9\n")
        run = self.run_gate("--final")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = self.final_receipt.read_bytes().decode("utf-8")
        self.assertEqual(F.parse_header(report), {
            "GATE": "FAIL", "CANDIDATE": self.candidate, "BASE": self.base,
            "SPEC-DIGEST": hashlib.sha256(self.spec_bytes).hexdigest(),
        })
        self.assertIn("- checks.sh exited 9\n", report)
        self.assertIn(r"original failure\rGATE: PASS\x08\x00\x7f\x1b[2K\x1b]0;title\x07\x85\u0085\u202e\u2028\xff", report)
        self.assertIn("\n    ```\n    GATE: PASS\n    ## Summary\n", report)
        self.assertIn(r"literal:\\x1b", report)
        self.assertTrue(all(c in "\n\t" or c.isprintable() for c in report))

    def test_tail_counts_lf_lines_and_separates_unterminated_streams(self):
        self.checks.write_text(
            "for n in {1..121}; do printf 'line %s\\n' \"$n\"; done\n"
            + r"printf 'bare\rprogress\tstep\013still same line\014tail\033['" + "\n"
            + r"printf '31mstderr\r\nlast\r' >&2" + "\n")
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = self.round_receipt.read_text()
        self.assertNotIn("    line 4\n", report)
        self.assertIn("    line 5\n", report)
        self.assertIn("    bare\\rprogress\tstep\\x0bstill same line\\x0ctail\\x1b[\n", report)
        self.assertIn("    31mstderr\n    last\\r\n", report)

    def test_re_review_preserves_the_legacy_receipt_and_rechecks_the_same_candidate(self):
        self.checks.write_text("printf 'diagnostic\\n'\n")
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # A receipt produced by the old gate persisted raw SGR in the log.
        old = self.round_receipt.read_bytes().replace(b"diagnostic", b"\x1b[32mdiagnostic\x1b[0m")
        self.round_receipt.write_bytes(old)
        self.checks.write_text(r"printf '\033[32mdiagnostic\033[0m\n'")
        restart = subprocess.run(
            [sys.executable, str(REPO / "factory/bin/job"), "re-review", self.job["id"]],
            cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(restart.returncode, 0, restart.stdout + restart.stderr)
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        fresh = self.d / "round-2/checks.md"
        self.assert_receipt(fresh, self.base, self.candidate)
        self.assertIn(b"\n    diagnostic\n", fresh.read_bytes())
        self.assertNotIn(b"\x1b", fresh.read_bytes())
        self.assertEqual(self.round_receipt.read_bytes(), old)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), self.candidate)
        self.assertEqual(F.read_job(self.d)["stage"], "review")

    def test_large_binary_line_retains_bounded_visible_tail_and_failure(self):
        self.checks.write_text(
            "python3 -c 'import sys; sys.stdout.buffer.write(bytes([255]) * 100000 + \" final diagnostic ✓\".encode())'\n"
            "exit 7\n")
        run = self.run_gate("--final")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = self.final_receipt.read_bytes()
        self.assertEqual(F.parse_header(report.decode())["GATE"], "FAIL")
        self.assertIn(b"- checks.sh exited 7\n", report)
        self.assertIn(b"[Earlier checks.sh output omitted", report)
        self.assertTrue(report.endswith(" final diagnostic ✓\n".encode()))
        # The budget includes Markdown indentation, escapes, newlines and the
        # omission notice. The trusted header and section description are separate.
        excerpt = report[report.index(b"[Earlier checks.sh output omitted"):]
        self.assertLessEqual(len(excerpt), 32 * 1024)

    def test_raw_log_budget_marks_omission_even_when_styles_render_small(self):
        self.checks.write_text(
            r"""python3 -c 'import sys; sys.stdout.buffer.write(b"lost start" + b"\x1b[0m" * 12000 + b"\nlast result\n")'""")
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_receipt(self.round_receipt, self.base, self.candidate)
        report = self.round_receipt.read_text()
        self.assertIn("[Earlier checks.sh output omitted", report)
        self.assertNotIn("lost start", report)
        self.assertIn("\n    last result\n", report)
