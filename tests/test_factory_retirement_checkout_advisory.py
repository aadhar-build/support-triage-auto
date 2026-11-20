"""Exercise the bounded checkout advisory with real disposable Git worktrees."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))


class CheckoutAdvisory(unittest.TestCase):
    def setUp(self):
        try:
            import retirement_checkout_advisory as advisory
        except ModuleNotFoundError as error:
            if error.name != "retirement_checkout_advisory":
                raise
            self.fail("checkout advisory is unavailable")
        self.advisory = advisory
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Checkout Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for path, value in {
            ".gitignore": "owner-private-ignored\n",
            "file.txt": "fixture\n",
            "backend/dist/tracked.txt": "source fixture\n",
            "web/source/deeper/tracked.txt": "source fixture\n",
        }.items():
            full = self.root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(value)
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture")
        self.job = {"id": "007-example", "branch": "factory/007-example", "worktree": ".worktrees/007-example"}
        self.worktree = self.root / self.job["worktree"]
        self.git("worktree", "add", "-qb", self.job["branch"], str(self.worktree))
        self.candidate = self.git("rev-parse", "HEAD")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def observe(self, candidate=None):
        return self.advisory.observe_checkout(self.root, self.job, candidate or self.candidate)

    def test_reports_artifact_roots_and_unknown_entries_without_descending_or_disclosing_names(self):
        paths = ("dist", "web/build", "backend/node_modules", "__pycache__", "backend/build",
                 "secret-untracked-island/node_modules", "secret-untracked-island/__pycache__")
        for path in paths:
            (self.worktree / path).mkdir(parents=True)
        (self.worktree / "owner-private-ignored").write_text("private-marker")
        (self.worktree / "owner-private-untracked").write_text("private-marker")
        before = self.git("rev-parse", "--absolute-git-dir", cwd=self.worktree)
        index = Path(before) / "index"
        index_before = index.stat().st_mtime_ns, index.read_bytes()
        result = self.observe()
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["observed"], {"node_modules_roots": 1, "build_output_roots": 2,
                                             "python_cache_roots": 1, "unclassified_roots_or_entries": 4,
                                             "tracked_shape_mismatches": 0})
        self.assertEqual(result["limiting_conditions"], [])
        self.assertTrue(result["counts_are_lower_bounds"])
        output = json.dumps(result)
        for marker in ("owner-private", "secret-untracked-island", "private-marker", str(self.root), "eligibility"):
            self.assertNotIn(marker, output)
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), index_before)
        self.assertFalse((self.root / ".git/factory-retirements").exists())

    def test_does_not_follow_a_tracked_directory_replaced_by_a_symlink(self):
        outside = self.root / "outside"
        (outside / "node_modules").mkdir(parents=True)
        shutil.rmtree(self.worktree / "backend")
        (self.worktree / "backend").symlink_to(outside)
        result = self.observe()
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["observed"]["node_modules_roots"], 0)
        self.assertEqual(result["observed"]["tracked_shape_mismatches"], 1)
        self.assertIn("tracked_shape_mismatch", result["limiting_conditions"])

    def test_untracked_fifo_and_artifact_named_symlink_are_unknown_entries(self):
        outside = self.root / "external-target"
        outside.mkdir()
        (self.worktree / "node_modules").symlink_to(outside)
        os.mkfifo(self.worktree / "private-fifo")
        result = self.observe()
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["observed"]["node_modules_roots"], 0)
        self.assertEqual(result["observed"]["unclassified_roots_or_entries"], 2)

    def test_checkout_alias_is_unresolved(self):
        outside = self.root / "outside"
        (outside / "node_modules").mkdir(parents=True)
        self.worktree.rename(self.worktree.with_name("preserved"))
        self.worktree.symlink_to(outside)
        result = self.observe()
        self.assertEqual(result["coverage"], "unresolved")
        self.assertEqual(result["inspected"], {"directories": 0, "entries": 0})
        self.assertEqual(sum(result["observed"].values()), 0)

    def test_stale_candidate_or_staged_index_does_not_certify_coverage(self):
        self.git("commit", "--allow-empty", "-qm", "Later candidate", cwd=self.worktree)
        result = self.observe()
        self.assertEqual(result["coverage"], "unresolved")
        self.assertIn("candidate_changed", result["limiting_conditions"])
        candidate = self.git("rev-parse", "HEAD", cwd=self.worktree)
        (self.worktree / "new-staged.txt").touch()
        self.git("add", "new-staged.txt", cwd=self.worktree)
        result = self.observe(candidate)
        self.assertEqual(result["coverage"], "unresolved")
        self.assertIn("git_inventory_unavailable", result["limiting_conditions"])

    def test_scan_limits_mark_lower_bounds_partial_or_unresolved(self):
        for constants, value, code in (("_MAX_ENTRIES", 1, "entry_limit"),
                                       ("_MAX_DIRECTORIES", 1, "directory_limit"),
                                       ("_MAX_DEPTH", 0, "depth_limit")):
            with self.subTest(limit=constants), patch.object(self.advisory, constants, value):
                result = self.observe()
                self.assertEqual(result["coverage"], "partial")
                self.assertIn(code, result["limiting_conditions"])
        with patch.object(self.advisory, "_MAX_SECONDS", 0):
            result = self.observe()
            self.assertEqual(result["coverage"], "unresolved")
            self.assertIn("deadline", result["limiting_conditions"])
        with patch.object(self.advisory.Git, "_MAX_TRACKED_LIST_BYTES", 1):
            result = self.observe()
            self.assertEqual(result["coverage"], "unresolved")
            self.assertIn("git_inventory_unavailable", result["limiting_conditions"])

    def test_checkout_advisory_detects_index_change_during_traversal(self):
        (self.worktree / "private-index-pending").touch()
        original_scan = self.advisory._scan_checkout

        def stage_existing_entry(*args):
            original_scan(*args)
            self.git("add", "private-index-pending", cwd=self.worktree)

        with patch.object(self.advisory, "_scan_checkout", side_effect=stage_existing_entry):
            result = self.observe()
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("git_inventory_changed", result["limiting_conditions"])
        self.assertNotIn("private-index-pending", json.dumps(result))

    def test_an_os_error_midway_is_partial_and_does_not_expose_diagnostics(self):
        real_scandir = os.scandir

        def deny_one_tracked_directory(fd):
            if os.fstat(fd).st_ino == (self.worktree / "backend").stat().st_ino:
                raise PermissionError("secret syscall path")
            return real_scandir(fd)

        with patch.object(self.advisory.os, "scandir", side_effect=deny_one_tracked_directory):
            result = self.observe()
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("directory_unavailable", result["limiting_conditions"])
        self.assertNotIn("secret syscall path", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
