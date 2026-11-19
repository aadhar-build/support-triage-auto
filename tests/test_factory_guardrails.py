import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory" / "lib"))
import guardrails


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.job = {"id": "job-1", "stage": "approve", "round": 2}
        self.current = {"candidate_sha": "a" * 40, "base_sha": "b" * 40,
                        "spec_digest": "c" * 64, "round": 2}
        self.window = {"scope": "protected_paths", "window_start": "2026-11-01T00:00:00Z",
                       "window_end": "2026-11-08T00:00:00Z"}
        self.attestations = {}

    def event(self, kind, **data):
        event_id = str(uuid4())
        observed = data["window_end"] if kind == "observation_coverage" else "2026-11-05T12:00:00Z"
        recorded = data["window_end"] if kind == "observation_coverage" else "2026-11-05T12:01:00Z"
        return {"schema_version": 1, "event_id": event_id, "job_id": self.job["id"],
                "origin": "real", "kind": kind, "occurred_at": observed,
                "recorded_at": recorded,
                "source": {"kind": "controller", "record_id": event_id},
                "evidence": [], "supersedes_event_id": None,
                "candidate_sha": self.current["candidate_sha"], "round": 2, "data": data}

    def check(self, verdict="pass"):
        return self.event("check_evidence", check_id="checks", verdict=verdict,
                          base_sha=self.current["base_sha"], spec_digest=self.current["spec_digest"])

    def attest(self, event):
        # Test verifier pins the facts independently from the editable job directory.
        payload = json.dumps(event, sort_keys=True).encode()
        path = event["event_id"] + ".json"
        (self.root / path).write_bytes(payload)
        self.attestations[path] = payload
        event["evidence"] = [{"path": path, "sha256": hashlib.sha256(payload).hexdigest()}]
        return event

    def verify(self, event, ref, payload):
        return (payload is not None and self.attestations.get(ref.get("path")) == payload
                and json.loads(payload) == {**event, "evidence": []})

    def evaluate(self, events, required=("checks",), **candidate):
        return guardrails.evaluate_guardrails(self.root, self.job, events, required,
                                               {**self.current, "verify_evidence": self.verify, **candidate})

    def test_verified_failure_is_covered_but_only_passing_checks_are_proven(self):
        for verdict, proven in [("fail", 0), ("pass", 1)]:
            with self.subTest(verdict=verdict):
                event = self.attest(self.check(verdict))
                report = self.evaluate([event], ["checks", "review:code", {"id": "review:eval", "required": False}])
                self.assertEqual([r["status"] for r in report["requirements"]], [verdict, "missing", "not_required"])
                self.assertEqual(report["requirements"][0]["evidence"], event["evidence"])
                self.assertEqual(report["coverage"]["covered"], 1)
                self.assertEqual(report["coverage"]["eligible"], 2)
                self.assertEqual(report["coverage"]["proven"], proven)
                self.assertEqual(report["coverage"]["jobs_covered"], 0)

    def test_candidate_base_spec_and_round_must_match_full_binding(self):
        for key, value in self.current.items():
            with self.subTest(key=key):
                event = self.check()
                fields = event if key in ("candidate_sha", "round") else event["data"]
                fields[key] = 1 if key == "round" else "d" * len(value)
                row = self.evaluate([self.attest(event)])["requirements"][0]
                self.assertEqual(row["status"], "stale")
                self.assertEqual(row["event_ids"], [event["event_id"]])
                fields[key] = None
                self.assertEqual(self.evaluate([self.attest(event)])["requirements"][0]["status"], "unverified")

    def test_assertion_digest_bare_link_legacy_marker_and_pending_cannot_prove_a_check(self):
        (self.root / "decision.md").write_text("APPROVE\n")
        (self.root / "job.json").write_text('{"stage":"approve"}\n')
        journal = self.root / "worker.pending"
        journal.write_text("pending\n")
        event = self.attest(self.check())
        for e in (event, {**event, "evidence": [{"url": "https://example.org/report/1", "verified": True}]}):
            result = self.evaluate([e], ["checks", {"id": "approval", "legacy_paths": ["decision.md"]}],
                                   verify_evidence=None)
            self.assertEqual([r["status"] for r in result["requirements"]], ["unverified", "unverified"])
        self.assertEqual(self.evaluate([event, self.check("pending")])["requirements"][0]["status"], "pending")
        self.assertEqual(journal.read_text(), "pending\n")
        self.assertEqual((self.root / "job.json").read_text(), '{"stage":"approve"}\n')

    def test_substitution_changed_bytes_and_symlinks_do_not_prove_a_check(self):
        event = self.attest(self.check())
        receipt = self.root / event["evidence"][0]["path"]
        original = receipt.read_bytes()
        def changing_verifier(*args):
            receipt.write_text("replaced")
            return True
        self.assertEqual(self.evaluate([event], verify_evidence=changing_verifier)["coverage"]["covered"], 0)
        self.assertEqual(self.evaluate([event])["coverage"]["covered"], 0)
        original_file = self.root / "original"
        original_file.write_bytes(original)
        receipt.unlink()
        receipt.symlink_to(original_file)
        self.assertEqual(self.evaluate([event])["coverage"]["covered"], 0)

    def test_current_conflicting_results_fail_closed_and_older_candidate_cannot_override(self):
        passed = self.attest(self.check())
        failed = self.attest(self.check("fail"))
        earlier = self.check("fail")
        earlier["candidate_sha"] = "d" * 40
        self.assertEqual(self.evaluate([passed, failed])["requirements"][0]["status"], "fail")
        self.assertEqual(self.evaluate([passed, self.attest(earlier)])["requirements"][0]["status"], "pass")

    def incident(self, incident_id, disposition, status="confirmed"):
        return self.event("incident", incident_id=incident_id, category="protected_path_change",
                          disposition=disposition, status=status, scope="protected_paths", resolution_ref=None)

    def test_duplicate_incidents_controlled_inputs_and_suspicions_are_separate(self):
        blocked = self.attest(self.incident("attempt", "blocked"))
        effective = self.attest(self.incident("violation", "effective"))
        suspected = self.incident("investigate", "unknown", "suspected")
        unverified = self.incident("claim", "effective")
        other = self.attest({**self.incident("other", "effective"), "job_id": "another"})
        controlled = self.attest({**self.incident("exercise", "effective"), "origin": "controlled"})
        result = self.evaluate([effective, blocked, suspected, blocked, unverified, controlled, other])["incidents"]
        self.assertEqual(len(result["records"]), 4)
        self.assertEqual(result["confirmed_blocked"], 1)
        self.assertEqual(result["confirmed_effective_observed"], 1)
        self.assertEqual(result["suspected"], 1)
        self.assertIsNone(result["effective_total"])

    def test_zero_requires_verified_complete_window_of_the_requested_scope(self):
        coverage = self.event("observation_coverage", **self.window, complete=True)
        for cases, window in [([], self.window), ([coverage], self.window),
                              ([self.attest(deepcopy(coverage))], {**self.window, "scope": "all"}),
                              ([self.attest(deepcopy(coverage))], {**self.window, "window_end": "2026-11-09T00:00:00Z"})]:
            self.assertIsNone(self.evaluate(cases, incident_window=window)["incidents"]["effective_total"])
        genuine = self.attest(coverage)
        self.assertEqual(self.evaluate([genuine], incident_window=self.window)["incidents"]["effective_total"], 0)
        self.assertIsNone(self.evaluate([genuine, self.incident("unknown", "unknown", "suspected")],
                                        incident_window=self.window)["incidents"]["effective_total"])

    def test_interruption_outcome_requires_independent_evidence_and_valid_time(self):
        def interruption(identifier, outcome, end):
            return self.event("interruption", interruption_id=identifier, outcome=outcome,
                              started_at="2026-11-05T11:00:00Z", ended_at=end, verification_ref=str(uuid4()))
        events = [self.attest(interruption("one", "resumed", "2026-11-05T12:00:00Z")),
                  self.attest(interruption("two", "stopped", "2026-11-05T11:01:00Z")),
                  interruption("three", "resumed", "2026-11-05T11:02:00Z"),
                  self.attest(interruption("four", "resumed", "2026-11-05T10:00:00Z")),
                  interruption("five", "unresolved", None)]
        result = self.evaluate(events + [events[0]])["interruptions"]
        self.assertEqual((result["observed"], result["verified_safe"], result["resumed"], result["stopped"]), (5, 2, 1, 1))
        self.assertEqual(result["unresolved"], 1)
        first = next(r for r in result["records"] if r["interruption_id"] == "one")
        self.assertEqual(first["elapsed_seconds"], 3600)
        self.assertEqual(first["event_ids"], [events[0]["event_id"]])

    def test_stale_history_can_prove_its_own_round_only(self):
        historic = self.check("fail")
        historic["round"] = 1
        current = self.attest(self.check())
        row = self.evaluate([self.attest(historic), current])["requirements"][0]
        self.assertEqual(row["status"], "pass")
        attempt = next(a for a in row["observations"] if a["event_id"] == historic["event_id"])
        self.assertTrue(attempt["verified"])
        self.assertEqual((attempt["status"], attempt["verdict"], attempt["round"]), ("stale", "fail", 1))
        self.assertEqual(attempt["base_sha"], self.current["base_sha"])

    def test_candidate_changed_during_snapshot_suppresses_positive_evidence(self):
        event = self.attest(self.check())
        report = self.evaluate([event], unstable=True)
        self.assertEqual(report["requirements"][0]["status"], "unverified")
        self.assertFalse(report["requirements"][0]["observations"][0]["verified"])
        self.assertEqual(report["coverage"]["covered"], 0)

    def test_typed_criterion_is_verified_against_the_trusted_requirement(self):
        criterion = self.attest(self.event("criterion", criterion_id="export", verdict="pass",
                                           base_sha=self.current["base_sha"], spec_digest=self.current["spec_digest"]))
        req = {"id": "criterion:export", "label": "Export a report"}
        row = self.evaluate([criterion], [req])["requirements"][0]
        self.assertEqual((row["status"], row["label"]), ("pass", "Export a report"))
        self.assertEqual(row["evidence"], criterion["evidence"])

    def test_multiple_windows_with_a_gap_or_diagnostics_cannot_prove_absence(self):
        tuesday = "2026-11-03T00:00:00Z"
        left = self.attest(self.event("observation_coverage", **{**self.window, "window_end": tuesday}, complete=True))
        right = self.attest(self.event("observation_coverage", **{**self.window, "window_start": tuesday}, complete=True))
        self.assertEqual(self.evaluate([left, right], incident_window=self.window)["incidents"]["effective_total"], 0)
        self.assertIsNone(self.evaluate([left, right], incident_window=self.window,
                                        diagnostics=["unreadable observation"])["incidents"]["effective_total"])
        right["data"]["window_start"] = "2026-11-03T00:00:01Z"
        self.assertIsNone(self.evaluate([left, self.attest(right)], incident_window=self.window)["incidents"]["effective_total"])

    def test_unrated_observations_and_incomplete_safe_outcomes_stay_visible(self):
        incident = self.incident("unrated", "unknown", None)
        record = self.event("interruption", interruption_id="unrated", outcome=None,
                            started_at=None, ended_at=None, verification_ref=None)
        result = self.evaluate([incident, record])
        self.assertEqual(result["incidents"]["suspected"], 0)
        self.assertEqual(result["incidents"]["records"][0]["status"], "unknown")
        self.assertEqual(result["interruptions"]["unknown"], 1)
        self.assertEqual(result["interruptions"]["verified_safe"], 0)

    def test_incident_window_uses_all_unsuperseded_location_claims(self):
        coverage = self.attest(self.event("observation_coverage", **self.window, complete=True))
        other_date = "2026-10-31T12:00:00Z"
        inside = self.incident("duplicate", "effective")
        for when, scope, expected in [
                (other_date, "protected_paths", None),
                (inside["occurred_at"], "other_scope", None),
                ("2026-11-04T12:00:00Z", "protected_paths", 1)]:
            with self.subTest(when=when, scope=scope):
                previous = self.incident("duplicate", "effective")
                previous["occurred_at"] = when
                previous["data"]["scope"] = scope
                self.attest(previous)
                report = self.evaluate([inside, coverage, previous], incident_window=self.window)["incidents"]
                self.assertEqual(report["effective_total"], expected)
                row = report["records"][0]
                expected_facts = {(when, scope), (inside["occurred_at"], inside["data"]["scope"])}
                self.assertEqual({(e["occurred_at"], e["scope"]) for e in row["claims"]}, expected_facts)

    def test_interruption_uses_latest_occurrence_and_retains_unordered_conflicts(self):
        def state(outcome, when):
            event = self.event("interruption", interruption_id="job-restart", outcome=outcome,
                               started_at="2026-11-05T10:00:00Z",
                               ended_at="2026-11-05T12:00:00Z" if outcome == "resumed" else None)
            event["occurred_at"] = when
            return self.attest(event)
        resumed = state("resumed", "2026-11-05T12:00:00Z")
        for when, expected_outcome, expected_safe in [
                ("2026-11-05T11:00:00Z", "resumed", 1),
                ("2026-11-05T13:00:00Z", "unresolved", 0),
                ("2026-11-05T12:00:00Z", "unresolved", 0),
                (None, "unresolved", 0)]:
            with self.subTest(when=when):
                unresolved = state("unresolved", when)
                result = self.evaluate([unresolved, resumed])["interruptions"]
                self.assertEqual(result["verified_safe"], expected_safe)
                row = result["records"][0]
                self.assertEqual(row["outcome"], expected_outcome)
                if expected_outcome == "unresolved":
                    self.assertIsNone(row["elapsed_seconds"])
                self.assertCountEqual(row["event_ids"], [unresolved["event_id"], resumed["event_id"]])

    def test_replaced_job_ancestors_and_hardlinked_receipts_cannot_expose_bytes(self):
        import os
        job = self.root / "factory" / "jobs" / "job-1"
        job.mkdir(parents=True)
        receipt = job / "receipt.json"
        receipt.write_bytes(b"verified")
        self.assertEqual(guardrails.read_job_artifact(job, receipt.name), b"verified")
        elsewhere = self.root / "external"
        elsewhere.write_bytes(b"external")
        receipt.unlink()
        os.link(elsewhere, receipt)
        self.assertIsNone(guardrails.read_job_artifact(job, receipt.name))
        receipt.unlink()
        receipt.write_bytes(b"verified")
        for relative in ("factory/jobs", "factory"):
            with self.subTest(ancestor=relative):
                ancestor = self.root / relative
                moved = ancestor.with_name("moved")
                ancestor.rename(moved)
                ancestor.symlink_to(moved, target_is_directory=True)
                try:
                    self.assertIsNone(guardrails.read_job_artifact(job, receipt.name))
                finally:
                    ancestor.unlink()
                    moved.rename(ancestor)

    def test_blocked_claim_cannot_clear_a_competing_effective_incident_report(self):
        coverage = self.attest(self.event("observation_coverage", **self.window, complete=True))
        blocked = self.attest(self.incident("disputed", "blocked"))
        competing = self.incident("disputed", "effective", "suspected")
        report = self.evaluate([coverage, blocked, competing], incident_window=self.window)["incidents"]
        self.assertIsNone(report["effective_total"])
        self.assertEqual(report["records"][0]["disposition"], "unknown")

    def test_coverage_cannot_extend_past_observation_or_ingestion(self):
        declared_end = self.window["window_end"]
        horizon = "2026-11-04T10:00:00Z"
        partial_window = {**self.window, "window_end": horizon}
        for field in ("occurred_at", "recorded_at"):
            with self.subTest(field=field):
                event = self.event("observation_coverage", **self.window, complete=True)
                event[field] = horizon
                self.attest(event)
                full = self.evaluate([event], incident_window=self.window)["incidents"]
                self.assertIsNone(full["effective_total"])
                row = full["coverage_windows"][0]
                self.assertEqual(row["window_end"], horizon)
                self.assertEqual(row["declared_window_end"], declared_end)
                self.assertEqual(self.evaluate([event], incident_window=partial_window)["incidents"]["effective_total"], 0)

    def test_unknown_or_future_declaration_cannot_observe_an_earlier_period(self):
        early = {**self.window, "window_end": "2026-11-04T00:00:00Z"}
        for field, value in [("occurred_at", None), ("recorded_at", None),
                             ("recorded_at", "2026-11-10T00:00:00Z"),
                             ("occurred_at", "2026-11-10T00:00:00Z")]:
            with self.subTest(field=field, value=value):
                event = self.event("observation_coverage", **self.window, complete=True)
                event[field] = value
                report = self.evaluate([self.attest(event)], incident_window=early, as_of="2026-11-09T00:00:00Z")
                self.assertIsNone(report["incidents"]["effective_total"])
                self.assertFalse(any(w["verified"] for w in report["incidents"]["coverage_windows"]))
