"""Read two gate headers without turning their presence into a retirement decision."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
from retirement_evidence_advisory import observe_evidence


class EvidenceAdvisoryTests(unittest.TestCase):
    candidate = "a" * 40

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "factory/jobs/001-example"
        self.round = self.folder / "round-2"
        self.round.mkdir(parents=True)
        self.job = {"id": "001-example", "round": 2}
        self.checks = self.round / "checks.md"
        self.final = self.folder / "final-checks.md"

    def observe(self):
        return observe_evidence(self.folder, self.job, self.candidate)

    def header(self, sha=None, verdict="PASS"):
        return f"GATE: {verdict}\nCANDIDATE: {sha or self.candidate}\nBASE: {'b'*40}\nSPEC-DIGEST: {'c'*64}\n\n".encode()

    def test_counts_headers_without_confirming_retention_or_echoing_private_material(self):
        self.checks.write_bytes(self.header(verdict="FAIL") + b"private first")
        self.final.write_bytes(self.header("d"*40) + b"private second")
        result = self.observe()
        self.assertEqual(result["slots"], {"round_gate": "candidate_match", "final_gate": "candidate_mismatch"})
        self.assertEqual((result["native_headers"], result["candidate_matched_headers"],
                          result["candidate_mismatched_headers"]), (2, 1, 1))
        self.assertIsNone(result["confirmed_retention_receipts"])
        self.assertEqual(result["retention_assessment"], "full_verification_required")
        data = json.dumps(result)
        for private in ("private", self.job["id"], str(self.folder), "b"*40, "d"*40, self.candidate, "PASS", "FAIL"):
            self.assertNotIn(private, data)

    def test_only_two_expected_slots_are_read_and_missing_is_distinct_from_legacy(self):
        (self.folder / "round-1").mkdir()
        (self.folder / "round-1/checks.md").write_bytes(self.header())
        (self.round / "review-code.md").write_bytes(self.header())
        self.assertEqual(self.observe()["slots"], {"round_gate": "missing", "final_gate": "missing"})
        self.checks.write_text("GATE: PASS\n\n## Summary\nlegacy note")
        self.final.write_text("private arbitrary note")
        result = self.observe()
        self.assertEqual(result["slots"], {"round_gate": "non_native", "final_gate": "non_native"})
        self.assertEqual(result["unresolved_slots"], 0)

    def test_bounded_prefix_and_oversized_receipt(self):
        self.checks.write_bytes(self.header() + b"X" * (512 * 1024))
        with self.final.open("wb") as stream:
            stream.truncate(4*1024*1024 + 1)
        reads = []
        original = os.read
        def count(fd, size):
            reads.append(size)
            return original(fd, size)
        with patch("retirement_evidence_advisory.os.read", side_effect=count):
            result = self.observe()
        self.assertEqual(result["slots"], {"round_gate": "candidate_match", "final_gate": "oversized"})
        self.assertTrue(reads)
        self.assertLessEqual(sum(reads), 512)
        self.assertEqual(result["unresolved_slots"], 1)

    def test_links_special_files_and_missing_job_never_supply_evidence(self):
        elsewhere = self.folder.parent / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "checks.md").write_bytes(self.header())
        self.round.rmdir()
        self.round.symlink_to(elsewhere, target_is_directory=True)
        self.final.symlink_to(elsewhere / "checks.md")
        self.assertEqual(self.observe()["slots"], {"round_gate": "unavailable", "final_gate": "unavailable"})
        self.final.unlink()
        os.mkfifo(self.final)
        self.assertEqual(self.observe()["slots"]["final_gate"], "unavailable")
        wrong = observe_evidence(self.folder, {**self.job, "id": "002-other"}, self.candidate)
        self.assertEqual(wrong["unresolved_slots"], 2)

    def test_unknown_candidate_round_and_malformed_or_duplicate_bindings(self):
        self.final.write_bytes(self.header() + b"ignored")
        r = observe_evidence(self.folder, {**self.job, "round": True}, None)
        self.assertEqual(r["slots"], {"round_gate": "round_unknown", "final_gate": "candidate_unknown"})
        self.assertEqual((r["native_headers"], r["unresolved_slots"]), (1, 2))
        self.checks.write_bytes(self.header().replace(b"BASE:", b"CANDIDATE:"))
        self.final.write_bytes(b"GATE: PASS\nCANDIDATE: bad\n")
        result = self.observe()
        self.assertEqual(result["slots"], {"round_gate": "malformed", "final_gate": "malformed"})
        self.assertEqual(result["unresolved_slots"], 2)

    def test_receipt_removed_during_inspection_is_unresolved(self):
        self.checks.write_bytes(self.header())
        original = os.read
        def remove_after_read(fd, size):
            prefix = original(fd, size)
            self.checks.unlink()
            return prefix
        with patch("retirement_evidence_advisory.os.read", side_effect=remove_after_read):
            result = self.observe()
        self.assertEqual(result["slots"]["round_gate"], "unavailable")
        self.assertEqual(result["unresolved_slots"], 1)

    def test_round_moved_during_inspection_is_unresolved(self):
        self.checks.write_bytes(self.header())
        original = os.read
        def move_after_read(fd, size):
            prefix = original(fd, size)
            self.round.rename(self.folder / "old-round")
            return prefix
        with patch("retirement_evidence_advisory.os.read", side_effect=move_after_read):
            result = self.observe()
        self.assertEqual(result["slots"]["round_gate"], "unavailable")
