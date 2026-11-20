import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory" / "lib"))
from decision_card import render_decision_card
from guardrails import evaluate_guardrails
from observations import append_event, load_events


CURRENT = "a" * 40
OLDER = "b" * 40
REPORT = {"path": "round-2/checks.json", "sha256": "c" * 64}
ROOT = "factory/jobs/012-owner-card"
SOURCE = "https://github.com/example/factory/issues/12"
CANDIDATE_URL = f"https://github.com/example/factory/commit/{CURRENT}"
DEMO = {"url": "https://example.com/demo/2", "verified": True}


def event(kind, data, candidate=CURRENT, evidence=(), source="owner", origin="real"):
    return {
        "kind": kind, "data": data, "candidate_sha": candidate, "round": 2,
        "evidence": list(evidence), "source": {"kind": source, "record_id": kind},
        "origin": origin,
    }


def guardrails(*requirements):
    return {
        "candidate": {
            "candidate_sha": CURRENT, "base_sha": "d" * 40,
            "spec_digest": "e" * 64, "round": 2,
        },
        "requirements": list(requirements),
    }


def requirement(identifier, status, required=True, evidence=(), label=None):
    return {
        "id": identifier, "status": status, "required": required,
        "evidence": list(evidence), "event_ids": [], "label": label,
    }


