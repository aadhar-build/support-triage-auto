"""Check retirement Git evidence with real worktrees and offline GitHub responses."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory" / "lib"))

from factory import FactoryError
from job_links import PublicGitHubResponse, capture_candidate, capture_source


class RetirementGit(unittest.TestCase):
    def setUp(self):
        try:
            from retirement_git import validate, validate_parked, verify_merge
        except ModuleNotFoundError as error:
            if error.name != "retirement_git":
                raise
            self.fail("retirement Git verification is unavailable")
        self.validate = validate
        self.validate_parked = validate_parked
        self.verify_merge = verify_merge
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Retirement Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.repo = "fixture/retirement"
        self.api = f"https://api.github.com/repos/{self.repo}"
        self.web = f"https://github.com/{self.repo}"
        self.repository = {"id": 42, "full_name": self.repo, "url": self.api, "html_url": self.web}
        self.git("remote", "add", "origin", self.web + ".git")
        (self.root / "code.txt").write_text("base\n")
        (self.root / ".gitignore").write_text("private-cache/\n")
        self.git("add", "code.txt", ".gitignore")
        self.git("commit", "-qm", "base")
        self.original_base = self.git("rev-parse", "HEAD")
        self.track_base()
        self.job = {"id": "001-example", "issue": 3, "pr": 4, "branch": "factory/001-example",
                    "worktree": ".worktrees/001-example", "stage": "merged"}
        self.config = {"base_branch": "main"}
        self.worktree = self.root / self.job["worktree"]
        self.folder = self.root / "factory/jobs" / self.job["id"]
        self.folder.mkdir(parents=True)
        self.git("worktree", "add", "-qb", self.job["branch"], str(self.worktree))
        self.admin = Path(self.git("rev-parse", "--absolute-git-dir", cwd=self.worktree))
        self.change(self.worktree, "code.txt", "candidate\n")
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.gh = self.root / "gh"
        self.gh.write_text("""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:7] != ['api', '--hostname', 'github.com', '--method', 'GET', '--jq', args[6]]:
    raise SystemExit(99)
with Path('requests').open('a') as requests:
    requests.write(args[-1] + '\\n')
