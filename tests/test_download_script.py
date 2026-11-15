"""Tests for scripts/download_data.sh. A stub kaggle CLI is used; nothing reaches the network."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

BASH = shutil.which("bash")
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "download_data.sh")

USER_SENTINEL = "SENTINEL_KAGGLE_USER_9f3a"
KEY_SENTINEL = "SENTINEL_KAGGLE_KEY_7c1e"


@unittest.skipUnless(BASH, "bash is not available")
class TestDownloadScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.home = os.path.join(root, "home")
        self.data_dir = os.path.join(root, "data")
        self.bin = os.path.join(root, "bin")
        self.empty_bin = os.path.join(root, "emptybin")
        self.marker = os.path.join(root, "kaggle_calls.log")
        for d in (self.home, self.bin, self.empty_bin):
            os.makedirs(d)

    def tearDown(self):
        self._tmp.cleanup()

    def write_stub(self, writes_csv=True):
        lines = [
            "#!/bin/sh",
            f"printf 'called\\n' >> '{self.marker}'",
            'dest=""',
            'while [ "$#" -gt 0 ]; do',
            '  if [ "$1" = "-p" ]; then dest="$2"; shift; fi',
            "  shift",
            "done",
        ]
        if writes_csv:
            lines.append(
                "printf 'tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\\n'"
                ' > "$dest/twcs.csv"'
            )
        path = os.path.join(self.bin, "kaggle")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def stub_calls(self):
        if not os.path.exists(self.marker):
            return 0
        with open(self.marker) as f:
            return len(f.read().splitlines())

    def env(self, path, **extra):
        env = {"HOME": self.home, "TRIAGE_DATA_DIR": self.data_dir, "PATH": path}
        env.update(extra)
        return env

    def run_script(self, env):
        return subprocess.run([BASH, SCRIPT], env=env, capture_output=True, text=True, timeout=30)

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_missing_cli(self):
        result = self.run_script(self.env(self.empty_bin))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Kaggle CLI", result.stderr)
        self.assertIn("pip install kaggle", result.stderr)

    def test_missing_credentials(self):
        self.write_stub()
        result = self.run_script(self.env(f"{self.bin}:/usr/bin:/bin"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credentials", result.stderr.lower())
        self.assertEqual(self.stub_calls(), 0)

    def test_only_username_is_not_enough(self):
        self.write_stub()
        result = self.run_script(self.env(f"{self.bin}:/usr/bin:/bin", KAGGLE_USERNAME=USER_SENTINEL))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stub_calls(), 0)
        self.assertNotIn(USER_SENTINEL, result.stdout + result.stderr)

    def test_download_and_rerun(self):
        self.write_stub()
        env = self.env(f"{self.bin}:/usr/bin:/bin", KAGGLE_USERNAME=USER_SENTINEL, KAGGLE_KEY=KEY_SENTINEL)
        first = self.run_script(env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, "twcs.csv")))
        self.assertEqual(self.stub_calls(), 1)
        for s in (USER_SENTINEL, KEY_SENTINEL):
            self.assertNotIn(s, first.stdout)
            self.assertNotIn(s, first.stderr)

        second = self.run_script(env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.stub_calls(), 1)
        for s in (USER_SENTINEL, KEY_SENTINEL):
            self.assertNotIn(s, second.stdout + second.stderr)

    def test_kaggle_json_counts_as_credentials(self):
        self.write_stub()
        os.makedirs(os.path.join(self.home, ".kaggle"))
        with open(os.path.join(self.home, ".kaggle", "kaggle.json"), "w") as f:
            f.write('{"username": "%s", "key": "%s"}' % (USER_SENTINEL, KEY_SENTINEL))
        result = self.run_script(self.env(f"{self.bin}:/usr/bin:/bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(KEY_SENTINEL, result.stdout + result.stderr)

    def test_nested_csv_is_moved_up(self):
        path = os.path.join(self.bin, "kaggle")
        with open(path, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f"printf 'called\\n' >> '{self.marker}'\n"
                'dest=""\n'
                'while [ "$#" -gt 0 ]; do if [ "$1" = "-p" ]; then dest="$2"; shift; fi; shift; done\n'
                'mkdir -p "$dest/twcs"\n'
                "printf 'tweet_id\\n' > \"$dest/twcs/twcs.csv\"\n"
            )
        os.chmod(path, 0o755)
        env = self.env(f"{self.bin}:/usr/bin:/bin", KAGGLE_USERNAME="u", KAGGLE_KEY="k")
        result = self.run_script(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, "twcs.csv")))

    def test_archive_without_csv_fails(self):
        self.write_stub(writes_csv=False)
        env = self.env(f"{self.bin}:/usr/bin:/bin", KAGGLE_USERNAME="u", KAGGLE_KEY="k")
        result = self.run_script(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("twcs.csv", result.stderr)

    def test_kaggle_failure_propagates(self):
        path = os.path.join(self.bin, "kaggle")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(path, 0o755)
        env = self.env(f"{self.bin}:/usr/bin:/bin", KAGGLE_USERNAME="u", KAGGLE_KEY="k")
        result = self.run_script(env)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
