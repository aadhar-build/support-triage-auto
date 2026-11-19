"""Recovery reporting from job history and independent v1 observations."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "factory" / "lib"))
import factory as F  # noqa: E402
from factory_recovery import recovery_summary  # noqa: E402


JOB_ID = "001-recovery"
OCCURRED = "2026-11-19T02:10:00Z"
RECORDED = "2026-11-19T02:15:00Z"
CANDIDATE = "a" * 40
EVIDENCE = {"path": "round-2/observed-failure.json", "sha256": "b" * 64}


def event_id(number):
    return str(UUID(int=(1 << 96) + (4 << 76) + (8 << 60) + number))


def observation(number, kind, data, *, origin="real", job_id=JOB_ID,
                round_number=2, evidence=(), source="controller", supersedes=None):
    return {
        "schema_version": 1,
        "event_id": event_id(number),
        "kind": kind,
        "job_id": job_id,
        "occurred_at": OCCURRED,
        "recorded_at": RECORDED,
        "origin": origin,
        "source": {"kind": source, "record_id": f"fixture-{number}"},
        "candidate_sha": CANDIDATE,
        "round": round_number,
        "evidence": list(evidence),
        "supersedes_event_id": supersedes,
        "data": data,
    }


def defect(number, *, evidence=(), **data):
    return observation(number, "defect", {
        "defect_id": "defect-1", "origin": "escaped", "discovered_at": OCCURRED,
        "impact": "workflow_blocked", "proposed_recovery": None, "outcome": "unresolved",
        "cause": None, "correction_event_id": None, "regression_check_event_id": None,
        "no_check_reason": None, "owner_effort_event_ids": [], "interruption_event_ids": [],
        **data,
    }, evidence=evidence)


def decision(number, *, round_number=2, evidence=(), source="controller", **data):
    return observation(number, "recovery_decision", {
        "failure_id": "failure-1", "decision": None, "impact": None,
        "proposed_recovery": None, "outcome": None, "cause": None,
        "correction_event_id": None, "regression_check_event_id": None,
        "no_check_reason": None, "owner_effort_event_ids": [], "interruption_event_ids": [],
        **data,
    }, round_number=round_number, evidence=evidence, source=source)


def exhausted_job():
    job = F.new_job(JOB_ID, 1, "Recovery fixture")
    for stage in ("spec", "ready", "build", "gate"):
        F.move(job, stage)
    job["round"] = 1
    F.next_round_or_recovery(job, 2, "gate failed")
    F.move(job, "gate")
    F.next_round_or_recovery(job, 2, "gate failed")
    return job


class TestRecoverySummary(unittest.TestCase):
    def test_exhausted_retries_preserve_failures_and_label_reported_scope_split(self):
        job = exhausted_job()
        proposal = decision(1, proposed_recovery="split_scope", evidence=[EVIDENCE])
        failed_check = observation(2, "check_evidence", {
            "check_id": "smoke", "verdict": "fail",
        }, evidence=[EVIDENCE])
        outcome = decision(3, outcome="pending", evidence=[EVIDENCE])
        events = [failed_check, proposal, outcome]
        before = deepcopy((job, events))

        summary = recovery_summary(job, events)

        self.assertIs(summary["retry_exhausted"], True)
        self.assertEqual(summary["retry_exhaustion"]["round"], 2)
        self.assertEqual([failure["kind"] for failure in summary["failed_attempts"]],
                         ["gate_failure", "gate_failure"])
        self.assertEqual(summary["failed_checks"][0]["evidence"], [EVIDENCE])
        self.assertEqual(summary["decisions"][0]["proposed_recovery"], "split_scope")
        self.assertEqual(summary["recommendations"][0]["action"], "ask_owner")
        self.assertIn("reported scope split", summary["owner_question"])
        self.assertEqual((job, events), before)

    def test_controller_exhaustion_recognizes_an_owner_retry_above_cap(self):
        job = exhausted_job()
        F.move(job, "build", "owner requested another attempt")
        job["round"] = 3
        F.move(job, "gate")
        F.next_round_or_recovery(job, 2, "gate failed")

        summary = recovery_summary(job, [])

        self.assertIs(summary["retry_exhausted"], True)
        self.assertEqual(summary["retry_exhaustion"]["round"], 3)
        self.assertEqual(summary["retry_exhaustion"]["round_cap"], 2)

    def test_exhaustion_without_safe_path_asks_one_current_job_question(self):
        events = [decision(1, round_number=1, proposed_recovery="permitted_alternative",
                           evidence=[EVIDENCE]),
                  decision(2, outcome="no_safe_recovery")]

        summary = recovery_summary(exhausted_job(), events)

        self.assertEqual([item["action"] for item in summary["recommendations"]], ["ask_owner"])
        self.assertIn(JOB_ID, summary["owner_question"])
        self.assertEqual(summary["owner_question"].count("?"), 1)

    def test_recorded_owner_choice_remains_visible_without_new_question(self):
        events = [decision(1, source="owner", decision="follow_up_debt", outcome="stopped")]

        summary = recovery_summary(exhausted_job(), events)

        self.assertEqual(summary["decisions"][0]["decision"], "follow_up_debt")
        self.assertEqual(summary["decisions"][0]["outcome"], "stopped")
        self.assertEqual(summary["recommendations"], [])
        self.assertIsNone(summary["owner_question"])

    def test_choice_for_another_failure_keeps_current_recovery_question(self):
        events = [decision(1, source="owner", decision="follow_up_debt", outcome="stopped"),
                  decision(2, failure_id="failure-2", outcome="unresolved")]

        summary = recovery_summary(exhausted_job(), events)

        self.assertEqual([item["action"] for item in summary["recommendations"]], ["ask_owner"])
        self.assertIn("failure-2", summary["owner_question"])

    def test_conflicting_owner_choices_for_the_same_failure_require_clarification(self):
        events = [decision(1, source="owner", decision="follow_up_debt"),
                  decision(2, source="owner", decision="retry")]

        summary = recovery_summary(exhausted_job(), events)

        self.assertIn("decision", summary["decisions"][0]["conflicts"])
        self.assertEqual([item["action"] for item in summary["recommendations"]], ["ask_owner"])
        self.assertIn("failure-1", summary["owner_question"])

    def test_round_count_and_interruption_cannot_establish_exhaustion(self):
        job = {"id": JOB_ID, "stage": "recovery", "round": 7, "history": []}
        interrup = observation(1, "interruption", {
            "interruption_id": "interrupt-1", "started_at": OCCURRED, "ended_at": None,
            "outcome": "unresolved", "verification_ref": None,
        })

        summary = recovery_summary(job, [interrup])

        self.assertIsNone(summary["retry_exhausted"])
        self.assertEqual(summary["failed_attempts"], [])
        self.assertEqual(summary["interruptions"][0]["outcome"], "unresolved")
        self.assertIsNone(summary["interruptions"][0]["elapsed_seconds"])

    def test_escaped_defect_links_fix_check_and_explicit_zero_owner_effort(self):
        correction = observation(1, "recovery_decision", {
            "failure_id": "failure-1", "outcome": "resolved",
        }, evidence=[{"url": "https://example.test/pull/2", "verified": True}])
        check = observation(2, "check_evidence", {
            "check_id": "full-regression", "verdict": "pass",
        }, evidence=[EVIDENCE])
        effort = observation(3, "owner_effort", {
            "activity": "recovery", "minutes": 0, "rework": True,
        })
        escaped = defect(4, outcome="resolved", correction_event_id=event_id(1),
                         regression_check_event_id=event_id(2), owner_effort_event_ids=[event_id(3)])

        summary = recovery_summary({"id": JOB_ID, "stage": "merged"}, [escaped, check, correction, effort])
        row = summary["defects"][0]

        self.assertEqual((row["job_id"], row["origin"], row["discovered_at"]),
                         (JOB_ID, "escaped", OCCURRED))
        self.assertEqual(row["correction"]["evidence"][0]["url"], "https://example.test/pull/2")
        self.assertEqual(row["regression_check"]["data"]["check_id"], "full-regression")
        self.assertEqual(row["known_owner_minutes"], 0)
        self.assertEqual(row["regression_status"], "reported_link")
        self.assertEqual(row["correction_status"], "reported_link")
        self.assertIsNone(row["cause"])
        self.assertEqual(summary["unresolved_defects"], [])

    def test_only_one_explicit_no_check_reason_and_no_cross_job_fix(self):
        correction = observation(1, "recovery_decision", {}, job_id="other-job", evidence=[EVIDENCE])
        controlled_check = observation(2, "check_evidence", {"verdict": "pass"},
                                       origin="controlled", evidence=[EVIDENCE])
        report = defect(3, evidence=[{"url": "https://example.test/assertion", "verified": False}],
                        correction_event_id=event_id(1), regression_check_event_id=event_id(2),
                        cause="input_mismatch", no_check_reason="vendor_outage")

        summary = recovery_summary({"id": JOB_ID, "stage": "merged"}, [correction, controlled_check, report])
        row = summary["defects"][0]

        self.assertIsNone(row["cause"])
        self.assertIsNone(row["correction"])
        self.assertIsNone(row["regression_check"])
        self.assertEqual(row["no_check_reason"], "vendor_outage")
        self.assertEqual(row["regression_status"], "no_suitable_check")
        self.assertEqual(summary["unresolved_defects"], ["defect-1"])

    def test_claimed_reference_verification_preserves_reported_cause_without_confirming_it(self):
        for evidence in (EVIDENCE, {"url": "https://example.test/cause", "verified": True}):
            with self.subTest(evidence=evidence):
                cause_report = defect(1, cause="schema_mismatch", evidence=[evidence])

                row = recovery_summary({"id": JOB_ID, "stage": "merged"}, [cause_report])["defects"][0]

                self.assertIsNone(row["cause"])
                self.assertEqual(row["reported_cause"], "schema_mismatch")
                self.assertEqual(row["observations"][0]["evidence"], [evidence])

    def test_duplicate_reports_preserve_evidence_without_doubling_incidents(self):
        first = defect(1, interruption_event_ids=[event_id(3), event_id(4)])
        second = defect(2, evidence=[EVIDENCE])
        interruption_data = {"interruption_id": "interrupt-1", "started_at": OCCURRED,
                             "ended_at": RECORDED, "outcome": "resumed", "verification_ref": None}
        third = observation(3, "interruption", interruption_data)
        fourth = observation(4, "interruption", interruption_data)
        controlled = observation(5, "interruption", interruption_data, origin="controlled")

        summary = recovery_summary({"id": JOB_ID, "stage": "merged"},
                                   [first, first, second, third, fourth, controlled])

        self.assertEqual(len(summary["defects"]), 1)
        self.assertEqual(len(summary["defects"][0]["observations"]), 2)
        self.assertEqual(len(summary["defects"][0]["linked_interruptions"]), 1)
        self.assertEqual(len(summary["interruptions"]), 1)
        self.assertEqual(summary["interruptions"][0]["elapsed_seconds"], 300)
        self.assertEqual(summary["controlled"]["interruptions"][0]["observations"][0]["event_id"],
                         event_id(5))

    def test_disagreeing_duplicate_outcomes_stay_unresolved(self):
        first = defect(1, outcome="resolved", no_check_reason="vendor_outage")
        second = defect(2, outcome="unresolved")

        summary = recovery_summary({"id": JOB_ID, "stage": "merged"}, [first, second])

        self.assertIsNone(summary["defects"][0]["outcome"])
        self.assertIn("outcome", summary["defects"][0]["conflicts"])
        self.assertEqual(summary["unresolved_defects"], ["defect-1"])

    def test_links_follow_current_correction_and_require_a_check_record(self):
        record = observation(2, "recovery_decision", {"failure_id": "failure-2"},
                             evidence=[EVIDENCE], supersedes=event_id(1))
        report = defect(3, correction_event_id=event_id(1), regression_check_event_id=event_id(2))

        row = recovery_summary({"id": JOB_ID, "stage": "merged"}, [record, report])["defects"][0]

        self.assertEqual(row["correction"]["event_id"], event_id(2))
        self.assertIsNone(row["regression_check"])
        self.assertIsNone(row["no_check_reason"])
        self.assertEqual(row["regression_status"], "pending")

    def test_links_follow_the_full_current_correction_ancestry(self):
        record = observation(3, "recovery_decision", {"failure_id": "failure-2"},
                             evidence=[EVIDENCE], supersedes=event_id(2))
        record["superseded_event_ids"] = [event_id(1), event_id(2)]
        report = defect(4, correction_event_id=event_id(1))

        row = recovery_summary({"id": JOB_ID, "stage": "merged"}, [record, report])["defects"][0]

        self.assertEqual(row["correction"]["event_id"], event_id(3))
        self.assertEqual(row["correction_event_id"], event_id(1))
