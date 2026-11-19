"""Cross-process job operation and final-gate admission checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "factory" / "lib"
GATE = REPO / "factory" / "bin" / "gate"


def py(code, *args, cwd=None, timeout=8):
    env = dict(os.environ, PYTHONPATH=str(LIB))
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=cwd,
                          env=env, text=True, capture_output=True, timeout=timeout)


class OperationAdmissionTests(unittest.TestCase):
    def test_job_mutex_contends_across_processes_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            holder = subprocess.Popen([sys.executable, "-u", "-c", '''
import sys
from pathlib import Path
import factory as F
with F.job_mutex(Path(sys.argv[1])):
 print("locked", flush=True)
 sys.stdin.readline()
''', str(d)], env=dict(os.environ, PYTHONPATH=str(LIB)), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                contender = py('''
import sys
from pathlib import Path
import factory as F
with F.job_mutex(Path(sys.argv[1])): pass
''', d, timeout=3)
                self.assertNotEqual(contender.returncode, 0, contender.stdout)
            finally:
                holder.communicate("release\n", timeout=5)
            self.assertEqual(holder.returncode, 0)
            retry = py('''
import sys
from pathlib import Path
import factory as F
with F.job_mutex(Path(sys.argv[1])): pass
''', d)
            self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_final_gate_requires_token_and_actual_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "factory/jobs/001-test"
            job.mkdir(parents=True)
            (root / ".gitignore").write_text("factory/jobs/\n")
            (root / "factory/config.json").write_text('{"base_branch":"main"}')
            (job / "job.json").write_text('{"id":"001-test","stage":"merging","worktree":".","round":1}')
            for args in (["git", "init", "-q", "-b", "main"], ["git", "add", "."],
                         ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"]):
                subprocess.run(args, cwd=root, check=True, capture_output=True)
            result = py('''
import json, subprocess, sys
from pathlib import Path
import factory as F
root, job, gate = map(Path, sys.argv[1:])
def call(args):
 p = subprocess.run([sys.executable, str(gate), "001-test", *args], cwd=root, text=True, capture_output=True, timeout=5)
 return [p.returncode, p.stdout + p.stderr]
with F.job_operation(job, "merge") as token:
 direct = call(["--final", "--merge-token=" + token])
 wrong = call(["--final", "--merge-token=wrong"])
 plain_final = call(["--final"])
 plain_gate = call([])
 sibling = subprocess.run([sys.executable, "-c", "import subprocess,sys; p=subprocess.run([sys.executable,sys.argv[1], '001-test', '--final', '--merge-token='+sys.argv[2]],capture_output=True,text=True); print(p.stdout+p.stderr)", str(gate), token], cwd=root, text=True, capture_output=True, timeout=5)
 print(json.dumps([direct, wrong, plain_final, plain_gate, sibling.stdout]))
''', root, job, GATE, cwd=root, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            direct, wrong, plain_final, plain_gate, sibling = json.loads(result.stdout)
            # The authorized gate gets beyond admission and reaches the intentionally
            # missing origin. All other calls must fail before attempting that fetch.
            self.assertIn("git fetch", direct[1])
            for refused in (wrong[1], plain_final[1], plain_gate[1], sibling):
                self.assertIn("REFUSED:", refused)
                self.assertNotIn("git fetch", refused)

    def test_crashed_operation_requires_verified_explicit_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            job = root / "factory/jobs/001-test"
            job.mkdir(parents=True)
            (root / "factory/config.json").write_text("{}")
            state = {"id": "001-test", "stage": "gate", "owner_minutes": 0}
            (job / "job.json").write_text(json.dumps(state))
            def command(*args):
                return subprocess.run([sys.executable, str(REPO / "factory/bin/job"), *args],
                                      cwd=root, capture_output=True, text=True, timeout=5)
            parent = subprocess.Popen([sys.executable, "-u", "-c", """
import sys
from pathlib import Path
import factory as F
with F.job_operation(Path(sys.argv[1]), "pr") as token:
    F.begin_effect("push fixture branch")
    print(token, flush=True)
    sys.stdin.readline()
""", str(job)], env=dict(os.environ, PYTHONPATH=str(LIB)), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                token = parent.stdout.readline().strip()
                self.assertTrue(token)
                live = command("reconcile-operation", job.name, token, "--reconciled")
                self.assertIn("still be running", live.stdout)
                refused = command("record", job.name, "owner_minutes", "9")
                self.assertEqual(refused.returncode, 1, refused.stdout)
            finally:
                parent.kill()
                parent.communicate(timeout=5)
            marker = job / ".factory-operation.json"
            self.assertEqual(json.loads(marker.read_bytes())["effect"], "push fixture branch")
            self.assertEqual(command("record", job.name, "owner_minutes", "9").returncode, 1)
            self.assertEqual(command("reconcile-operation", job.name, "wrong", "--reconciled").returncode, 1)
            self.assertEqual(json.loads((job / "job.json").read_bytes()), state)
            released = command("reconcile-operation", job.name, token, "--reconciled")
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(command("record", job.name, "owner_minutes", "9").returncode, 0)
            self.assertEqual(json.loads((job / "job.json").read_bytes())["owner_minutes"], 9)


if __name__ == "__main__":
    unittest.main()
