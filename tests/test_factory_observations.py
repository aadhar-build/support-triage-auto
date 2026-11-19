"""Exercise the factory observation store through its capture and read APIs."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "factory" / "lib"))
import observations as O  # noqa: E402


class TestObservationStore(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name) / "001-example"
        self.job.mkdir()
        self.records = self.job / "observations"
        self.job_file = self.job / "job.json"
        self.job_file.write_bytes(b'{"stage":"gate", "revision":5}\n')

    def event(self, kind="owner_effort", data=None, **changes):
        if data is None:
            data = {"activity": "review", "minutes": 0, "rework": False}
        return {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "job_id": self.job.name,
            "origin": "real",
            "kind": kind,
            "source": {"kind": "owner", "record_id": "exercise-01"},
            "data": data,
            **changes,
        }

    def test_no_observations_leaves_legacy_files_untouched(self):
        self.job_file.write_bytes(b'{"owner_minutes":0,"owner_changes":false}')
        self.assertEqual(O.load_events(self.job), ([], []))
        self.assertFalse(self.records.exists())
        self.assertEqual(
            self.job_file.read_bytes(), b'{"owner_minutes":0,"owner_changes":false}'
        )

    def test_zero_unknown_and_abandonment_stay_distinct(self):
        default = O.append_event(self.job, self.event())
        unknown = O.append_event(
            self.job,
            self.event(data={"activity": "recovery", "minutes": None}),
        )
        abandoned = O.append_event(
            self.job, self.event("owner_decision", {"disposition": "abandoned"})
        )
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(diagnostics, [])
        self.assertCountEqual(events, [default, unknown, abandoned])
        self.assertIsNone(default["occurred_at"])
        self.assertEqual(default["data"], {"activity": "review", "minutes": 0, "rework": False})
        self.assertEqual(unknown["data"], {"activity": "recovery", "minutes": None, "rework": None})
        self.assertIsNone(abandoned["data"]["material_follow_up"])

    def test_cost_type_and_units_are_explicit(self):
        cases = [
            ("subscription_usage", 0, "tokens", None),
            ("subscription_charge", "0.00", "USD", "USD"),
            ("incremental_charge", "2.35", "EUR", "EUR"),
            ("api_equivalent_estimate", "1.75", "USD", "USD"),
        ]
        for cost_type, amount, unit, currency in cases:
            O.append_event(
                self.job,
                self.event("usage_cost", {
                    "cost_type": cost_type, "amount": amount, "unit": unit,
                    "currency": currency, "billing_item": cost_type,
                }),
            )
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(diagnostics, [])
        self.assertCountEqual(
            [(d["cost_type"], d["amount"], d["unit"], d["currency"])
             for d in (event["data"] for event in events)],
            cases,
        )

    def test_replay_and_late_correction_project_as_of(self):
        original = self.event(recorded_at="2000-01-01T00:00:00Z")
        stored = O.append_event(self.job, original)
        cutoff = datetime.fromisoformat(stored["recorded_at"].replace("Z", "+00:00"))
        self.assertNotEqual(stored["recorded_at"], original["recorded_at"])
        correction = self.event(
            data={"activity": "review", "minutes": 15, "rework": True},
            supersedes_event_id=stored["event_id"],
            occurred_at="2026-11-19T01:03:05+00:00",
        )
        amended = O.append_event(self.job, correction)
        self.assertEqual(O.append_event(self.job, original), stored)
        self.assertEqual(O.append_event(self.job, amended), amended)
        self.assertEqual(O.load_events(self.job, as_of=cutoff), ([stored], []))
        self.assertEqual(O.load_events(self.job), ([amended], []))
        self.assertEqual(amended["occurred_at"], "2026-11-19T01:03:05Z")
        for record in (stored, amended):
            self.assertEqual(
                json.loads((self.records / f"{record['event_id']}.json").read_bytes()), record
            )
        with self.assertRaisesRegex(O.ObservationError, "conflicting"):
            O.append_event(self.job, {**original, "data": correction["data"]})

    def test_correction_target_is_scoped_to_current_job_kind_and_origin(self):
        original = O.append_event(self.job, self.event())
        other = self.job.parent / "002-other"
        other.mkdir()
        with self.assertRaisesRegex(O.ObservationError, "target.*this job"):
            O.append_event(other, self.event(job_id=other.name, supersedes_event_id=original["event_id"]))
        with self.assertRaisesRegex(O.ObservationError, "same kind"):
            O.append_event(self.job, self.event("limitation", {"code": "incomplete"},
                                               supersedes_event_id=original["event_id"]))
        with self.assertRaisesRegex(O.ObservationError, "same origin"):
            O.append_event(self.job, self.event(origin="controlled",
                                               supersedes_event_id=original["event_id"]))

    def test_correction_ancestry_is_available_at_the_recording_cutoff(self):
        first = O.append_event(self.job, self.event())
        second = O.append_event(self.job, self.event(supersedes_event_id=first["event_id"]))
        cutoff = datetime.fromisoformat(second["recorded_at"].replace("Z", "+00:00"))
        third = O.append_event(self.job, self.event(supersedes_event_id=second["event_id"]))

        self.assertEqual(O.load_events(self.job), ([third], []))
        self.assertEqual(O.load_events(self.job, as_of=cutoff, include_ancestry=True), (
            [{**second, "superseded_event_ids": [first["event_id"]]}], [],
        ))
        self.assertEqual(O.load_events(self.job, include_ancestry=True), (
            [{**third, "superseded_event_ids": [first["event_id"], second["event_id"]]}], [],
        ))
        self.assertNotIn(
            "superseded_event_ids",
            json.loads((self.records / f"{third['event_id']}.json").read_bytes()),
        )

    def test_ancestry_cannot_be_injected_or_fork_the_projected_chain(self):
        first = O.append_event(self.job, self.event())
        second = O.append_event(self.job, self.event(supersedes_event_id=first["event_id"]))
        spoof = self.event(superseded_event_ids=[first["event_id"]])
        with self.assertRaises(O.ObservationError):
            O.append_event(self.job, spoof)
        fork = self.event(supersedes_event_id=first["event_id"], recorded_at="2099-01-01T00:00:00Z")
        (self.records / f"{fork['event_id']}.json").write_text(json.dumps(fork))

        events, diagnostics = O.load_events(self.job, include_ancestry=True)
        self.assertEqual(events, [{**second, "superseded_event_ids": [first["event_id"]]}])
        self.assertEqual(len(diagnostics), 1)
        self.assertIn(fork["event_id"], diagnostics[0])

    def test_incomplete_correction_history_cannot_fork_after_restoration(self):
        first = O.append_event(self.job, self.event())
        second = O.append_event(self.job, self.event(supersedes_event_id=first["event_id"]))
        existing = self.records / f"{second['event_id']}.json"
        complete = existing.read_bytes()
        existing.write_bytes(b"{")
        with self.assertRaisesRegex(O.ObservationError, "incomplete.*history"):
            O.append_event(self.job, self.event(supersedes_event_id=first["event_id"]))
        unrelated = O.append_event(self.job, self.event())
        existing.write_bytes(complete)
        events, diagnostics = O.load_events(self.job)
        self.assertCountEqual(events, [second, unrelated])
        self.assertEqual(diagnostics, [])

    def test_concurrent_corrections_cannot_fork(self):
        original = O.append_event(self.job, self.event())
        ready = Barrier(2)

        def submit(minutes):
            event = self.event(
                data={"activity": "review", "minutes": minutes},
                supersedes_event_id=original["event_id"],
            )
            ready.wait(timeout=5)
            try:
                return O.append_event(self.job, event)
            except O.ObservationError as exc:
                self.assertRegex(str(exc), "already.*superseded")
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            written = [record for record in pool.map(submit, [4, 9]) if record is not None]
        self.assertEqual(len(written), 1)
        self.assertEqual(O.load_events(self.job), (written, []))
        self.assertEqual(len(list(self.records.glob("*.json"))), 2)

    def test_append_during_pending_gate_and_worker_publication(self):
        (self.job / "round-1").mkdir()
        gate = self.job / ".gate-publication.pending"
        worker = self.job / "round-1" / "build.json.pending"
        gate.write_bytes(b"gate journal\n")
        worker.write_bytes(b'{"worker":"pending"}\n')
        lock = os.open(self.job, os.O_RDONLY)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                result = pool.submit(O.append_event, self.job, self.event()).result(timeout=5)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(O.load_events(self.job), ([result], []))
        self.assertEqual(self.job_file.read_bytes(), b'{"stage":"gate", "revision":5}\n')
        self.assertEqual(gate.read_bytes(), b"gate journal\n")
        self.assertEqual(worker.read_bytes(), b'{"worker":"pending"}\n')

    def test_invalid_records_surface_and_interrupted_stage_is_ignored(self):
        good = O.append_event(self.job, self.event())
        version = self.event(schema_version=99, recorded_at=good["recorded_at"])
        (self.records / f"{version['event_id']}.json").write_text(json.dumps(version))
        malformed = self.records / f"{uuid4()}.json"
        malformed.write_bytes(b"bad json")
        oversized = self.records / f"{uuid4()}.json"
        oversized.write_bytes(b"x" * (64 * 1024 + 1))
        symlink = self.records / f"{uuid4()}.json"
        symlink.symlink_to(self.job_file)
        stage = self.records / ".interrupted.json.tmp"
        stage.write_bytes(b"{")
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(events, [good])
        self.assertEqual(len(diagnostics), 4)
        self.assertTrue(any("unsupported schema version" in d for d in diagnostics))
        for path in (malformed, oversized, symlink):
            self.assertTrue(any(path.name in d for d in diagnostics))
        self.assertEqual(stage.read_bytes(), b"{")

    def test_hard_link_to_external_record_cannot_create_an_observation(self):
        external = self.job.parent / "external.json"
        event = self.event(recorded_at="2026-11-19T01:03:05Z")
        external.write_text(json.dumps(event))
        self.records.mkdir()
        link = self.records / f"{event['event_id']}.json"
        os.link(external, link)
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(events, [])
        self.assertTrue(any(link.name in item for item in diagnostics))

    def test_diagnostics_do_not_repeat_untrusted_filenames(self):
        self.records.mkdir()
        (self.records / "raw customer name\nsecond line.json").write_text("{}")
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(events, [])
        self.assertEqual(diagnostics, ["invalid observation filename"])

    def test_full_length_job_directory_and_event_share_a_filename_bound(self):
        job_id = "001-" + "x" * 251
        directory = self.job.with_name(job_id)
        directory.mkdir()
        with self.assertRaises(O.ObservationError):
            O.append_event(directory, self.event(job_id=job_id, source={"kind": "owner", "record_id": "a" * 129}))
        self.assertFalse((directory / "observations").exists())
        event = O.append_event(directory, self.event(job_id=job_id))
        self.assertEqual(O.load_events(directory), ([event], []))
        with self.assertRaises(O.ObservationError):
            oversized = directory.with_name(job_id + "x")
            O.append_event(oversized, self.event(job_id=oversized.name))

    def test_invalid_payloads_do_not_create_observations(self):
        invalid = [
            self.event(event_id="../escape"),
            self.event(job_id="other-job"),
            self.event(schema_version=True),
            self.event(occurred_at="2026-11-19T01:03:05"),
            self.event(occurred_at="2026-11-19T01:03:05+05:30"),
            self.event(candidate_sha="a" * 12, round=1),
            self.event(round=False),
            self.event(data={"activity": "review", "minutes": True}),
            self.event(data={"activity": "review", "minutes": float("inf")}),
            self.event(data={"activity": "review", "minutes": -5}),
            self.event(data={"activity": "review", "minutes": 0, "raw": "private data"}),
            self.event("usage_cost", {"cost_type": "incremental_charge", "amount": 1.0, "currency": "USD"}),
            self.event("usage_cost", {"cost_type": "incremental_charge", "amount": "1.0"}),
            self.event("usage_cost", {"cost_type": "incremental_charge", "amount": "1.0", "currency": "USD"}),
        ]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(O.ObservationError):
                O.append_event(self.job, event)
        self.assertFalse(self.records.exists())

    def test_json_decoder_matches_ingress_and_rejects_ambiguous_records(self):
        self.assertEqual(O.decode_event_json(b'{"data":{"minutes":0}}'), {"data": {"minutes": 0}})
        for raw in (
            b'{"event_id":"first","event_id":"second"}',
            b'{"data":{"minutes":0,"minutes":1}}',
            b'{"data":{"minutes":Infinity}}',
            b'[]',
        ):
            with self.subTest(raw=raw), self.assertRaises(O.ObservationError):
                O.decode_event_json(raw)

    def test_unsafe_evidence_references_are_refused(self):
        outside = self.job.parent / "outside.txt"
        outside.write_text("synthetic")
        (self.job / "alias.txt").symlink_to(outside)
        (self.job / "alias").symlink_to(outside.parent)
        for artifact in ("../outside.txt", "/etc/passwd", "foo//bar", "foo/./bar",
                         "foo\\bar", "alias.txt", "alias/outside.txt"):
            with self.subTest(artifact=artifact), self.assertRaises(O.ObservationError):
                O.append_event(self.job, self.event(evidence=[{"path": artifact, "sha256": "a" * 64}]))
        for url in ("http://example.org/proof", "https://user:pass@example.org/proof",
                    "https://example.org/proof?sig=private", "https://example.org/proof)",
                    "https://example.org/proof#access_token=synthetic-demo",
                    "https://example.org/proof#section%26access_token%3Dsynthetic-demo"):
            with self.subTest(url=url), self.assertRaises(O.ObservationError):
                O.append_event(self.job, self.event(evidence=[{"url": url, "verified": True}]))
        self.assertEqual(O.load_events(self.job), ([], []))

    def test_valid_evidence_is_retained_without_claiming_verification(self):
        ref = {"path": "round-1/sanitized.md", "sha256": "a" * 64}
        url = {"url": "https://github.com/example/work/issues/1#issuecomment-456", "verified": False}
        record = O.append_event(self.job, self.event(evidence=[ref, url]))
        self.assertEqual(record["evidence"], [ref, url])
        self.assertEqual(O.load_events(self.job), ([record], []))

    def test_symlinked_job_and_observation_directories_are_refused(self):
        outside = self.job.parent / "outside"
        outside.mkdir()
        job_alias = self.job.parent / "second-job"
        job_alias.symlink_to(self.job)
        with self.assertRaises(O.ObservationError):
            O.append_event(job_alias, self.event(job_id=job_alias.name))
        self.records.symlink_to(outside)
        with self.assertRaises(O.ObservationError):
            O.append_event(self.job, self.event())
        events, diagnostics = O.load_events(self.job)
        self.assertEqual(events, [])
        self.assertTrue(diagnostics)
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_publication_can_be_retried_with_the_same_id(self):
        event = self.event()
        with patch.object(O.os, "replace", side_effect=OSError("interrupted publication")):
            with self.assertRaises(O.ObservationError):
                O.append_event(self.job, event)
        self.assertEqual(O.load_events(self.job), ([], []))
        stored = O.append_event(self.job, event)
        self.assertEqual(O.load_events(self.job), ([stored], []))

    def test_replay_can_finish_sync_after_publication(self):
        event = self.event()
        sync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("interrupted directory sync")
            sync(fd)

        with patch.object(O.os, "fsync", side_effect=fail_directory_sync):
            with self.assertRaises(O.ObservationError):
                O.append_event(self.job, event)
        stored = json.loads((self.records / f"{event['event_id']}.json").read_text())
        self.assertEqual(O.append_event(self.job, event), stored)
        self.assertEqual(O.load_events(self.job), ([stored], []))


if __name__ == "__main__":
    unittest.main()