class TestDecisionCard(unittest.TestCase):
    def test_links_verified_criteria_and_distinguishes_review_requirements(self):
        job = {
            "id": "012-owner-card", "stage": "review", "round": 2,
            "candidate_url": CANDIDATE_URL, "source_url": SOURCE,
        }
        card = render_decision_card(job, [], guardrails(
            requirement("criterion:export", "pass", evidence=[REPORT], label="Export a report"),
            requirement("test:unit", "fail", evidence=[REPORT]),
            requirement("review:code", "stale", evidence=[REPORT]),
            requirement("review:security", "missing"),
            requirement("review:eval", "not_required", required=False),
        ), {}, verified_source_url=SOURCE)

        self.assertIn(f"- **Candidate** — `{CURRENT}`\n", card)
        self.assertNotIn(CANDIDATE_URL, card)
        self.assertIn("Export a report (`export`) — pass (required)", card)
        self.assertIn(f"Local evidence reference `{ROOT}/round-2/checks.json`", card)
        self.assertNotIn("](factory/jobs/", card)
        self.assertIn(f"[GitHub issue]({SOURCE})", card)
        self.assertIn("test:unit` — fail (required)", card)
        self.assertIn("review:code` — stale (required)", card)
        self.assertIn("review:security` — missing (required)", card)
        self.assertIn("review:eval` — not required (optional)", card)
        self.assertIn("observation ledger unavailable", card)
        self.assertIn("Resolve failed evidence for `test:unit`", card)

    def test_job_tracker_fields_cannot_authenticate_themselves(self):
        job = {"id": "012-owner-card", "source_url": SOURCE, "candidate_url": CANDIDATE_URL}
        card = render_decision_card(job, [], guardrails(), {})
        self.assertIn("Tracker link unavailable", card)
        self.assertNotIn("https://github.com/", card)

    def test_assertions_and_tests_cannot_stand_in_for_verified_criterion_or_demo(self):
        events = [
            event("criterion", {"criterion_id": "export", "verdict": "pass"}, evidence=[REPORT]),
            event("demo", {"demo_id": "old", "criterion_id": "export", "verdict": "pass"}, OLDER, [DEMO]),
        ]
        card = render_decision_card({"id": "012-card", "stage": "review"}, events, guardrails(
            requirement("criterion:export", "missing"),
            requirement("test:unit", "pass", evidence=[REPORT]),
            requirement("review:code", "pass"),
        ), {})

        self.assertIn("export` — unverified (required)", card)
        self.assertIn("test:unit` — pass (required)", card)
        self.assertIn("review:code` — unverified (required)", card)
        self.assertIn("No working-software demonstration available for the current candidate.", card)
        self.assertIn("old` — stale", card)
        self.assertIn(OLDER, card)

    def test_job_metrics_use_the_local_ledger_and_ignore_unverified_job_urls(self):
        job = {"id": "012-card"}
        record = "https://example.com/metrics/12"
        for suffix in (
            "", "#elapsed-time",
            "?X-Amz-Signature=fixture-signature",
            "#access_token=fixture-token",
            "#access_token%3Dfixture-token",
        ):
            with self.subTest(suffix=suffix):
                card = render_decision_card({**job, "metrics_url": record + suffix}, [], guardrails(), {})
                self.assertIn("observation ledger unavailable", card)
                self.assertNotIn(record, card)
                self.assertNotIn("job.json", card)

        effort = event("owner_effort", {"minutes": 0})
        card = render_decision_card(job, [effort], guardrails(), {})
        self.assertIn("Local observation ledger `factory/jobs/012-card/observations/`", card)
        self.assertIn("`ls -1 -- factory/jobs/012-card/observations`", card)

    def test_groups_current_and_stale_evidence_under_their_own_results(self):
        earlier_report = {"path": "round-1/checks.json", "sha256": "f" * 64}
        checked = requirement("test:unit", "pass", evidence=[earlier_report, REPORT])
        checked["observations"] = [
            {"candidate_sha": OLDER, "round": 1, "status": "stale", "verdict": "pass",
             "verified": True, "evidence": [earlier_report]},
            {"candidate_sha": CURRENT, "round": 2, "status": "pass", "verdict": "pass",
             "verified": True, "evidence": [REPORT]},
        ]
        card = render_decision_card(
            {"id": "012-card"}, [], guardrails(checked), {},
        )

        current = next(line for line in card.splitlines() if "test:unit` — pass" in line)
        self.assertIn("Stale evidence", card)
        stale = next(line for line in card.splitlines() if "Stale evidence" in line)
        self.assertIn("Local evidence reference `factory/jobs/012-card/round-2/checks.json`", current)
        self.assertNotIn("round-1/checks.json", current)
        self.assertIn(OLDER, stale)
        self.assertIn("Local evidence reference `factory/jobs/012-card/round-1/checks.json`", stale)
        self.assertIn("observation ledger unavailable", card)

    def test_demo_and_limitations_stay_independent_of_owner_and_technical_decisions(self):
        events = [
            event("demo", {"demo_id": "export-demo", "criterion_id": "export", "verdict": "pass"}, evidence=[DEMO]),
            event("limitation", {"code": "csv-only"}, evidence=[REPORT]),
            dict(event("owner_decision", {"disposition": "accepted", "material_follow_up": False}),
                 occurred_at="2026-11-19T07:15:00Z"),
        ]
        card = render_decision_card(
            {"id": "012-card", "stage": "review", "owner_changes": False}, events, guardrails(), {},
        )

        self.assertIn("export-demo` — owner-reported demonstration", card)
        self.assertIn("Evidence link unavailable", card)
        self.assertNotIn("https://example.com/demo/2", card)
        self.assertIn("No working-software demonstration available for the current candidate.", card)
        self.assertIn("`csv-only`", card)
        self.assertIn("Owner decision** — accepted", card)
        self.assertIn("Technical decision** — pending", card)

    def test_blocked_job_keeps_recovery_question_as_a_single_next_step(self):
        job = {"id": "012-card", "stage": "needs-human", "owner_changes": False}
        card = render_decision_card(job, [], {"requirements": []}, {
            "retry_exhausted": True, "owner_question": "Can the CSV export ship first?",
        })

        self.assertIn("needs-human", card)
        self.assertIn("Candidate** — unknown", card)
        self.assertIn("Owner decision** — unrecorded", card)
        self.assertIn("Owner active time** — unknown", card)
        self.assertIn("Elapsed time** — unknown", card)
        self.assertEqual(card.partition("## Next step\n\n")[2].strip(), "Can the CSV export ship first?")

    def test_shows_zero_and_bounds_elapsed_time_from_recorded_history(self):
        job = {
            "id": "012-card", "stage": "needs-human", "owner_minutes": 50,
            "history": [
                {"at": "2026-11-19T08:00:00Z", "stage": "intake"},
                {"at": "2026-11-19T09:00:00Z", "stage": "needs-human"},
            ],
        }
        events = [
            event("owner_effort", {"activity": "review", "minutes": 0, "rework": False}, None),
            event("owner_effort", {"activity": "recovery", "minutes": 18, "rework": True}, None, origin="controlled"),
        ]
        card = render_decision_card(job, events, guardrails(), {})
        self.assertIn("Owner active time** — 0 minutes recorded (partial coverage)", card)
        self.assertIn("Elapsed time** — 60 minutes from intake to the last recorded stage", card)

        job["owner_minutes"] = 0
        standalone = render_decision_card(job, [], guardrails(), {})
        self.assertIn("Owner active time** — 0 minutes (legacy aggregate)", standalone)

    def test_damaged_correction_makes_the_current_owner_decision_and_coverage_unknown(self):
        job = {"id": "012-card", "stage": "needs-human", "round": 2}

        def append(folder, observation, supersedes=None):
            return append_event(folder, {
                **observation, "job_id": job["id"], "schema_version": 1,
                "event_id": str(uuid4()), "supersedes_event_id": supersedes,
            })

        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / job["id"]
            folder.mkdir()
            (folder / "job.json").write_text("{}", encoding="utf-8")
            original = append(folder, event("owner_decision", {"disposition": "accepted"}))
            correction = append(folder, event("owner_decision", {"disposition": "rejected"}), original["event_id"])
            append(folder, event("owner_effort", {"activity": "review", "minutes": 4}, None))
            append(folder, event("owner_effort_coverage", {"scope": "job", "complete": True}, None))
            path = folder / "observations" / f"{correction['event_id']}.json"
            path.write_bytes(b"{")

            events, diagnostics = load_events(folder)
            result = evaluate_guardrails(folder, job, events, [], {**guardrails()["candidate"], "diagnostics": diagnostics})
            card = render_decision_card(job, events, result, {"owner_question": "Can we retry?"})

        self.assertIn("**Evidence collection incomplete**", card.partition("## Acceptance criteria")[0])
        self.assertIn(path.name, card)
        self.assertIn("Owner decision** — current status unknown · last readable record reported accepted", card)
        self.assertIn("Owner active time** — 4 minutes in readable observations (coverage unknown)", card)
        self.assertIn("Limitations unknown", card)
        self.assertIn("Current demonstration status is unknown", card)
        self.assertEqual(card.partition("## Next step\n\n")[2].strip(),
                         "Resolve the evidence collection diagnostics and regenerate this card before acting on the decision.")


if __name__ == "__main__":
    unittest.main()
