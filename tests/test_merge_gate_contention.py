"""A standalone final gate can finish before a merge safely retries."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests import test_gate_receipt_consumption as receipts

ROOT = Path(__file__).resolve().parents[1]


class TestMergeGateContention(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = receipts.TestGateReceiptConsumption()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        (fixture.wt / "code.txt").write_text("candidate\n")
        fixture.git("add", "code.txt")
        fixture.git("commit", "--quiet", "--amend", "--no-edit")
        fixture.head = fixture.git("rev-parse", "HEAD")
        fixture.checks.write_bytes(fixture.receipt())
        fixture.approver_fixture()
        fixture.cmd("decide")

        self.final_checks = fixture.d / "final-checks.md"
        self.final_checks.write_bytes(fixture.receipt() + b"\nPrior final receipt\n")
        fixture.cfg.update(protected_paths=[], max_rounds=2, checks_timeout_s=30)
        (fixture.root / "factory" / "config.json").write_text(json.dumps(fixture.cfg))
        (fixture.root / "factory" / "checks.sh").write_text(
            'printf "%s\\n" "$MERGE_GATE_TEST_MODE" >> "$MERGE_GATE_TEST_DIR/checks.log"\n'
            'if [ "$MERGE_GATE_TEST_MODE" = standalone ]; then\n'
            '    while [ ! -f "$MERGE_GATE_TEST_DIR/release-checks" ]; do sleep 0.02; done\n'
            'fi\n'
        )

        # Git fetch uses the worktree's local bare origin. The main checkout
        # supplies a repository slug to the fake gh executable.
        fake_bin = fixture.root / "fake-bin"
        fake_bin.mkdir()
        self.env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "MERGE_GATE_TEST_DIR": str(fixture.root),
            "MERGE_GATE_TEST_MODE": "merge",
            "MERGE_GATE_TEST_PR": json.dumps({
                "base": {"ref": "main", "sha": fixture.base},
                "head": {"sha": fixture.head},
            }),
        }
        self.github_log = fixture.root / "github.log"
        gh = fake_bin / "gh"
        gh.write_text(
            '#!/bin/sh\n'
            'printf "%s %s\\n" "$3" "$4" >> "$MERGE_GATE_TEST_DIR/github.log"\n'
            'case "$3 $4" in\n'
            '    "GET repos/example/factory/pulls/42") printf "%s\\n" "$MERGE_GATE_TEST_PR";;\n'
            '    "POST repos/example/factory/issues/42/comments" | '
            '"POST repos/example/factory/issues/42/labels" | '
            '"POST repos/example/factory/issues/7/labels") printf "{}\\n";;\n'
            '    *) exit 1;;\n'
            'esac\n'
        )
        gh.chmod(0o700)
        for args in (("init", "-q", "-b", "main"),
                     ("remote", "add", "origin", "https://github.com/example/factory.git")):
            subprocess.run(["git", *args], cwd=fixture.root, env=self.env,
                           check=True, capture_output=True, text=True, timeout=10)

    def start_final_gate(self):
        fixture = self.fixture
        gate = subprocess.Popen(
            [sys.executable, str(ROOT / "factory" / "bin" / "gate"), fixture.id, "--final"],
            cwd=fixture.root, env={**self.env, "MERGE_GATE_TEST_MODE": "standalone"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )

        def stop_gate():
            if gate.poll() is None:
                os.killpg(gate.pid, signal.SIGKILL)
            gate.communicate(timeout=10)

        self.addCleanup(stop_gate)
        self.checks_log = fixture.root / "checks.log"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and gate.poll() is None:
            if self.checks_log.exists() and self.checks_log.read_text() == "standalone\n":
                return gate
            time.sleep(0.02)
        self.fail("the standalone final gate did not reach checks")

    def merge(self):
        fixture = self.fixture
        return subprocess.run(
            [sys.executable, str(ROOT / "factory" / "bin" / "job"), "merge", fixture.id],
            cwd=fixture.root, env=self.env, capture_output=True, text=True, timeout=10,
        )

    def finish_final_gate(self, gate):
        (self.fixture.root / "release-checks").touch()
        output, _ = gate.communicate(timeout=10)
        self.assertEqual(gate.returncode, 0, output)
        self.assertIn("final gate PASS", output)

    def test_busy_final_gate_refuses_merge_without_mutation_then_allows_retry(self):
        fixture = self.fixture
        gate = self.start_final_gate()

        def evidence():
            return {path.relative_to(fixture.d): path.read_bytes()
                    for path in fixture.d.rglob("*") if path.is_file()}

        def git_metadata(name):
            path = fixture.wt / ".git" / name
            return (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None

        before = evidence()
        git_before = {name: git_metadata(name) for name in ("FETCH_HEAD", "ORIG_HEAD")}

        blocked = self.merge()
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("REFUSED:", blocked.stdout)
        self.assertIn("retry", blocked.stdout.lower())
        self.assertEqual(evidence(), before)
        self.assertEqual(fixture.git("rev-parse", "HEAD"), fixture.head)
        self.assertEqual({name: git_metadata(name) for name in git_before}, git_before)
        self.assertFalse(self.github_log.exists())
        self.assertEqual(self.checks_log.read_text(), "standalone\n")

        self.finish_final_gate(gate)

        retry = self.merge()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn("-> needs-human", retry.stdout)
        self.assertEqual(receipts.F.read_job(fixture.d)["stage"], "needs-human")
        self.assertEqual(self.checks_log.read_text(), "standalone\nmerge\n")
        self.assertEqual(self.github_log.read_text().splitlines(), [
            "GET repos/example/factory/pulls/42",
            "POST repos/example/factory/issues/42/comments",
            "POST repos/example/factory/issues/42/labels",
            "POST repos/example/factory/issues/7/labels",
        ])

    def test_authorized_final_gate_reports_temporary_failure_if_worktree_is_busy(self):
        fixture = self.fixture
        gate = self.start_final_gate()
        with receipts.F.job_operation(fixture.d, "merge") as token:
            nested = subprocess.run(
                [
                    sys.executable, str(ROOT / "factory" / "bin" / "gate"), fixture.id,
                    "--final", "--merge-token=" + token,
                ],
                cwd=fixture.root, env=self.env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(nested.returncode, os.EX_TEMPFAIL, nested.stdout + nested.stderr)
        self.assertEqual(self.checks_log.read_text(), "standalone\n")
        self.finish_final_gate(gate)
