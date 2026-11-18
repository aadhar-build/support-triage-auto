"""Factory round handling (shared with software-factory-mvp's tests/test_factory.py)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "factory" / "lib"))
import factory as F  # noqa: E402


class TestEnterBuild(unittest.TestCase):
    """Moving a job into build by hand must always start a fresh round folder."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)

    def test_first_build_from_spec_is_round_one(self):
        job = F.new_job("001-x", 1, "x")
        job.update(stage="spec", round=0)
        F.enter_build(job, self.d, "spec done")
        self.assertEqual((job["stage"], job["round"]), ("build", 1))

    def test_owner_feedback_after_needs_human_starts_a_new_round(self):
        job = F.new_job("001-x", 1, "x")
        job.update(stage="needs-human", round=1)
        (self.d / "round-1").mkdir()
        (self.d / "round-1" / "review-code.md").write_text("VERDICT: PASS\n")
        F.enter_build(job, self.d, "owner feedback")
        self.assertEqual((job["stage"], job["round"]), ("build", 2))
        # the old round's reviews are left exactly as they were
        self.assertEqual((self.d / "round-1" / "review-code.md").read_text(), "VERDICT: PASS\n")
        self.assertFalse((self.d / "round-2").exists())

    def test_owner_rounds_are_not_capped_by_max_rounds(self):
        job = F.new_job("001-x", 1, "x")
        job.update(stage="needs-human", round=5)
        F.enter_build(job, self.d, "owner feedback")
        self.assertEqual(job["round"], 6)

    def test_recovery_and_merging_back_to_build_also_bump(self):
        for stage in ("recovery", "merging"):
            job = F.new_job("001-x", 1, "x")
            job.update(stage=stage, round=2)
            F.enter_build(job, self.d, "again")
            self.assertEqual(job["round"], 3, stage)

    def test_refuses_if_the_new_round_folder_already_exists(self):
        job = F.new_job("001-x", 1, "x")
        job.update(stage="needs-human", round=1)
        (self.d / "round-2").mkdir()
        with self.assertRaises(F.FactoryError):
            F.enter_build(job, self.d, "owner feedback")
        self.assertEqual((job["stage"], job["round"]), ("needs-human", 1))

    def test_illegal_source_stage_is_refused_without_bumping(self):
        job = F.new_job("001-x", 1, "x")
        job.update(stage="intake", round=0)
        with self.assertRaises(F.FactoryError):
            F.enter_build(job, self.d, "x")
        self.assertEqual(job["round"], 0)


if __name__ == "__main__":
    unittest.main()
