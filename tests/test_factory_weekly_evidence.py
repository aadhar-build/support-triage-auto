"""Exercise the offline, sanitized weekly export in disposable repositories."""

import hashlib
import json
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID, ROOT

sys.path.insert(0, str(ROOT / "factory" / "lib"))
import evidence_bundle
from weekly_evidence import _page, export_week


class WeeklyEvidenceTests(FactoryCLI, unittest.TestCase):
    OPTIONS = ("--weekly", "2026-11-19", "--timezone", "Asia/Kolkata",
               "--as-of", "2099-11-27T10:00:00Z", "--defect-window-days", "14")

    def setUp(self):
        super().setUp()
        self.output = self.root.resolve() / "weekly"

    def export(self, *extra, options=OPTIONS, success=True):
        result = self.cli("evidence-bundle", *options, "--output", str(self.output), *extra, success=success)
        if not success:
            self.assertNotEqual(result.returncode, 0)
            return result
        page = (self.output / "index.html").read_text()
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        return page, manifest

    def observe(self, kind, data, *, origin="real", actor="owner", job=JOB_ID):
        self.cli("job", "observe", job, "-", input=json.dumps({
            "schema_version": 1, "event_id": str(uuid4()), "job_id": job, "kind": kind,
            "origin": origin, "occurred_at": "2026-11-19T08:00:00Z",
            "source": {"kind": actor, "record_id": "SECRET-source"}, "data": data,
        }))

    def test_defined_empty_week_preserves_unavailable_rates(self):
        page, data = self.export()
        self.assertEqual(data["period"]["start_utc"], "2026-11-18T18:30:00Z")
        self.assertEqual(data["period"]["end_utc"], "2026-11-25T18:30:00Z")
        self.assertEqual(data["period"]["as_of"], "2099-11-27T10:00:00Z")
        self.assertEqual(data["defect_window_days"], 14)
        self.assertEqual(data["metrics"]["delivery"]["count"], 0)
        self.assertIsNone(data["metrics"]["delivery"]["independently_verified_count"])
        self.assertEqual(data["metrics"]["delivery"]["first_gate"],
                         {"numerator": 0, "eligible": 0, "covered": 0, "unknown": 0, "percent": None})
        self.assertIn("Rate unavailable", page)
        self.assertIn('id="weekly-headlines"', page)
        self.assertIn("No submissions with a recorded date in this week", page)
        self.assertNotIn("<meter", page)
        self.assertIn("<details", page)
        self.assertIn("1 day", page)
        self.assertIn("unit and scope", page)
        self.assertIn("Owner review pending", page)

    def test_dashboard_charts_observed_counts_without_a_partial_acceptance_rate(self):
        self.write_job()
        pending = self.job_dir.with_name("002-pending")
        pending.mkdir()
        pending_job = json.loads(self.state.read_bytes())
        pending_job.update(id=pending.name, issue=2)
        (pending / "job.json").write_text(json.dumps(pending_job))
        context = {"submitted_at": "2026-11-19T07:30:00Z", "workflow_mode": "direct"}
        self.observe("task_context", context)
        self.observe("task_context", context, job=pending.name)
        self.observe("owner_decision", {"disposition": "accepted", "material_follow_up": False})
        self.observe("owner_effort", {"minutes": 0, "activity": "review", "rework": False})

        page, data = self.export()
        self.assertEqual(data["metrics"]["acceptance"]["rate"],
                         {"numerator": 1, "eligible": 2, "covered": 1, "unknown": 1, "percent": None})
        self.assertIn('id="report-details"', page)
        overview, details = page.split('id="report-details"', 1)
        self.assertIn("1 recorded as accepted among 2 eligible submissions", overview)
        self.assertIn("1 observed, 1 unknown", overview)
        self.assertIn("0 min", overview)
        self.assertIn("0/2 jobs fully covered", overview)
        self.assertIn("Submission outcomes", overview)
        self.assertRegex(overview, r'<meter[^>]*max="2"[^>]*value="1"')
        self.assertIn("Evidence coverage", overview)
        self.assertNotIn("50%", overview)
        self.assertNotIn("<script", page)
        self.assertNotIn(' style="', page)
        self.assertIn("Historical baseline", details)

    def test_dashboard_labels_missing_owner_outcome_as_unknown(self):
        self.write_job()
        self.observe("task_context", {"submitted_at": "2026-11-19T07:30:00Z", "workflow_mode": "direct"})
        page, data = self.export()
        self.assertEqual(data["metrics"]["acceptance"]["rate"],
                         {"numerator": 0, "eligible": 1, "covered": 0, "unknown": 1, "percent": None})
        overview = page.split('id="report-details"', 1)[0]
        self.assertIn("0 recorded as accepted among 1 eligible submission", overview)
        self.assertIn("0 observed, 1 unknown", overview)
        self.assertNotIn(">0%</span>", overview)

    def test_full_requirement_bar_does_not_hide_jobs_without_an_inventory(self):
        self.write_job()
        _, data = self.export()
        data["metrics"]["guardrails"].update(covered_requirements=2, eligible_requirements=2,
                                             jobs_without_requirement_inventory=1)
        overview = _page(data).decode().split('id="report-details"', 1)[0]
        self.assertIn("Verified inventoried requirements", overview)
        self.assertIn("2 of 2", overview)
        self.assertIn("1 active job has no requirement inventory", overview)

    def test_seven_day_defect_window_excludes_deliveries_still_in_follow_up(self):
        self.write_job(stage="merged")
        options = ("--weekly", "2026-11-19", "--timezone", "UTC", "--as-of", "2026-11-25T10:00:00Z",
                   "--defect-window-days", "7")

        page, data = self.export(options=options)
        self.assertEqual(data["metrics"]["escaped_defects"]["immature"], 1)
        self.assertEqual(data["metrics"]["escaped_defects"]["rate"]["eligible"], 0)
        overview = page.split('id="report-details"', 1)[0]
        self.assertIn("7-day follow-up", overview)
        self.assertIn("1 still in follow-up", overview)
        self.assertIn("0 confirmed affected among 0 mature deliveries", overview)

        self.output.rename(self.output.with_name("immature"))
        boundary = (*options[:5], "2026-11-26T07:15:00Z", *options[6:])
        page, data = self.export(options=boundary)
        self.assertEqual(data["metrics"]["escaped_defects"]["rate"],
                         {"numerator": 0, "eligible": 1, "covered": 0, "unknown": 1, "percent": None})
        overview = page.split('id="report-details"', 1)[0]
        self.assertIn("0 confirmed affected among 1 mature delivery", overview)
        self.assertIn("0 observed, 1 unknown. 0 still in follow-up", overview)
        self.assertNotIn(">0%</span>", overview)

    def test_unknown_settings_leave_affected_metrics_unknown(self):
        options = ("--weekly", "2026-11-19", "--as-of", "2099-11-27T10:00:00Z")
        page, data = self.export(options=options)
        self.assertIsNone(data["metrics"])
        self.assertIsNone(data["defect_window_days"])
        self.assertFalse(data["period"]["time_zone_configured"])
        self.assertIn("weekly cohort is unknown", page)
        self.assertNotIn('id="weekly-headlines"', page)
        self.assertNotIn("<meter", page)
        self.output.rename(self.output.with_name("unwindowed"))
        page, data = self.export(options=(*options, "--timezone", "UTC"))
        self.assertIsNone(data["metrics"]["escaped_defects"]["rate"])
        self.assertIn("Follow-up window unset", page)

    def test_reports_typed_zero_coverage_and_cost_without_source_strings(self):
        self.write_job(title="SECRET-title", owner_minutes=18, cost_usd=0)
        (self.job_dir / "spec.md").write_text("## Acceptance Criteria\n1. [SECRET-criterion] SECRET-spec\n")
        self.observe("owner_effort", {"minutes": 0, "activity": "review", "rework": False})
        self.observe("owner_effort", {"minutes": 97, "activity": "correction", "rework": True},
                     origin="controlled", actor="fixture")
        self.observe("task_context", {"task_type": "SECRET-type", "task_size": "SECRET-size"})
        for kind, amount, units in (
                ("subscription_charge", "0", {"currency": "USD", "unit": "SECRET-unit"}),
                ("incremental_charge", "1.25", {"currency": "KEY"}),
                ("api_equivalent_estimate", "2.50", {"currency": "USD"}),
                ("subscription_usage", 3, {"unit": "SECRET-usage"})):
            self.observe("usage_cost", {"cost_type": kind, "amount": amount, "billing_item": f"SECRET-{kind}",
                                        **units}, actor="billing")
        controlled = self.job_dir.with_name("002-fixture-unexported-controlled")
        controlled.mkdir()
        controlled_job = json.loads(self.state.read_bytes())
        controlled_job.update(id=controlled.name, origin="controlled", stage="merged")
        (controlled / "job.json").write_text(json.dumps(controlled_job))
        broken = self.job_dir.with_name("003-fixture-unexported-broken")
        broken.mkdir()
        (broken / "job.json").write_text("SECRET-damaged")
        self.job_dir.with_name("005-fixture-unexported-missing-state").mkdir()
        self.job_dir.with_name("-fixture-unexported-invalid-id").mkdir()
        undated = self.job_dir.with_name("004-fixture-unexported-undated")
        undated.mkdir()
        (undated / "job.json").write_text(json.dumps({"id": undated.name, "title": "SECRET-undated"}))

        page, data = self.export()
        metrics = data["metrics"]
        self.assertEqual(data["source_coverage"]["controlled_jobs_excluded"], 1)
        self.assertEqual(data["source_coverage"]["unreadable_jobs_omitted"], 2)
        self.assertEqual(data["source_coverage"]["invalid_job_directories"], 1)
        self.assertEqual(data["source_coverage"]["jobs_without_start_time"], 1)
        self.assertEqual(metrics["active_jobs"], 1)
        self.assertEqual(metrics["owner_effort"]["known_minutes"], 0)
        self.assertEqual(metrics["owner_effort"]["covered_jobs"], 0)
        self.assertEqual(metrics["owner_effort"]["unknown_jobs"], 1)
        self.assertIsNone(metrics["owner_effort"]["rework_total_minutes"])
        self.assertIsNone(metrics["guardrails"]["effective_total"])
        self.assertTrue(metrics["small_sample"])
        self.assertEqual(metrics["cohorts"]["task_type"]["coverage"],
                         {"eligible": 1, "covered": 1, "unknown": 0})
        self.assertEqual(metrics["cohorts"]["task_type"]["groups"], [])
        costs = {item["cost_type"]: item for item in metrics["costs"]["categories"]}
        self.assertEqual(costs["subscription_charge"]["totals"][0]["amount"], "0")
        self.assertEqual(costs["incremental_charge"]["totals"][0]["amount"], "1.25")
        self.assertEqual(costs["api_equivalent_estimate"]["totals"][0]["amount"], "2.50")
        self.assertEqual(costs["subscription_usage"]["totals"][0]["amount"], "3")
        for private in ("SECRET", "fixture-unexported", JOB_ID, "fixture/software-factory", "https://", '"KEY"'):
            self.assertNotIn(private, page + json.dumps(data))
        self.assertIn("Actual subscription charge", page)
        self.assertIn("Estimated API equivalent", page)

    def test_selected_evidence_is_finite_local_projection_with_explicit_gaps(self):
        self.write_job(stage="merged", title="SECRET-title")
        round_path = self.job_dir / "round-1"
        round_path.mkdir()
        inputs = {
            "round-1/checks.md": b"GATE: PASS\n\n## Summary\nSECRET-gate",
            "round-1/review-code.md": b"VERDICT: CHANGES\n\nSECRET-review",
            "round-1/review-security.md": None,
            "decision.md": b"SECRET-unrecognized",
            "final-checks.md": b"GATE: PASS\n\n## Summary\nSECRET-stale",
        }
        flags = []
        for path, raw in inputs.items():
            if raw is not None:
                (self.job_dir / path).write_bytes(raw)
            digest = hashlib.sha256(raw or b"").hexdigest()
            if path == "final-checks.md":
                digest = "f" * 64
            flags.extend(("--weekly-evidence", f"{JOB_ID}:{path}@{digest}"))
        page, data = self.export(*flags)
        rows = data["selected_evidence"]
        self.assertEqual([row["artifact_status"] for row in rows],
                         ["projected", "projected", "missing", "withheld", "stale"])
        linked = [row for row in rows if "file" in row]
        self.assertEqual([row["reported_verdict"] for row in linked], ["pass", "changes"])
        delivery = data["metrics"]["delivery"]
        self.assertEqual(delivery["count"], 1)
        self.assertEqual(delivery["jobs"][0]["job"], rows[0]["job"])
        self.assertIsNone(delivery["independently_verified_count"])
        gaps = data["metrics"]["guardrails"]["gaps"]
        gate_gap = next(row for row in gaps if row["requirement"] == "Round gate")
        self.assertEqual(gate_gap["selected_receipts"], [linked[0]["file"]])
        self.assertIn("GitHub merge verification is unavailable", page)
        self.assertEqual(len(list(self.output.iterdir())), 4)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for row in linked:
            self.assertEqual(row["source_timing"], "current_at_export")
            self.assertEqual(row["week_association"], "unverified")
            path = self.output / row["file"]
            raw = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])
            self.assertEqual(json.loads(raw)["verdict"], row["reported_verdict"])
            self.assertIn(f'href="{path.name}"', page)
            self.assertNotIn(b"SECRET", raw)
        self.assertIn("default-src 'none'", page)
        self.assertIn("week or by the reporting cutoff is unverified", page)
        for private in ("SECRET", JOB_ID, "round-1/", "decision.md", flags[1].split("@")[-1]):
            self.assertNotIn(private, page + json.dumps(data))

    def test_refuses_out_of_cohort_selection_and_unsafe_destinations(self):
        self.write_job(origin="controlled")
        selected = f"{JOB_ID}:decision.md@{'0' * 64}"
        self.export("--weekly-evidence", selected, success=False)
        self.assertFalse(self.output.exists())
        self.export(JOB_ID, success=False)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        self.export(success=False)
        self.assertEqual(list(self.output.iterdir()), [])
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        for destination in (alias / "export", self.job_dir / "export", self.root / ".git" / "export"):
            with self.subTest(path=destination):
                self.output = destination
                self.export(success=False)
                self.assertFalse(destination.exists())
        alternate = self.root / "FACTORY" / "JOBS"
        if alternate.is_dir():
            self.output = alternate / "export"
            self.export(success=False)
            self.assertFalse(self.output.exists())

    def test_fresh_install_reserves_case_variants_of_the_job_store_name(self):
        store = self.factory / "jobs"
        self.assertFalse(store.exists())
        self.output = self.root.resolve() / "factory" / "JOBS"
        self.export(success=False)
        self.assertFalse(self.output.exists())
        self.assertFalse(store.exists())

    def test_typed_delivery_with_future_stage_is_sanitized_and_marks_active_coverage_incomplete(self):
        self.write_job(stage="merged", history=[{"at": "2099-11-28T08:00:00Z", "stage": "merged"}])
        self.observe("task_context", {"started_at": "2026-11-19T07:00:00Z",
                                      "ended_at": "2026-11-19T08:00:00Z",
                                      "status": "merged"})
        page, data = self.export()
        self.assertIsNone(data["metrics"]["active_jobs"])
        self.assertEqual(data["metrics"]["observed_active_jobs"], 0)
        self.assertEqual(data["metrics"]["delivery"]["count"], 1)
        self.assertEqual(data["metrics"]["delivery"]["jobs"], [{"job": 1, "selected_receipts": []}])
        self.assertEqual(data["source_coverage"]["recorded_deliveries_outside_active_cohort"], 1)
        self.assertNotIn(JOB_ID, page + json.dumps(data))
        self.assertIn("Active job coverage is incomplete", page)

    def test_source_changes_during_write_roll_back_the_bundle(self):
        self.write_job()
        report = self.job_dir / "decision.md"
        content = b"APPROVE\n"
        report.write_bytes(content)
        selected = f"{JOB_ID}:decision.md@{hashlib.sha256(content).hexdigest()}"
        state = self.state.read_bytes()
        controlled = {**json.loads(state), "origin": "controlled"}
        write = evidence_bundle._write_file
        for file, replacement in ((report, b"ESCALATE\n"), (self.state, json.dumps(controlled).encode())):
            with self.subTest(file=file.name):
                def change_source(directory, name, data):
                    if name == "manifest.json":
                        file.write_bytes(replacement)
                    return write(directory, name, data)

                with patch.object(evidence_bundle, "_write_file", change_source):
                    with self.assertRaisesRegex(evidence_bundle.BundleError, "changed during export"):
                        export_week(self.root.resolve(), date(2026, 11, 19),
                                    datetime(2099, 11, 27, tzinfo=timezone.utc), "UTC", 14, self.output, [selected])
                self.assertFalse(self.output.exists())
                report.write_bytes(content)
                self.state.write_bytes(state)
