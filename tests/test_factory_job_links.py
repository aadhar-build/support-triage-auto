"""Exercise link capture at admission and PR publication with an offline GitHub fixture."""
import json
import os
import shutil
import unittest

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID


class JobLinks(FactoryCLI, unittest.TestCase):
    repo = "fixture/software-factory"
    api = "https://api.github.com/repos/" + repo
    web = "https://github.com/" + repo

    def setUp(self):
        super().setUp()
        self.repository = {"id": 56, "full_name": self.repo, "url": self.api, "html_url": self.web}
        self.issue = {"id": 77, "number": 1, "url": self.api + "/issues/1", "repository_url": self.api,
                      "repository": self.repository, "html_url": self.web + "/issues/1", "user": {"login": "fixture"},
                      "labels": [{"name": "factory"}], "state": "open", "title": "Example", "body": "private body"}
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                 "-qm", "candidate")
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("worktree", "add", "-qb", "factory/" + JOB_ID, ".worktrees/" + JOB_ID, self.sha)
        self.pr = {"id": 120, "number": 9, "url": self.api + "/pulls/9", "html_url": self.web + "/pull/9",
                   "base": {"repo": self.repository}, "head": {"repo": self.repository, "ref": "factory/" + JOB_ID,
                                                               "sha": self.sha}}
        stub = self.root / "stub"
        stub.mkdir()
        gh = stub / "gh"
        gh.write_text("""#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
with Path('gh-calls').open('a') as f:
    f.write(json.dumps(a) + '\\n')
if 'user' in a: print('fixture')
elif 'repos/fixture/software-factory' in a: print(os.environ['REPOSITORY'])
elif 'repos/fixture/software-factory/issues/1' in a:
    issue = json.loads(os.environ['ISSUE'])
    if '--jq' in a:
        action = os.environ.get('VERIFY_FIXTURE')
        if action == 'repoint':
            subprocess.run(['git','remote','set-url','origin','https://github.com/fixture/repointed'],check=True)
        if action == 'replace':
            folder = Path('factory/jobs/001-example')
            shutil.copytree(folder, str(folder) + '.replacement')
            folder.rename(str(folder) + '.old')
            Path(str(folder) + '.replacement').rename(folder)
        issue['pull_request'] = issue.get('pull_request') is not None
        if action == 'missing-type':
            del issue['pull_request']
    print(json.dumps(issue))
elif 'repos/fixture/software-factory/pulls/9' in a: print(os.environ['PULL'])
elif 'repos/fixture/software-factory/pulls' in a: print(os.environ['PULL'])
elif any('comments?' in s for s in a): print('[]')
elif 'repos/fixture/software-factory/issues/9/labels' in a: print('[]')
else: sys.exit(99)
""")
        gh.chmod(0o755)
        git = stub / "git"
        git.write_text(f"#!/bin/sh\n[ \"$1\" = push ] && exit 0\nexec '{shutil.which('git')}' \"$@\"\n")
        git.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "GH_HOST": "github.com"}

    def command(self, *args, success=True):
        return self.cli("job", *args, env={**self.env, "ISSUE": json.dumps(self.issue), "PULL": json.dumps(self.pr),
                                            "REPOSITORY": json.dumps(self.repository)},
                        success=success)

    def create(self):
        self.assertEqual(self.command("create", "1").stdout.strip(), "created " + JOB_ID)

    def weekly(self, *flags):
        env = {**self.env, "ISSUE": json.dumps(self.issue), "PULL": json.dumps(self.pr),
               "REPOSITORY": json.dumps(self.repository)}
        result = self.cli("scorecard", "--weekly", "2026-11-19", "--timezone", "UTC", "--as-of",
                          "2099-11-27T10:00:00Z", "--json", *flags, env=env)
        return json.loads(result.stdout)["links"][JOB_ID]

    def card(self):
        return self.command("card", JOB_ID).stdout

    def test_creation_and_publication_bind_card_links_without_extra_requests(self):
        self.create()
        state = self.state.read_bytes()
        issue_link = f"[GitHub issue]({self.web}/issues/1)"
        self.assertNotIn(issue_link, self.card())
        self.assertNotIn("/commit/", self.card())
        self.assertIsNone(self.weekly())
        calls = [json.loads(line) for line in (self.root / "gh-calls").read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][calls[0].index("--hostname") + 1], "github.com")
        verify = lambda: self.command("card", JOB_ID, "--verify-github-links").stdout
        self.assertIn(issue_link, verify())
        self.assertEqual(self.weekly("--verify-github-links"), self.web + "/issues/1")
        self.assertEqual(self.command("create", "1").stdout.strip(), "exists " + JOB_ID)
        self.assertEqual(self.state.read_bytes(), state)
        self.command("pr", JOB_ID)
        link = f"({self.web}/commit/{self.sha})"
        published_state = self.state.read_bytes()
        self.assertNotIn(link, self.card())
        self.assertIn(link, verify())
        self.assertEqual(self.state.read_bytes(), published_state)
        original_job = json.loads(published_state)
        self.state.write_text(json.dumps({**original_job, "pr": 10}))
        self.assertNotIn("/commit/", verify())
        self.state.write_bytes(published_state)
        self.git("remote", "set-url", "origin", "https://github.com/fixture/replacement.git")
        self.assertNotIn("/commit/", verify())
        self.assertNotIn(issue_link, verify())
        self.git("remote", "set-url", "origin", self.web + ".git")
        self.git("-C", ".worktrees/" + JOB_ID, "-c", "user.name=Fixture", "-c", "user.email=x@example.invalid",
                 "commit", "--allow-empty", "-qm", "unpushed")
        self.assertNotIn("/commit/", verify())

    def test_spoofed_issue_response_and_stored_job_urls_have_no_link_authority(self):
        for field, value in [("id", True), ("number", 2), ("repository_url", self.api + "-fake"),
                             ("html_url", self.web + "/issues/1?secret"), ("url", self.api + "/issues/2"),
                             ("repository", {**self.repository, "full_name": "other/repo"})]:
            with self.subTest(field=field):
                original = self.issue[field]
                self.issue[field] = value
                self.create()
                self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)
                self.assertNotIn("secret", self.card())
                shutil.rmtree(self.job_dir)
                self.issue[field] = original
        self.write_job(source_url=self.web + "/issues/1", candidate_url=self.web + "/commit/" + self.sha,
                       source_receipt="f" * 32)
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)

    def test_publication_requires_same_database_identity_and_job_head(self):
        for field, value in [("head", {**self.pr["head"], "repo": {**self.repository, "id": 57}}),
                             ("base", {"repo": {**self.repository, "full_name": "fork/other"}}),
                             ("head", {**self.pr["head"], "ref": "another-job"}),
                             ("head", {**self.pr["head"], "sha": "a" * 40}),
                             ("url", self.api + "/pulls/10"), ("html_url", self.web + "/pull/9#secret")]:
            with self.subTest(field=field, value=value):
                self.create()
                original = self.pr[field]
                self.pr[field] = value
                self.command("pr", JOB_ID)
                self.assertNotIn("/commit/", self.command("card", JOB_ID, "--verify-github-links").stdout)
                shutil.rmtree(self.job_dir)
                self.pr[field] = original
    def test_normal_issue_without_nested_repository_captures_identity_with_one_read(self):
        del self.issue["repository"]
        self.create()
        calls = [json.loads(line) for line in (self.root / "gh-calls").read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        lookup = calls[-1]
        self.assertIn("repos/" + self.repo, lookup)
        self.assertEqual(lookup[lookup.index("--hostname") + 1], "github.com")
        self.assertEqual(lookup[lookup.index("--method") + 1], "GET")
        self.command("pr", JOB_ID)
        out = self.command("card", JOB_ID, "--verify-github-links").stdout
        self.assertIn(f"[GitHub issue]({self.web}/issues/1)", out)
        self.assertIn(f"({self.web}/commit/{self.sha})", out)
        shutil.rmtree(self.job_dir)
        self.repository["html_url"] += "?private"
        self.create()
        self.assertNotIn("source_receipt", json.loads(self.state.read_bytes()))
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)

    def test_missing_incomplete_and_misdirected_receipts_leave_plain_links(self):
        self.create()
        store = self.root / ".git" / "factory-link-receipts"
        source = store / json.loads(self.state.read_bytes())["source_receipt"]
        contents = source.read_bytes()
        self.assertNotIn(b"private body", contents)
        self.assertNotIn(b"private body", self.state.read_bytes())
        source.chmod(0o600)
        source.write_bytes(contents[:-1])
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)
        source.unlink()
        other = self.root / "other"
        other.write_bytes(contents)
        source.symlink_to(other)
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)
        shutil.rmtree(self.job_dir)
        shutil.rmtree(store)
        store.symlink_to(self.root, target_is_directory=True)
        self.create()
        self.assertNotIn("source_receipt", json.loads(self.state.read_bytes()))
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)

    def test_custom_host_and_admission_refusal_do_not_issue_receipts(self):
        self.env["GH_HOST"] = "custom.example.invalid"
        self.create()
        self.assertIn("Tracker link unavailable", self.command("card", JOB_ID, "--verify-github-links").stdout)
        self.assertFalse((self.root / ".git" / "factory-link-receipts").exists())
        shutil.rmtree(self.job_dir)
        self.env["GH_HOST"] = "github.com"
        self.issue["labels"] = []
        result = self.command("create", "1", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root / ".git" / "factory-link-receipts").exists())

    def test_opt_in_rejects_recreation_and_replayed_job_state(self):
        self.create()
        self.command("pr", JOB_ID)
        old = self.state.read_bytes()
        def checked():
            return self.command("card", JOB_ID, "--verify-github-links").stdout
        self.pr["id"] += 1
        self.assertNotIn("/commit/", checked())
        self.pr["id"] -= 1
        self.issue["id"] += 1
        self.assertNotIn("](https://github.com/", checked())
        self.issue["id"] -= 1
        self.repository["id"] += 1
        self.assertNotIn("](https://github.com/", checked())
        self.repository["id"] -= 1
        shutil.rmtree(self.job_dir)
        self.create()
        self.state.write_bytes(old)
        self.assertNotIn("](https://github.com/", checked())

    def test_changed_origin_replaced_admission_or_missing_type_during_verification(self):
        self.create()
        for action in ("missing-type", "repoint", "replace"):
            with self.subTest(action=action):
                self.git("remote", "set-url", "origin", self.web + ".git")
                self.env["VERIFY_FIXTURE"] = action
                out = self.command("card", JOB_ID, "--verify-github-links").stdout
                self.assertIn("Tracker link unavailable", out)

    def test_later_published_head_must_match_original_pull_request_identity(self):
        self.create()
        self.command("pr", JOB_ID)
        wt = ".worktrees/" + JOB_ID
        self.git("-C", wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "next round")
        current = self.git("-C", wt, "rev-parse", "HEAD").stdout.strip()
        def checked():
            return self.command("card", JOB_ID, "--verify-github-links").stdout
        self.assertNotIn("/commit/", checked())
        self.pr["head"]["sha"] = current
        self.assertIn(f"({self.web}/commit/{current})", checked())
        self.pr["id"] += 1
        self.assertNotIn("/commit/", checked())
