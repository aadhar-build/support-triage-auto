"""A gate in checks excludes other gates on the same real worktree."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "factory" / "lib"))
import factory as F  # noqa: E402


class TestGateExclusion(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Gate Test",
            "GIT_AUTHOR_EMAIL": "gate@example.test",
            "GIT_COMMITTER_NAME": "Gate Test",
            "GIT_COMMITTER_EMAIL": "gate@example.test",
            "GATE_TEST_DIR": temp.name,
        }
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(self.root))
        (self.root / "code.txt").write_text("base\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "base")

        self.job = F.new_job("001-gate", 1, "gate exclusion test")
        self.job.update(stage="gate", round=1)
        wt = self.root / self.job["worktree"]
        self.git("worktree", "add", "-qb", self.job["branch"], str(wt))
        (wt / "code.txt").write_text("candidate\n")
        self.git("commit", "-qam", "job change", cwd=wt)
        factory = self.root / "factory"
        self.job_dir = factory / "jobs" / self.job["id"]
        self.job_dir.mkdir(parents=True)
        F.write_job(self.job_dir, self.job)
        (self.job_dir / "spec.md").write_text("## Files\n- code.txt\n")
        (factory / "config.json").write_text(json.dumps({
            "base_branch": "main",
            "protected_paths": [],
            "max_rounds": 2,
            "checks_timeout_s": 30,
        }))
        (factory / "checks.sh").write_text(
            'printf "%s\\n" "$GATE_TEST_RUN" >> "$GATE_TEST_DIR/check-invocations"\n'
            'if [ "$GATE_TEST_RUN" = holder ]; then\n'
            '    while [ ! -f "$GATE_TEST_DIR/release-checks" ]; do sleep 0.02; done\n'
            'fi\n'
        )

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args], cwd=cwd or self.root, env=self.env, check=True,
            capture_output=True, text=True, timeout=10,
        )

    def gate(self, name, *args):
        return subprocess.Popen(
            [sys.executable, str(ROOT / "factory" / "bin" / "gate"), self.job["id"], *args],
            cwd=self.root, env={**self.env, "GATE_TEST_RUN": name}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def stop_gate(self, process):
        # The gate may have died while its checks process is still alive.
        if process.poll() in (None, -signal.SIGKILL):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)

    def start_holder(self, *holder_args):
        holder = self.gate("holder", *holder_args)
        self.addCleanup(self.stop_gate, holder)
        invocations = self.root / "check-invocations"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and holder.poll() is None:
            if invocations.exists() and invocations.read_text() == "holder\n":
                return holder, invocations
            time.sleep(0.02)
        self.fail("the holder gate did not reach checks")

    def assert_excludes_other_gates(self, *holder_args):
        holder, invocations = self.start_holder(*holder_args)
        for contender_args in ((), ("--final",)):
            with self.subTest(contender_args=contender_args):
                contender = self.gate("contender", *contender_args)
                self.addCleanup(self.stop_gate, contender)
                output, _ = contender.communicate(timeout=10)
                self.assertEqual(contender.returncode, 1, output)
                self.assertIn("REFUSED:", output)
        self.assertEqual(invocations.read_text().splitlines(), ["holder"])

        (self.root / "release-checks").touch()
        output, _ = holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, 0, output)
        self.assertIn("gate PASS", output)

        retry = self.gate("retry", "--final")
        self.addCleanup(self.stop_gate, retry)
        output, _ = retry.communicate(timeout=10)
        self.assertEqual(retry.returncode, 0, output)
        self.assertEqual(invocations.read_text().splitlines(), ["holder", "retry"])

    def test_round_gate_excludes_other_gates_during_checks(self):
        self.assert_excludes_other_gates()

    def test_final_gate_excludes_other_gates_during_checks(self):
        self.assert_excludes_other_gates("--final")

    def test_checks_still_exclude_gates_after_the_gate_process_dies(self):
        holder, invocations = self.start_holder()
        holder.kill()
        holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, -signal.SIGKILL)

        contender = self.gate("contender", "--final")
        self.addCleanup(self.stop_gate, contender)
        output, _ = contender.communicate(timeout=10)
        self.assertEqual(contender.returncode, 1, output)
        self.assertIn("REFUSED:", output)
        self.assertEqual(invocations.read_text().splitlines(), ["holder"])

    def test_job_record_can_commit_while_checks_are_running(self):
        holder, _ = self.start_holder()
        record = subprocess.run(
            [
                sys.executable, str(ROOT / "factory" / "bin" / "job"),
                "record", self.job["id"], "owner_minutes", "12.5",
            ],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(record.returncode, 0, record.stdout + record.stderr)

        (self.root / "release-checks").touch()
        output, _ = holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, 1, output)
        self.assertIn("job changed while the gate ran", output)
        self.assertEqual(F.read_job(self.job_dir)["owner_minutes"], 12.5)
        self.assertFalse((self.job_dir / "round-1" / "checks.md").exists())