print('private fixture output', file=sys.stderr)
print(json.dumps(json.loads(Path('responses.json').read_text())[args[-1]]))
""")
        self.gh.chmod(0o755)
        environment = patch.dict(os.environ, PATH=f"{self.root}{os.pathsep}{os.environ['PATH']}")
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, *args, cwd=None):
        command = subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True, text=True)
        return command.stdout.rstrip("\n")

    def change(self, cwd, name, value):
        (cwd / name).write_text(value)
        self.git("add", name, cwd=cwd)
        self.git("commit", "-qm", "update " + name, cwd=cwd)

    def track_base(self):
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))

    def local(self):
        return self.validate(self.root, self.folder, self.job, self.config)

    def put_xattr(self, path, name, *, follow=True):
        if hasattr(os, "setxattr"):
            os.setxattr(path, name, b"private fixture attribute", follow_symlinks=follow)
        else:
            flags = [] if follow else ["-s"]
            subprocess.run(["/usr/bin/xattr", *flags, "-w", name, "private fixture attribute", str(path)],
                           check=True, capture_output=True)

    def clear_xattr(self, path, name):
        if hasattr(os, "removexattr"):
            os.removexattr(path, name)
        else:
            subprocess.run(["/usr/bin/xattr", "-d", name, str(path)], check=True, capture_output=True)

    def generated_cache(self, *, ignored=True, source="pkg/sample.py"):
        if ignored:
            self.change(self.worktree, ".gitignore", "private-cache/\n__pycache__/\n")
        source_path = self.worktree / source
        source_path.parent.mkdir(exist_ok=True)
        self.change(self.worktree, source, 'raise RuntimeError("inspection must not execute source")\n')
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        subprocess.run([sys.executable, "-I", "-m", "py_compile", source], cwd=self.worktree,
                       check=True, capture_output=True)
        return next(source_path.parent.glob(f"__pycache__/{source_path.stem}.*.pyc"))

    def generated(self):
        import retirement_git

        inspect = getattr(retirement_git, "inspect_generated", None)
        self.assertIsNotNone(inspect, "opt-in Git inventory is unavailable")
        return inspect(self.root, self.folder, self.job, self.config)

    def pull(self, merged_sha, **changes):
        return {"id": 9, "number": 4, "url": self.api + "/pulls/4", "html_url": self.web + "/pull/4",
                "state": "closed", "merged": True, "merged_at": "2026-01-01T12:00:00Z",
                "merge_commit_sha": merged_sha,
                "base": {"repo": self.repository, "ref": "main", "sha": self.original_base},
                "head": {"repo": self.repository, "ref": self.job["branch"], "sha": self.candidate},
                **changes}

    def historical(self, pull):
        capture_source(self.root, self.folder, self.job, PublicGitHubResponse({
            "id": 8, "number": 3, "url": self.api + "/issues/3", "html_url": self.web + "/issues/3",
            "repository_url": self.api, "repository": self.repository,
        }, self.repo))
        capture_candidate(self.root, self.job, PublicGitHubResponse(pull, self.repo))
        self.assertIn("candidate_receipt", self.job)

    def remote(self, pull, *, current_base=None):
        (self.root / "responses.json").write_text(json.dumps({
            f"repos/{self.repo}": self.repository,
            f"repos/{self.repo}/pulls/4": {**pull, "body": "private fixture body"},
            f"repos/{self.repo}/git/ref/heads/main": {"ref": "refs/heads/main", "object": {
                "type": "commit", "sha": current_base or self.git("rev-parse", "refs/remotes/origin/main"),
            }},
        }))

    def proof(self):
        return self.verify_merge(self.root, self.job, self.config, self.local())

    def test_canonical_clean_worktree_can_be_inspected_offline(self):
        before = (self.admin / "index").read_bytes()
        local = self.local()
        self.assertEqual(local["candidate_sha"], self.candidate)
        self.assertEqual(local["base_sha"], self.original_base)
        self.assertEqual(local["admin"], str(self.admin))
        self.assertEqual(local["repository"], self.repo)
        self.assertEqual((self.admin / "index").read_bytes(), before)
        self.assertFalse((self.root / "requests").exists())

    def test_stale_backlink_refuses_the_registered_checkout(self):
        (self.admin / "gitdir").write_text(str(self.root / ".git/worktrees/other/.git") + "\n")
        with self.assertRaises(FactoryError):
            self.local()

    def test_retirement_refuses_a_job_folder_alias(self):
        alias = self.root / "alternate-job-path"
        alias.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(FactoryError):
            self.validate(self.root, alias, self.job, self.config)

    def test_parked_checkout_must_keep_the_same_identity_and_clean_contents(self):
        original = self.local()
        parked = self.root / ".git/factory-retirements/fixture.checkout"
        parked.parent.mkdir()
        self.git("worktree", "move", str(self.worktree), str(parked))
        self.validate_parked(self.root, self.folder, self.job, self.config, original, parked)
        (parked / "private-cache").mkdir()
        with self.assertRaises(FactoryError):
            self.validate_parked(self.root, self.folder, self.job, self.config, original, parked)

    def test_extended_attributes_block_removal_of_clean_tracked_and_admin_paths(self):
        (self.worktree / "nested").mkdir()
        self.change(self.worktree, "nested/file.txt", "nested clean content")
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        paths = (self.worktree, self.worktree / "code.txt", self.worktree / "nested", self.worktree / ".git",
                 self.admin, self.admin / "index", self.admin / "logs", self.admin / "logs/HEAD")
        for path in paths:
            with self.subTest(path=path):
                name = "user.retirement.private"
                self.put_xattr(path, name)
                try:
                    with self.assertRaises(FactoryError) as refused:
                        self.local()
                    self.assertNotIn(name, str(refused.exception))
                    self.assertNotIn("private fixture attribute", str(refused.exception))
                finally:
                    self.clear_xattr(path, name)

    def test_symlink_metadata_is_read_without_following_the_target(self):
        external = self.root / "external.txt"
        external.write_text("outside checkout")
        self.put_xattr(external, "user.retirement.unrelated")
        link = self.worktree / "link"
        link.symlink_to(external)
        self.git("add", "link", cwd=self.worktree)
        self.git("commit", "-qm", "Add tracked link", cwd=self.worktree)
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.assertEqual(self.local()["candidate_sha"], self.candidate)
        # Linux does not allow user.* on symlinks. Darwin supports them and
        # must refuse even when the target itself has no effect on validation.
        if sys.platform == "darwin":
            self.put_xattr(link, "com.apple.quarantine", follow=False)
            with self.assertRaises(FactoryError):
                self.local()

    def test_a_parked_checkout_with_new_extended_attributes_is_refused(self):
        original = self.local()
        parked = self.root / ".git/factory-retirements/fixture.checkout"
        parked.parent.mkdir()
        self.git("worktree", "move", str(self.worktree), str(parked))
        self.put_xattr(parked / "code.txt", "user.retirement.after-parking")
        with self.assertRaises(FactoryError):
            self.validate_parked(self.root, self.folder, self.job, self.config, original, parked)

    def test_opt_in_inventory_binds_cache_and_bytes_while_default_remains_strict(self):
        cache = self.generated_cache()
        baseline = cache.read_bytes()
        with self.assertRaises(FactoryError):
            self.local()
        local, manifest = self.generated()
        self.assertEqual(local["candidate_sha"], self.candidate)
        self.assertEqual([entry["kind"] for entry in manifest["entries"]], ["directory", "file"])
        self.assertEqual([entry["path"] for entry in manifest["entries"]],
                         ["pkg/__pycache__", cache.relative_to(self.worktree).as_posix()])
        self.assertEqual(manifest["entries"][1]["size"], len(baseline))
        self.assertEqual(manifest["entries"][1]["sha256"], hashlib.sha256(baseline).hexdigest())
        self.assertEqual(self.generated(), (local, manifest))
        self.assertEqual(cache.read_bytes(), baseline)

    def test_opt_in_refuses_a_real_cache_without_an_ignore_rule(self):
        self.generated_cache(ignored=False)
        with self.assertRaises(FactoryError):
            self.generated()

    def test_opt_in_allows_a_canonical_zero_inventory_for_a_clean_checkout(self):
        from retirement_git import check_generated_manifest

        local, manifest = self.generated()
        self.assertEqual(local, self.local())
        self.assertEqual(manifest["entries"], [])
        check_generated_manifest(manifest, self.job["id"], self.candidate)

    def test_opt_in_refuses_other_ignored_contents_or_an_empty_cache(self):
        cache = self.generated_cache()
        other = self.worktree / "private-cache"
        other.mkdir()
        (other / "surrogate.pyc").write_bytes(cache.read_bytes())
        with self.assertRaises(FactoryError):
            self.generated()
        (other / "surrogate.pyc").unlink()
        other.rmdir()
        cache.unlink()
        with self.assertRaises(FactoryError):
            self.generated()

    def test_opt_in_refuses_hardlinks_unsafe_permissions_or_cache_xattrs(self):
        cache = self.generated_cache()
        other = self.root / "other-reference"
        os.link(cache, other)
        with self.assertRaises(FactoryError):
            self.generated()
        other.unlink()
        cache.chmod(0o664)
        with self.assertRaises(FactoryError):
            self.generated()
        cache.chmod(0)
        with self.assertRaises(FactoryError):
            self.generated()
        cache.chmod(0o644)
        self.put_xattr(cache.parent, "user.retirement.unexpected")
        with self.assertRaises(FactoryError):
            self.generated()

    def test_opt_in_refuses_tracked_content_or_git_inventory_over_budget(self):
        self.generated_cache()
        for limit in ("_MAX_TRACKED_BYTES", "_MAX_TRACKED_LIST_BYTES", "_MAX_TRACKED_ENTRIES"):
            with self.subTest(limit=limit), patch(f"retirement_git.{limit}", 1), self.assertRaises(FactoryError):
                self.generated()

    def test_parked_checkout_requires_the_same_approved_cache_identities(self):
        cache = self.generated_cache()
        local, manifest = self.generated()
        parked = self.root / ".git/factory-retirements/fixture.checkout"
        parked.parent.mkdir()
        self.git("worktree", "move", str(self.worktree), str(parked))
        self.validate_parked(self.root, self.folder, self.job, self.config, local, parked,
                             expected_generated=manifest)
        parked_cache = parked / cache.relative_to(self.worktree)
        identical = parked_cache.read_bytes()
        parked_cache.unlink()
        parked_cache.write_bytes(identical)
        with self.assertRaises(FactoryError):
            self.validate_parked(self.root, self.folder, self.job, self.config, local, parked,
                                 expected_generated=manifest)

    def test_inventory_rechecks_a_cache_changed_while_another_cache_is_compiled(self):
        from retirement_bytecode import inspect_bytecode

        self.generated_cache()
        subprocess.run([sys.executable, "-I", "-O", "-m", "py_compile", "pkg/sample.py"], cwd=self.worktree,
                       check=True, capture_output=True)
        verified = []

        def change_previous_cache(directory, name, *args):
            if verified:
                flags = os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW
                with os.fdopen(os.open(verified[0], flags, dir_fd=directory), "wb") as first:
                    first.write(b"unexpected bytes")
            result = inspect_bytecode(directory, name, *args)
            verified.append(name)
            return result

        with patch("retirement_bytecode.inspect_bytecode", side_effect=change_previous_cache):
            with self.assertRaises(FactoryError):
                self.generated()
        self.assertEqual(len(verified), 2)

    def test_durable_cache_manifest_rejects_invalid_paths_identities_and_digest(self):
        self.generated_cache()
        _, manifest = self.generated()
        from retirement_git import check_generated_manifest

        check_generated_manifest(manifest, self.job["id"], self.candidate)
        cases = (
            {**manifest, "digest": "0" * 64},
            {**manifest, "entries": list(reversed(manifest["entries"]))},
            {**manifest, "entries": [manifest["entries"][0], {**manifest["entries"][1], "path": "../outside"}]},
            {**manifest, "entries": [manifest["entries"][0], {**manifest["entries"][1], "identity": [True] * 9}]},
        )
        for index, tampered in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(FactoryError):
                if index:
                    unsigned = {key: value for key, value in tampered.items() if key != "digest"}
                    raw = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
                    tampered["digest"] = hashlib.sha256(raw).hexdigest()
                check_generated_manifest(tampered, self.job["id"], self.candidate)

    def test_unavailable_extended_attribute_inventory_is_refused_without_private_errors(self):
        with patch("retirement_git._attribute_names", side_effect=OSError("private provider error")):
            with self.assertRaisesRegex(FactoryError, "attribute inventory is unavailable") as refused:
                self.local()
        self.assertNotIn("private provider error", str(refused.exception))

    def test_filter_cannot_hide_modified_tracked_bytes_or_execute(self):
        self.git("config", "filter.hide.clean", "touch ../filter-executed; printf 'candidate\\n'")
        (self.root / ".git/info/attributes").write_text("code.txt filter=hide\n")
        (self.worktree / "code.txt").write_text("private local modification")
        with self.assertRaises(FactoryError) as refused:
            self.local()
        self.assertFalse((self.worktree.parent / "filter-executed").exists())
        self.assertNotIn("private local modification", str(refused.exception))

    def test_applicable_external_filter_refuses_even_unchanged_tracked_bytes(self):
        self.git("config", "filter.inspect.process", "touch ../filter-executed")
        (self.root / ".git/info/attributes").write_text("code.txt filter=inspect\n")
        with self.assertRaises(FactoryError):
            self.local()
        self.assertFalse((self.worktree.parent / "filter-executed").exists())

    def test_unrelated_configured_filter_does_not_block_retirement(self):
        self.git("config", "filter.inspect.process", "touch ../filter-executed")
        self.assertEqual(self.local()["candidate_sha"], self.candidate)

    def test_unknown_git_administration_data_makes_worktree_ineligible(self):
        (self.admin / "private.local").write_text("unretained admin artifact")
        with self.assertRaises(FactoryError) as refused:
            self.local()
        self.assertNotIn("private.local", str(refused.exception))

    def test_linux_same_device_mounts_inside_checkout_or_admin_make_retirement_ineligible(self):
        mountinfo = self.root / "mountinfo"
        root_mount = b"20 10 8:2 / / rw - ext4 /dev/fixture rw\n"
        for mount in (self.worktree / "path with spaces", self.admin / "logs", self.worktree):
            escaped = os.fsencode(mount).replace(b" ", b"\\040")
            mountinfo.write_bytes(root_mount + b"31 20 8:2 /other " + escaped + b" rw - ext4 /dev/fixture rw\n")
            with (self.subTest(mount=mount), patch("retirement_git.sys.platform", "linux"),
                  patch("retirement_git._MOUNTINFO", mountinfo), self.assertRaises(FactoryError)):
                self.local()
        mountinfo.write_bytes(root_mount)
        with patch("retirement_git.sys.platform", "linux"), patch("retirement_git._MOUNTINFO", mountinfo):
            self.assertEqual(self.local()["candidate_sha"], self.candidate)
            mountinfo.unlink()
            with self.assertRaises(FactoryError):
                self.local()

    def test_checkout_reflog_cannot_be_the_last_reference_to_a_commit(self):
        self.git("switch", "--detach", "-q", cwd=self.worktree)
        self.git("commit", "--allow-empty", "-qm", "candidate detached work", cwd=self.worktree)
        orphan = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.git("switch", "-q", self.job["branch"], cwd=self.worktree)
        with self.assertRaises(FactoryError):
            self.local()
        self.git("branch", "retained-detached-work", orphan)
        self.assertEqual(self.local()["candidate_sha"], self.candidate)

    def test_index_flags_and_ignored_files_make_the_checkout_ineligible(self):
        for flag in ("skip-worktree", "assume-unchanged"):
            with self.subTest(flag=flag):
                self.git("update-index", "--" + flag, "code.txt", cwd=self.worktree)
                with self.assertRaises(FactoryError):
                    self.local()
                self.git("update-index", "--no-" + flag, "code.txt", cwd=self.worktree)
        cache = self.worktree / "private-cache"
        cache.mkdir()
        (cache / "private.txt").write_text("private fixture cache")
        with self.assertRaises(FactoryError) as refused:
            self.local()
        self.assertNotIn("private.txt", str(refused.exception))

    def test_git_worktree_and_index_locks_make_the_checkout_ineligible(self):
        self.git("worktree", "lock", str(self.worktree))
        with self.assertRaises(FactoryError):
            self.local()
        self.git("worktree", "unlock", str(self.worktree))
        (self.admin / "index.lock").touch()
        with self.assertRaises(FactoryError):
            self.local()
        (self.admin / "index.lock").unlink()
        (self.root / ".git/index.lock").touch()
        with self.assertRaises(FactoryError):
            self.local()

    def test_git_environment_override_cannot_select_another_index_or_repository(self):
        with patch.dict(os.environ, GIT_DIR="/invalid/directory", GIT_INDEX_FILE="/dev/null"):
            self.assertEqual(self.local()["candidate_sha"], self.candidate)

    def test_same_pr_head_and_exact_squash_delta_with_historical_receipts(self):
        self.change(self.root, "unrelated.txt", "separate base change\n")
        self.git("merge", "--squash", self.job["branch"])
        self.git("commit", "-qm", "squash")
        merge = self.git("rev-parse", "HEAD")
        self.change(self.root, "later.txt", "later base change\n")
        self.track_base()
        pull = self.pull(merge)
        self.historical(pull)
        self.remote(pull)
        proof = self.proof()
        self.assertEqual(proof["merge_commit_sha"], merge)
        self.assertEqual(proof["candidate_sha"], self.candidate)
        self.assertEqual(proof["job_pr_association"], "verified")
        self.assertEqual(proof["candidate_merge_identity"], "content_equivalent")
        self.assertEqual(proof["repository_id"], 42)
        self.assertNotIn("private", json.dumps(proof))
        self.assertEqual((self.root / "requests").read_text().splitlines(),
                         [f"repos/{self.repo}", f"repos/{self.repo}/pulls/4", f"repos/{self.repo}/git/ref/heads/main"])

    def test_exact_linear_rebase_delta_supports_explicit_legacy_association(self):
        self.change(self.worktree, "extra.txt", "extra candidate change\n")
        self.candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.change(self.root, "unrelated.txt", "separate base change\n")
        self.git("cherry-pick", f"{self.original_base}..{self.candidate}")
        self.track_base()
        self.remote(self.pull(self.git("rev-parse", "HEAD")))
        proof = self.proof()
        self.assertEqual(proof["job_pr_association"], "local_only")
        self.assertEqual(proof["candidate_merge_identity"], "content_equivalent")

    def test_merge_parent_preserves_the_exact_candidate(self):
        self.change(self.root, "unrelated.txt", "separate base change\n")
        self.git("merge", "--no-ff", self.job["branch"])
        self.track_base()
        merge = self.git("rev-parse", "HEAD")
        self.remote(self.pull(merge))
        proof = self.proof()
        self.assertEqual(proof["merge_commit_sha"], merge)
        self.assertEqual(proof["candidate_merge_identity"], "exact")

    def test_merged_api_status_does_not_allow_unrelated_local_delta(self):
        self.change(self.root, "unrelated.txt", "unrelated merged result\n")
        self.track_base()
        self.remote(self.pull(self.git("rev-parse", "HEAD")))
        with self.assertRaises(FactoryError):
            self.proof()

    def test_stale_remote_tracking_base_cannot_certify_live_base(self):
        self.git("merge", "--ff-only", self.job["branch"])
        self.track_base()
        self.remote(self.pull(self.candidate), current_base=self.original_base)
        with self.assertRaises(FactoryError):
            self.proof()

    def test_merge_object_on_a_side_branch_does_not_prove_base_integration(self):
        self.git("switch", "-qc", "side")
        self.git("merge", "--no-ff", self.job["branch"])
        merge = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        self.change(self.root, "unrelated.txt", "separate base change\n")
        self.git("merge", "--no-ff", "side")
        self.track_base()
        self.remote(self.pull(merge))
        with self.assertRaises(FactoryError):
            self.proof()

    def test_fresh_pr_must_agree_with_historical_id_and_both_repository_ids(self):
        self.git("merge", "--ff-only", self.job["branch"])
        self.track_base()
        pull = self.pull(self.candidate)
        self.historical(pull)
        for changes in ({"id": 129}, {"head": {**pull["head"], "repo": {**self.repository, "id": 100}}}):
            with self.subTest(changes=changes):
                self.remote({**pull, **changes})
                with self.assertRaises(FactoryError):
                    self.proof()

    def test_invalid_receipt_does_not_become_legacy_association(self):
        self.git("merge", "--ff-only", self.job["branch"])
        self.track_base()
        self.remote(self.pull(self.candidate))
        self.job["source_receipt"] = "f" * 64
        with self.assertRaises(FactoryError):
            self.proof()


if __name__ == "__main__":
    unittest.main()
