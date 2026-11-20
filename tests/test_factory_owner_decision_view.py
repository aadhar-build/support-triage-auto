"""Exercise the opt-in owner view against disposable synthetic jobs."""

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.test_factory_evidence_bundle import append_event, bundle, receipt
from tests.test_factory_observation_integration import FactoryCLI, JOB_ID


class OwnerDecisionViewTests(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name).resolve() / "owner"
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "synthetic candidate")
        self.candidate = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("branch", f"factory/{JOB_ID}", self.candidate)
        self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        self.write_job(stage="needs-human", origin="real", title="<script>synthetic owner title</script>",
                       owner_minutes=0)
        self.spec = self.job_dir / "spec.md"
        (self.job_dir / "evidence").mkdir()

    def observe(self, kind, data, *, path=None, body=None, origin="real", source="controller", **bindings):
        refs = []
        if path:
            (self.job_dir / path).write_bytes(body)
            refs.append({"path": path, "sha256": hashlib.sha256(body).hexdigest()})
        return append_event(self.job_dir, {
            "schema_version": 1, "job_id": JOB_ID, "event_id": str(uuid4()), "kind": kind,
            "source": {"kind": source, "record_id": "synthetic"}, "origin": origin,
            "candidate_sha": self.candidate, "round": 1, "data": data, "evidence": refs, **bindings,
        })

    def selected_gate(self, name="round-1/checks.md", content=b"GATE: FAIL\n\n## Summary\nfixture logs\n"):
        path = self.job_dir / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return f"{name}@{hashlib.sha256(content).hexdigest()}"

    def run_private(self, *args, success=True):
        return self.cli("evidence-bundle", JOB_ID, "--private-owner-view", "--output", str(self.out),
                        *args, success=success)

    def page(self):
        return (self.out / "index.html").read_text()

    def test_owner_view_joins_reordered_ids_to_receipts_and_escapes_literal_text(self):
        self.spec.write_text("## Acceptance criteria\n30. [zed] Zed <img src=x onerror=alert(1)>\n"
                             "10. [alpha] Alpha [label](javascript:alert(1))\n")
        self.observe("check_evidence", {"check_id": "criterion:alpha", "verdict": "pass"},
                     path="evidence/check-1.json", body=receipt())
        self.observe("criterion", {"criterion_id": "zed", "verdict": "fail"},
                     path="evidence/check-2.json", body=receipt("criterion", "fail"))
        self.observe("demo", {"demo_id": "walkthrough", "criterion_id": "alpha", "verdict": "pass"},
                     path="evidence/check-3.json", body=receipt("demo"))
        self.observe("limitation", {"code": "synthetic-known-limitation"})
        self.observe("usage_cost", {"cost_type": "api_equivalent_estimate", "amount": "2.50",
                                    "currency": "USD", "billing_item": "synthetic-estimate"})
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False}, source="owner",
                     occurred_at="2026-11-19T07:20:00Z")
        self.run_private("--include", "evidence/check-1.json", "--include", "evidence/check-2.json",
                         "--include", "evidence/check-3.json", "--gate", self.selected_gate())
        page = self.page()
        rows = re.findall(r"<tr>(.*?)</tr>", page, re.S)
        zed, = [row for row in rows if "Zed &lt;img" in row]
        alpha, = [row for row in rows if "Alpha [label]" in row]
        self.assertIn('href="receipt-002.json"', zed)
        self.assertNotIn('href="receipt-001.json"', zed)
        self.assertIn('href="receipt-001.json"', alpha)
        self.assertIn("(alpha)", alpha)
        self.assertIn("(zed)", zed)
        self.assertIn('data-label="Local receipt validation">unknown</td>', alpha)
        self.assertIn('href="receipt-003.json"', page)
        self.assertIn('href="gate-round-1.json"', page)
        self.assertIn("synthetic-known-limitation", page)
        self.assertIn("2.50 USD", page)
        self.assertIn("0 minutes (legacy aggregate)", page)
        self.assertIn("15 minutes from intake", page)
        self.assertIn(self.candidate, page)
        self.assertIn("Owner reported accepted", page)
        self.assertIn("Technical decision", page)
        self.assertIn("<th>Local receipt validation</th>", page)
        self.assertIn("Private owner view", page)
        self.assertEqual(page.count("<h2>Next owner action</h2>"), 1)
        self.assertRegex(page, r"<h2>Next owner action</h2>\n<p>[^<]*\(zed\)")
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img ", page)
        self.assertNotIn('href="javascript', page)
        self.assertNotIn("fixture logs", page)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.out / "index.html").stat().st_mode & 0o777, 0o600)
        self.assertTrue(set(re.findall(r'href="([^"]+)"', page)) <= {
            "manifest.json", "receipt-001.json", "receipt-002.json", "receipt-003.json", "gate-round-1.json",
        })
        manifest = (self.out / "manifest.json").read_text()
        self.assertNotIn("synthetic owner title", manifest)
        self.assertNotIn("synthetic-known-limitation", manifest)
        self.assertNotIn("criterion:alpha", manifest)
        self.out = self.out.with_name("sanitized")
        self.cli("evidence-bundle", JOB_ID, "--overview", "--output", str(self.out))
        self.assertNotIn("Zed", self.page())
        self.assertNotIn("Private owner view", self.page())

    def test_private_view_withholds_stale_sources_and_controlled_observations(self):
        self.spec.write_text("## Acceptance criteria\n1. [current] Current criterion\n")
        self.observe("criterion", {"criterion_id": "current", "verdict": "pass"},
                     path="evidence/check-1.json", body=receipt("criterion"), candidate_sha="a" * 40)
        self.observe("owner_decision", {"disposition": "accepted"}, origin="controlled", source="owner")
        self.observe("limitation", {"code": "controlled-canary"}, origin="controlled", source="fixture")
        self.run_private("--include", "evidence/check-1.json")
        page = self.page()
        self.assertIn("stale", page)
        self.assertIn("Owner decision unrecorded", page)
        self.assertIn("No working-software demonstration available", page)
        self.assertNotIn("controlled-canary", page)
        self.assertNotIn('href="receipt-001.json"', page)
        self.assertEqual({file.name for file in self.out.iterdir()}, {"manifest.json", "index.html"})

    def test_positional_criterion_requires_current_candidate_round_and_spec_to_link_wording(self):
        original = b"## Acceptance criteria\n1. Older wording\n"
        current = b"## Acceptance criteria\n1. Replacement wording\n"
        self.spec.write_bytes(original)
        self.observe("criterion", {"criterion_id": "1", "verdict": "pass"},
                     path="evidence/check-1.json", body=receipt("criterion"))
        self.observe("criterion", {"criterion_id": "1", "verdict": "pass"},
                     path="evidence/check-3.json", body=receipt("criterion"), candidate_sha=None, round=None)
        self.spec.write_bytes(current)
        self.observe("criterion", {"criterion_id": "1", "verdict": "fail"},
                     path="evidence/check-2.json", body=receipt("criterion", "fail"),
                     spec_digest=hashlib.sha256(current).hexdigest())
        self.run_private("--include", "evidence/check-1.json", "--include", "evidence/check-2.json",
                         "--include", "evidence/check-3.json")
        rows = re.findall(r"<tr>(.*?)</tr>", self.page(), re.S)
        match, = [row for row in rows if "Replacement wording" in row]
        unmatched, = [row for row in rows if "current wording association unknown" in row]
        self.assertIn('href="receipt-002.json"', match)
        self.assertIn("spec matched", match)
        self.assertIn('data-label="Local receipt validation">unknown</td>', match)
        self.assertNotIn('href="receipt-001.json"', match)
        self.assertNotIn('href="receipt-003.json"', match)
        self.assertIn("spec unrecorded", unmatched)
        self.assertIn("candidate unrecorded", unmatched)
        self.assertIn('href="receipt-001.json"', unmatched)
        self.assertIn('href="receipt-003.json"', unmatched)

    def test_positional_receipt_does_not_inherit_another_receipts_spec_binding(self):
        spec = b"## Acceptance criteria\n1. Numbered criterion\n"
        self.spec.write_bytes(spec)
        content = receipt("criterion")
        digest = hashlib.sha256(content).hexdigest()
        references = [
            {"path": "evidence/check-1.json", "sha256": digest, "spec_digest": hashlib.sha256(spec).hexdigest()},
            {"path": "evidence/check-2.json", "sha256": digest},
        ]
        for item in references:
            (self.job_dir / item["path"]).write_bytes(content)
        self.observe("criterion", {"criterion_id": "1", "verdict": "pass"}, evidence=references)
        self.run_private("--include", "evidence/check-1.json", "--include", "evidence/check-2.json")
        rows = re.findall(r"<tr>(.*?)</tr>", self.page(), re.S)
        match, = [row for row in rows if "Numbered criterion" in row]
        self.assertIn('href="receipt-001.json"', match)
        self.assertNotIn('href="receipt-002.json"', match)
        self.assertIn("spec matched", match)
        unmatched, = [row for row in rows if "current wording association unknown" in row]
        self.assertIn('href="receipt-002.json"', unmatched)
        self.assertIn("spec unrecorded", unmatched)

    def test_duplicate_stable_criterion_ids_refuse_private_output(self):
        self.spec.write_text("## Acceptance criteria\n1. [same] First\n2. [same] Second\n")
        result = self.run_private(success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate acceptance criterion", result.stderr)
        self.assertFalse(self.out.exists())

    def test_private_view_uses_one_cutoff_for_receipts_and_metric_dates(self):
        self.spec.write_text("## Acceptance criteria\n1. [current] Current criterion\n")
        self.observe("demo", {"demo_id": "future-demo", "verdict": "pass"},
                     path="evidence/check-1.json", body=receipt("demo"), occurred_at="2099-01-01T00:00:00Z")
        self.observe("task_context", {"ended_at": "2099-01-01T00:00:00Z"})
        declined = self.run_private("--include", "evidence/check-1.json", success=False)
        self.assertNotEqual(declined.returncode, 0)
        self.assertFalse(self.out.exists())
        self.run_private()
        page = self.page()
        self.assertIn("15 minutes from intake", page)
        self.assertNotIn("future-demo", page)
        self.assertNotIn("2099", page)

    def test_private_output_refuses_unsafe_or_controlled_combinations(self):
        relative_destination = self.root / "private-report"
        for args in (("--overview",), ("--weekly", "2026-11-16"), ("--output", str(relative_destination))):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_private(*args, success=False).returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertFalse(relative_destination.exists())
        self.write_job(origin="controlled")
        self.assertNotEqual(self.run_private(success=False).returncode, 0)
        self.assertFalse(self.out.exists())

    def test_private_output_refuses_a_sibling_linked_checkout(self):
        linked = self.out.with_name("sibling checkout")
        self.git("worktree", "add", "--detach", str(linked), self.candidate)
        self.out = linked / "private-owner-view"
        result = self.run_private(success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside all factory checkouts", result.stderr)
        self.assertFalse(self.out.exists())

    def test_private_output_refuses_an_unverifiable_worktree_registry(self):
        linked = self.out.with_name("registered-checkout")
        self.git("worktree", "add", "--detach", str(linked), self.candidate)
        linked.rename(linked.with_name("moved-checkout"))
        result = self.run_private(success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot verify the local factory checkout locations", result.stderr)
        self.assertFalse(self.out.exists())

    def test_private_output_refuses_a_moved_checkout_with_its_old_path_recreated(self):
        linked = self.out.with_name("registered-checkout")
        moved = self.out.with_name("moved-checkout")
        self.git("worktree", "add", "--detach", str(linked), self.candidate)
        linked.rename(moved)
        linked.mkdir()
        self.out = moved / "private-owner-view"
        result = self.run_private(success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside a Git checkout", result.stderr)
        self.assertFalse(self.out.exists())

    def submitted_job(self):
        def history(minute, stage):
            return {"at": f"2026-11-19T07:{minute:02d}:00Z", "stage": stage, "note": ""}

        self.write_job(stage="needs-human", origin="real", history=[
            history(0, "intake"), history(10, "approve"), history(15, "needs-human"),
        ])

    def assert_owner_decisions(self, *, card_status, private_status, outcome, next_step=None):
        with self.subTest(surface="Markdown card"):
            card = self.cli("job", "card", JOB_ID).stdout
            self.assertIn(f"**Owner decision** — {card_status}", card)
            self.assertIn("**Technical decision** — pending human action", card)
            self.assertNotIn("Review the recorded outcome before starting follow-up work.", card)
            if next_step:
                self.assertEqual(card.partition("## Next step\n\n")[2].strip(), next_step)
        with self.subTest(surface="private HTML"):
            self.run_private()
            page = self.page()
            self.assertIn(f"<dt>Owner decision</dt><dd>{private_status}", page)
            self.assertIn("<dt>Technical decision</dt><dd>pending human action", page)
            self.assertNotIn("Review the recorded outcome before starting follow-up work.", page)
            if next_step:
                self.assertIn(f"<h2>Next owner action</h2>\n<p>{next_step}</p>", page)
        with self.subTest(surface="weekly scorecard"):
            result = self.cli("scorecard", "--weekly", "2026-11-16", "--timezone", "UTC", "--json")
            scorecard = json.loads(result.stdout)
            accepted = int(outcome == "accepted")
            self.assertEqual(scorecard["acceptance"]["rate"], {
                "numerator": accepted, "eligible": 1, "covered": accepted, "unknown": 1 - accepted,
                "percent": 100 if accepted else None,
            })
            self.assertEqual(scorecard["outcomes"], {outcome: 1})
            if outcome == "unknown":
                self.assertEqual(scorecard["acceptance"]["unobserved"], 1)

    def test_owner_decisions_use_occurrence_and_leave_undated_reports_unresolved_until_corrected(self):
        self.submitted_job()
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:30:00Z")
        self.observe("owner_decision", {"disposition": "rejected"}, source="owner",
                     occurred_at="2026-11-19T07:25:00Z")
        self.observe("owner_decision", {"disposition": "rejected"}, source="owner", round=2)
        self.assert_owner_decisions(card_status="accepted", private_status="Owner reported accepted",
                                    outcome="accepted")

        undated = self.observe("owner_decision", {"disposition": "rejected"}, source="owner")
        self.out = self.out.with_name("undated")
        self.assert_owner_decisions(card_status="current status unknown · owner report dates incomplete",
                                    private_status="Current owner decision unknown · owner report dates incomplete",
                                    outcome="unknown")

        self.observe("owner_decision", {"disposition": "rejected"}, source="owner",
                     occurred_at="2026-11-19T07:20:00Z", supersedes_event_id=undated["event_id"])
        self.out = self.out.with_name("corrected")
        self.assert_owner_decisions(card_status="accepted", private_status="Owner reported accepted",
                                    outcome="accepted")

    def test_unbound_owner_decision_cannot_decide_a_code_candidate(self):
        self.submitted_job()
        self.observe("owner_decision", {"disposition": "rejected"}, source="owner",
                     occurred_at="2026-11-19T07:30:00Z", candidate_sha=None, round=None)
        self.assert_owner_decisions(card_status="current status unknown",
                                    private_status="Current owner decision unknown", outcome="needs-human")

    def test_unreadable_correction_withholds_readable_acceptance_from_all_reports(self):
        self.submitted_job()
        accepted = self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                                source="owner", occurred_at="2026-11-19T07:30:00Z")
        correction = self.observe("owner_decision", {"disposition": "rejected"},
                                  source="owner", occurred_at="2026-11-19T07:30:00Z",
                                  supersedes_event_id=accepted["event_id"])
        damaged = self.job_dir / "observations" / f"{correction['event_id']}.json"
        damaged.write_bytes(b"{")

        card = self.cli("job", "card", JOB_ID).stdout
        self.assertIn("**Owner decision** — current status unknown", card)
        self.assertIn("Resolve the evidence collection diagnostics", card)
        private = self.run_private(success=False)
        self.assertNotEqual(private.returncode, 0)
        self.assertIn("Observation coverage is incomplete", private.stderr)
        self.assertFalse(self.out.exists())

        weekly = self.cli("scorecard", "--weekly", "2026-11-16", "--timezone", "UTC", "--json")
        scorecard = json.loads(weekly.stdout)
        self.assertTrue(any(damaged.name in message for message in scorecard["diagnostics"][JOB_ID]))
        self.assertEqual(scorecard["outcomes"], {"unknown": 1})
        self.assertEqual(scorecard["acceptance"]["unobserved"], 1)
        self.assertEqual(scorecard["acceptance"]["rate"], {
            "numerator": 0, "eligible": 1, "covered": 0, "unknown": 1, "percent": None,
        })

    def test_job_level_owner_decision_applies_to_a_direct_non_code_job(self):
        self.submitted_job()
        self.git("update-ref", "-d", f"refs/heads/factory/{JOB_ID}")
        self.observe("task_context", {"task_type": "planning", "workflow_mode": "direct"},
                     occurred_at="2026-11-19T07:00:00Z", candidate_sha=None, round=None)
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:30:00Z", candidate_sha=None, round=None)
        self.assert_owner_decisions(card_status="accepted", private_status="Owner reported accepted",
                                    outcome="accepted", next_step="Review the recorded owner acceptance and any material follow-up.")

    def test_owner_decisions_outside_the_review_window_are_ineligible(self):
        self.submitted_job()
        for name, occurred_at in (("future", "2099-11-19T07:30:00Z"),
                                  ("pre-submission", "2026-11-19T07:10:00Z")):
            with self.subTest(name=name):
                self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                             source="owner", occurred_at=occurred_at)
                self.out = self.out.with_name(name)
                self.assert_owner_decisions(card_status="unrecorded", private_status="Owner decision unrecorded",
                                            outcome="needs-human")

    def test_installed_merge_handoff_sets_the_review_boundary_for_all_reports(self):
        self.write_job(stage="needs-human", origin="real", history=[
            {"at": "2026-11-19T07:00:00Z", "stage": "intake", "note": ""},
            {"at": "2026-11-19T07:10:00Z", "stage": "merging", "note": ""},
            {"at": "2026-11-19T07:15:00Z", "stage": "needs-human",
             "note": "merge requires human verification of the PR head and base"},
        ])
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:14:00Z")
        self.assert_owner_decisions(card_status="unrecorded", private_status="Owner decision unrecorded",
                                    outcome="needs-human")

        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:15:00Z")
        self.out = self.out.with_name("handoff")
        self.assert_owner_decisions(card_status="accepted", private_status="Owner reported accepted",
                                    outcome="accepted")

    def test_mismatched_spec_or_nested_candidate_cannot_establish_owner_acceptance(self):
        self.submitted_job()
        self.spec.write_text("## Acceptance criteria\n1. Current wording\n")
        for name, binding in (
            ("spec", {"spec_digest": "b" * 64}),
            ("reference", {"evidence": [{"url": "https://example.invalid/proof", "candidate_sha": "b" * 40}]}),
        ):
            with self.subTest(name=name):
                self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                             source="owner", occurred_at="2026-11-19T07:30:00Z", **binding)
                self.out = self.out.with_name(name)
                self.assert_owner_decisions(card_status="current status unknown",
                                            private_status="Current owner decision unknown", outcome="needs-human")

    def test_newer_unknown_owner_decision_withholds_prior_acceptance(self):
        self.submitted_job()
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:30:00Z")
        self.observe("owner_decision", {}, source="owner", occurred_at="2026-11-19T07:40:00Z")
        self.assert_owner_decisions(card_status="current status unknown",
                                    private_status="Current owner decision unknown", outcome="unknown")

    def test_direct_coding_job_still_requires_a_bound_candidate(self):
        self.submitted_job()
        self.git("update-ref", "-d", f"refs/heads/factory/{JOB_ID}")
        self.observe("task_context", {"task_type": "coding", "workflow_mode": "direct"},
                     occurred_at="2026-11-19T07:00:00Z", candidate_sha=None, round=None)
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False},
                     source="owner", occurred_at="2026-11-19T07:30:00Z", candidate_sha=None, round=None)
        self.assert_owner_decisions(card_status="current status unknown",
                                    private_status="Current owner decision unknown", outcome="needs-human",
                                    next_step="Capture the exact candidate for evidence review.")

    def test_coverage_without_typed_effort_preserves_the_legacy_total(self):
        self.write_job(stage="needs-human", origin="real", owner_minutes=75)
        self.observe("owner_effort_coverage", {"scope": "job", "complete": True})
        self.run_private()
        self.assertIn("75 minutes (legacy aggregate)", self.page())
        self.assertNotIn("0 minutes recorded", self.page())

    def test_typed_gate_report_does_not_hide_a_separate_unselected_native_report(self):
        self.observe("check_evidence", {"check_id": "checks", "verdict": "pass"},
                     path="evidence/check-1.json", body=receipt())
        self.selected_gate()
        self.run_private("--include", "evidence/check-1.json")
        rows = re.findall(r"<tr>(.*?)</tr>", self.page(), re.S)
        gate, = [row for row in rows if ">Round gate<" in row]
        self.assertIn('href="receipt-001.json"', gate)
        self.assertIn("Native report available locally · unselected", gate)
        self.assertNotIn('href="gate-round-1.json"', gate)

    def test_locally_validated_gate_pass_leaves_independent_verification_unknown(self):
        self.write_job(stage="review", origin="real", reviewers={})
        self.git("worktree", "add", "-q", str(self.root / ".worktrees" / JOB_ID), f"factory/{JOB_ID}")
        spec = b"## Acceptance criteria\n1. Fixture criterion\n"
        self.spec.write_bytes(spec)
        gate = self.selected_gate(content=(
            f"GATE: PASS\nCANDIDATE: {self.candidate}\nBASE: {self.candidate}\n"
            f"SPEC-DIGEST: {hashlib.sha256(spec).hexdigest()}\n\n## Summary\nsynthetic private gate log\n"
        ).encode("ascii"))

        self.run_private("--gate", gate)

        page = self.page()
        gate_row, = [row for row in re.findall(r"<tr>(.*?)</tr>", page, re.S) if ">Round gate<" in row]
        self.assertIn('href="gate-round-1.json"', gate_row)
        self.assertIn('data-label="Local receipt validation">pass</td>', gate_row)
        self.assertIn("Independent verification is unknown", page)
        self.assertNotIn('data-label="Independent verification"', gate_row)
        self.assertNotIn("synthetic private gate log", page)
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["rows"][0]["claim_verification"], "unverified")

    def test_private_snapshot_revalidation_removes_output_when_spec_or_state_changes(self):
        original_spec = b"## Acceptance criteria\n1. [a] Fixture A\n"
        initial_state = self.state.read_bytes()
        real_write = bundle._write_file
        for changed_path, initial, changed in (
            (self.spec, original_spec, original_spec.replace(b"Fixture A", b"Fixture B")),
            (self.state, initial_state, initial_state.replace(b"synthetic owner title", b"changed title")),
        ):
            with self.subTest(path=changed_path.name):
                changed_path.write_bytes(initial)
                self.spec.write_bytes(original_spec)
                self.state.write_bytes(initial_state)

                def changing_write(directory, name, content):
                    result = real_write(directory, name, content)
                    if name == "index.html":
                        changed_path.write_bytes(changed)
                    return result

                with patch.object(bundle, "_write_file", changing_write), self.assertRaises(bundle.BundleError):
                    bundle.export_job(self.root, JOB_ID, self.out, [], private_owner_view=True)
                self.assertFalse(self.out.exists())
