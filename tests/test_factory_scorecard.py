import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
from scorecard_report import weekly_report, render_weekly_report


def at(day):
    return f"2026-11-{day:02}T12:00:00Z"


def event(kind, number, day=4, **data):
    return dict(event_id=str(number), kind=kind, occurred_at=at(day),
                recorded_at=at(day), source={"kind": "owner", "record_id": str(number)},
                origin="real", data=data)


def record(name="one", history=None, events=(), **fields):
    if history is None:
        history = [(1, "intake"), (3, "build"), (4, "approve"),
                   (4, "needs-human"), (5, "merged")]
    return dict(job=dict(id=name, stage=history[-1][1] if history else "build",
                         history=[dict(at=at(d), stage=s) for d, s in history], **fields),
                events=[dict(e, job_id=name, schema_version=1) for e in events], merged_candidate_sha="a" * 40)


def report(*records, timezone="UTC", start="2026-11-02", cutoff=at(25), window=10):
    return weekly_report(records, start, cutoff, timezone, window)


class WeeklyScorecard(unittest.TestCase):
    def test_empty_defined_week_and_unconfigured_week(self):
        empty = report()
        self.assertEqual(empty["delivered"]["count"], 0)
        self.assertEqual(empty["acceptance"]["rate"]["eligible"], 0)
        self.assertIsNone(empty["acceptance"]["rate"]["percent"])
        self.assertIsNone(report(record(), window=None)["escaped_defects"]["rate"])
        unknown = report(record(), timezone=None)
        self.assertIsNone(unknown["delivered"])
        self.assertEqual(unknown["unwindowed"]["jobs"], 1)
        self.assertIn("time zone", render_weekly_report(unknown).lower())

    def test_local_boundaries_and_as_of_visibility(self):
        item = record(history=[(1, "intake"), (9, "merged")])
        item["job"]["history"][-1]["at"] = "2026-11-09T02:00:00Z"
        self.assertEqual(report(item, timezone="America/New_York")["delivered"]["count"], 1)
        self.assertEqual(report(item)["delivered"]["count"], 0)
        self.assertEqual(report(item, cutoff=at(7))["delivered"]["count"], 0)

    def test_acceptance_uses_submissions_and_explicit_owner_decisions(self):
        accepted = record("a", events=[event("owner_decision", 1, 10, disposition="accepted",
                                             material_follow_up=False)])
        accepted["events"][0].update(candidate_sha="a" * 40, round=1)
        accepted["merged_round"] = 1
        pending = record("p", history=[(1, "intake"), (4, "approve"), (4, "needs-human")])
        merged = record("m", owner_changes=False)
        failed = record("f", history=[(3, "intake"), (4, "failed")])
        untimed = record("legacy", history=[])
        result = report(accepted, pending, merged, failed, untimed)
        self.assertEqual(result["acceptance"]["rate"], dict(numerator=1, eligible=3, covered=1,
                                                             unknown=2, percent=None))
        self.assertEqual(result["acceptance"]["pending"], 1)
        self.assertEqual(result["acceptance"]["unobserved"], 1)
        self.assertEqual(result["outcomes"]["failed"], 1)
        self.assertIn("legacy", result["undated_jobs"])

    def test_corrected_effort_partial_and_comparable_per_merge(self):
        entries = [event("owner_effort", 1, activity="preparation", minutes=12, rework=False),
                   event("owner_effort", 2, activity="correction", minutes=0, rework=True),
                   event("owner_effort", 3, activity="preparation", minutes=7, rework=False),
                   event("owner_effort_coverage", 4, day=6, scope="job", complete=True)]
        entries[2].update(supersedes_event_id="1", recorded_at=at(10))
        full = record("a", events=entries, owner_minutes=999)
        partial = record("b", events=[event("owner_effort", 5, activity="correction",
                                           minutes=4, rework=True)])
        early = report(full, cutoff=at(8))
        self.assertEqual(early["owner_effort"]["known_minutes"], 12)
        effort = report(full, partial)["owner_effort"]
        self.assertEqual(effort["activities"]["preparation"], 7)
        self.assertEqual(effort["activities"]["correction"], 4)
        self.assertEqual(effort["rework_minutes"], 4)
        self.assertEqual((effort["known_minutes"], effort["covered_jobs"], effort["eligible_jobs"]),
                         (11, 1, 2))
        self.assertEqual(effort["per_merge_median"], dict(minutes=7, sample=1, excluded=1))
        self.assertIsNone(report(record())["owner_effort"]["known_minutes"])

    def test_mature_window_verified_affected_distinct_and_incomplete_negative(self):
        defects = [event("defect", i, 8, origin="escaped", defect_id=f"d{i}", discovered_at=at(8))
                   for i in (1, 2)]
        for obs in defects:
            obs["candidate_sha"] = "a" * 40
        clean = event("observation_coverage", 3, 20, scope="escaped_defects",
                      window_start=at(5), window_end=at(15), complete=True)
        clean["candidate_sha"] = "a" * 40
        affected = record("a", events=defects)
        covered = record("b", events=[clean])
        unknown = record("c", escaped=False)
        result = report(affected, covered, unknown)["escaped_defects"]
        self.assertEqual(result["rate"], dict(numerator=1, eligible=3, covered=2, unknown=1,
                                              percent=None))
        self.assertEqual(report(affected, covered)["escaped_defects"]["rate"]["percent"], 50)
        self.assertEqual(report(affected, cutoff=at(14))["escaped_defects"]["immature"], 1)
        affected["merged_candidate_sha"] = "b" * 40
        self.assertEqual(report(affected)["escaped_defects"]["rate"]["covered"], 0)

    def test_usage_units_and_explicit_legacy_zero(self):
        def cost(i, typ, amount, unit, currency):
            return event("usage_cost", i, cost_type=typ, amount=amount, unit=unit,
                         currency=currency, billing_item=str(i))
        item = record(events=[cost(1, "subscription_usage", "100", "tokens", None),
                              cost(2, "incremental_charge", "0.00", "currency", "USD"),
                              cost(3, "api_equivalent_estimate", "7.20", "currency", "USD")])
        result = report(item, record("b", cost_usd=0, owner_minutes=0))
        amounts = {r["cost_type"]: r["amount"] for r in result["costs"]["totals"]}
        self.assertEqual(amounts, dict(subscription_usage="100", incremental_charge="0.00",
                                       api_equivalent_estimate="7.20"))
        self.assertEqual(result["legacy"]["b"]["cost_usd"], 0)
        self.assertIn("1 day", render_weekly_report(result))
        self.assertIn("small sample", render_weekly_report(result).lower())

    def test_matched_pairs_require_complete_comparable_observations(self):
        def sample(name, mode, minutes, size="s"):
            return record(name, events=[event("task_context", 1, workflow_mode=mode, task_size=size,
                                              task_type="docs", matched_task_key="pair"),
                                        event("owner_effort", 2, activity="review", minutes=minutes,
                                              rework=False),
                                        event("owner_effort_coverage", 3, day=6, scope="job", complete=True)])
        a, b = sample("a", "factory", 2), sample("b", "direct", 5)
        self.assertEqual(report(a, b)["matched_tasks"]["pairs"][0]["factory_minus_direct_minutes"], -3)
        self.assertEqual(report(a, sample("b", "direct", 5, "m"))["matched_tasks"]["pairs"], [])
        self.assertEqual(report(a, b)["task_cohorts"], [])

    def test_cohort_metrics_use_each_outcomes_eligible_population(self):
        context = event("task_context", 1, task_type="docs", task_size="small")
        accepted = event("owner_decision", 2, disposition="accepted", material_follow_up=False)
        accepted.update(candidate_sha="a" * 40, round=1)
        charge = event("usage_cost", 5, cost_type="incremental_charge", unit="currency",
                       currency="USD", amount="0.00", billing_item="shared-invoice")
        known = record("known", events=[context, accepted, charge,
                       event("owner_effort", 3, activity="correction", minutes=5, rework=True),
                       event("owner_effort_coverage", 4, day=6, scope="job", complete=True)])
        known["merged_round"] = 1
        known["guardrails"] = dict(coverage=dict(eligible=2, covered=1, proven=0), requirements=[
            dict(id="test", required=True, status="fail"), dict(id="review", required=True, status="unverified")])
        others = [record(f"other-{i}", events=[context]) for i in range(7)]
        others[0]["events"].append(dict(accepted, source={"kind": "reviewer"}))
        unclassified_effort = event("owner_effort", 8, activity="review", minutes=3, rework=None)
        others[1]["events"].extend([unclassified_effort, event("owner_effort_coverage", 9, 6, scope="job", complete=True)])
        merged_only = record("merged-only", history=[(5, "merged")], events=[context])
        ongoing = record("ongoing", history=[(3, "intake")], events=[context])
        separate = record("separate", events=[dict(context, data=dict(task_type="docs", task_size="large")), charge])
        result = report(known, *others, merged_only, ongoing, separate, window=None)
        self.assertEqual(len(result["task_cohorts"]), 1)
        cohort = result["task_cohorts"][0]
        self.assertEqual(cohort["jobs"], 10)
        self.assertEqual(cohort.get("acceptance", {}).get("rate"),
                         dict(numerator=1, eligible=8, covered=1, unknown=7, percent=None))
        self.assertEqual(cohort["merged_jobs"], 9)
        self.assertEqual(cohort["lead_hours"], dict(median=96, sample=8, excluded=1))
        self.assertEqual(cohort["agent_elapsed_minutes"], dict(median=4320, sample=8, excluded=1))
        self.assertEqual(cohort["owner_minutes"], dict(median=4, sample=2, excluded=8))
        self.assertEqual(cohort["owner_effort"]["rework_minutes"], 5)
        self.assertIsNone(cohort["owner_effort"]["rework_total_minutes"])
        self.assertEqual(cohort["owner_effort"]["covered_jobs"], 2)
        self.assertEqual(cohort["owner_effort"]["rework_covered_jobs"], 1)
        self.assertEqual(cohort["guardrails"]["covered_requirements"], 1)
        self.assertEqual(cohort["guardrails"]["proven_requirements"], 0)
        self.assertEqual(cohort["guardrails"]["jobs_without_requirement_inventory"], 9)
        self.assertIsNone(cohort["guardrails"]["effective_total"])
        self.assertEqual(cohort["cost_coverage"]["incremental_charge"], dict(covered=1, eligible=10, unknown=9))
        self.assertEqual(cohort["cost_coverage"]["subscription_charge"]["unknown"], 10)
        self.assertIsNone(cohort["escaped_defects"]["rate"])
        self.assertIsNone(cohort["interruptions"]["verified_safe"]["percent"])
        self.assertIsNone(cohort["escalations"]["precision"]["percent"])
        summary = render_weekly_report(result).split("**Task type and size**")[-1]
        self.assertIn("1/8", summary)
        self.assertIn("7 unknown", summary)
        self.assertIn("Median whole-job owner time 4.0 minutes (2/10 measured, 8 unknown)", summary)
        self.assertIn("8/9", summary)
        self.assertIn("1/10", summary)

    def test_cohort_inventory_shows_small_and_unclassified_groups_at_cutoff(self):
        odd_label = "[fake](https://example.test)"
        labeled = record(events=[event("task_context", 1, task_type=odd_label, task_size="small")])
        late = record("late", events=[event("task_context", 1, 12, task_type="docs", task_size="small")])
        missing_size = record("missing", events=[event("task_context", 1, task_type="docs")])
        controlled = record("controlled", events=[event("task_context", 1, task_type="docs", task_size="small")],
                            origin="controlled")
        fake_label = event("task_context", 1, task_type="demo", task_size="small")
        fake_label["origin"] = "controlled"
        result = report(labeled, late, missing_size, controlled, record("real", events=[fake_label]), cutoff=at(8))
        self.assertEqual(result.get("task_cohort_coverage"), dict(
            covered=1, eligible=4, unknown=3, minimum_jobs=10,
            small_groups=[dict(task_type=odd_label, task_size="small", jobs=1)]))
        self.assertEqual(result["task_cohorts"], [])
        summary = render_weekly_report(result)
        self.assertIn("1/4 active jobs", summary)
        self.assertIn("3 unknown", summary)
        self.assertIn("Small sample", summary)
        self.assertIn(f"`{odd_label}`", summary)
        self.assertNotIn("**Task type and size**", render_weekly_report(report(labeled, timezone=None)))

    def test_cohort_recovery_counts_only_linked_effort_and_distinguishes_reported_recurrence(self):
        label = event("task_context", 1, task_type="docs", task_size="small")
        linked = [event("interruption", 2, interruption_id="i", started_at=at(3), outcome="unresolved"),
                  event("owner_effort", 3, activity="recovery", minutes=0, rework=True),
                  event("owner_effort", 4, activity="recovery", minutes=22, rework=True)]
        linked.extend(event("recovery_decision", n, failure_id=f"f{n}", cause="timeout",
                            interruption_event_ids=["2"], owner_effort_event_ids=["3"]) for n in (5, 6))
        result = report(record("first", events=[label, *linked]),
                        *(record(f"j-{i}", events=[label]) for i in range(9)))
        cohort = result["task_cohorts"][0]
        self.assertEqual(cohort["interruptions"].get("known_owner_minutes"), 0)
        self.assertEqual(cohort["interruptions"]["linked_effort_entries"], 1)
        self.assertEqual(cohort["recovery"]["reported_recurring"], [dict(cause="timeout", count=2)])
        self.assertEqual(cohort["recovery"]["recurring"], [])
        self.assertEqual([r["cause"] for r in cohort["recovery"]["records"]], [None, None])

    def test_cost_subtotals_allocate_unique_items_once_and_withhold_shared_or_conflicting_items(self):
        label = event("task_context", 1, task_type="docs", task_size="small")
        def charge(n, bill, amount, kind="incremental_charge", currency="USD"):
            row = event("usage_cost", n, cost_type=kind, unit="currency", currency=currency,
                        amount=amount, billing_item=bill)
            row["source"]["provider"] = "synthetic"
            return row
        events = [charge(2, "one", "2.20"), charge(3, "one", "2.20"), charge(4, "shared", "30.00"),
                  charge(5, "disputed", "5"), charge(6, "disputed", "8"),
                  charge(7, "estimated", "4.00", "api_equivalent_estimate"),
                  charge(8, "eur", "0.00", currency="EUR")]
        prior = record("prior", history=[(1, "intake"), (1, "merged")], events=[charge(9, "shared", "30.00")])
        result = report(prior, record("first", events=[label, *events]),
                        *(record(f"j-{i}", events=[label]) for i in range(9)))
        cohort = result["task_cohorts"][0]
        totals = {(r["cost_type"], r["currency"]): r for r in cohort.get("cost_totals", [])}
        self.assertIn(("incremental_charge", "USD"), totals)
        usd = totals["incremental_charge", "USD"]
        self.assertEqual((usd["known_amount"], usd["allocated_items"], usd["shared_items"],
                          usd["conflicting_items"]), ("2.20", 1, 1, 1))
        self.assertIsNone(usd["recorded_amount"])
        self.assertEqual(totals["api_equivalent_estimate", "USD"]["recorded_amount"], "4.00")
        self.assertEqual(totals["incremental_charge", "EUR"]["recorded_amount"], "0.00")

    def test_week_and_cohort_share_billing_identity_and_exact_decimal_subtotals(self):
        label = event("task_context", 1, task_type="docs", task_size="small")
        def money(n, amount, currency="USD", bill=None):
            row = event("usage_cost", n, cost_type="incremental_charge", unit="currency",
                        currency=currency, amount=amount, billing_item=bill, window_start=at(3), window_end=at(4))
            row["source"] = dict(kind="billing", provider="test-provider", record_id="source-window")
            return row
        def totals(*entries):
            result = report(record("first", events=[label, *entries]), *(record(f"j-{i}", events=[label]) for i in range(9)))
            return ({r["currency"]: r["amount"] for r in result["costs"]["totals"]},
                    {r["currency"]: r["recorded_amount"] for r in result["task_cohorts"][0]["cost_totals"]})
        weekly, cohort = totals(money(2, "2.20"), money(3, "2.20"),
                                money(4, "1234567890123456789012345678.12", bill="big"),
                                money(5, "0.000000000000000001", bill="tiny"))
        exact = {"USD": "1234567890123456789012345680.320000000000000001"}
        self.assertEqual(weekly, exact)
        self.assertEqual(cohort, exact)
        weekly, cohort = totals(money(6, "3", bill="disputed"), money(7, "4", "EUR", "disputed"))
        self.assertEqual(weekly, {"USD": None, "EUR": None})
        self.assertEqual(cohort, weekly)

    def test_independent_type_and_size_views_keep_partially_labeled_jobs(self):
        for dimension, other in (("task_type", "task_size"), ("task_size", "task_type")):
            def labeled(name, **labels):
                return record(name, events=[event("task_context", 1, **labels)])
            rows = [labeled(f"j-{i}", **{dimension: "shared", other: "a" if i < 6 else "b"}) for i in range(12)]
            rows.extend([labeled("only-one", **{other: "a"}), labeled("only-other", **{dimension: "different"}),
                         dict(labeled("demo", **{dimension: "shared", other: "a"}), origin="controlled")])
            result = report(*rows)
            self.assertEqual(result["task_cohorts"], [])
            view = result.get("task_cohorts_by", {}).get(dimension, {})
            self.assertEqual(view.get("coverage", {}).get("covered"), 13)
            self.assertEqual(view["coverage"]["unknown"], 1)
            self.assertEqual(view["coverage"]["eligible"], 14)
            self.assertEqual(view["cohorts"][0][dimension], "shared")
            self.assertEqual(view["cohorts"][0]["jobs"], 12)
            self.assertEqual(sorted(r["jobs"] for r in result["task_cohort_coverage"]["small_groups"]), [6, 6])
            self.assertIn("shared · 12 active", render_weekly_report(result))

    def test_unsubmitted_dispositions_and_partial_zero_rework_remain_visible(self):
        label = event("task_context", 1, task_type="docs", task_size="small")
        free = record("free", events=[label, event("owner_effort_coverage", 2, 6, scope="job", complete=True)])
        others = [record(f"other-{i}", events=[label]) for i in range(6)]
        terminal = [record(stage, history=[(1, "intake"), (4, stage)], events=[label])
                    for stage in ("failed", "abandoned", "rejected")]
        cohort = report(free, *others, *terminal)["task_cohorts"][0]
        self.assertEqual(cohort.get("outcomes"), dict(merged=7, failed=1, abandoned=1, rejected=1))
        self.assertEqual(cohort["acceptance"]["rate"]["eligible"], 7)
        self.assertEqual(cohort["acceptance"]["no_recorded_submission"], 3)
        effort = cohort["owner_effort"]
        self.assertEqual(effort["known_minutes"], 0)
        self.assertEqual(effort["rework_minutes"], 0)
        self.assertIsNone(effort["rework_total_minutes"])

    def test_ratings_and_acceptance_require_owner_source(self):
        judgment = event("owner_decision", 1, disposition="accepted", material_follow_up=False)
        judgment["source"]["kind"] = "reviewer"
        escalation = event("escalation", 2, request_id="r", rating="necessary")
        escalation["source"]["kind"] = "controller"
        r = report(record(events=[judgment, escalation]))
        self.assertEqual(r["acceptance"]["rate"]["covered"], 0)
        self.assertEqual(r["escalations"]["rated_requests"], 0)
        self.assertEqual(r["escalations"]["unrated"], 1)

    def test_undated_ongoing_and_recurring_recovery_retain_links(self):
        events = [event("recovery_decision", i, failure_id=f"f{i}", cause="timeout",
                        outcome="unresolved") for i in (1, 2)]
        for e in events:
            e["evidence"] = [dict(path="sanitized.txt", sha256="0" * 64)]
        r = report(record("001-undated", history=[]), record("002-other", events=events))
        self.assertEqual(r["undated_statuses"]["001-undated"], "build")
        self.assertEqual(r["recovery"]["recurring"], [])
        self.assertIsNone(r["recovery"]["records"][0]["cause"])
        repeated = r["recovery"]["reported_recurring"]
        self.assertEqual(repeated[0]["count"], 2)
        self.assertIn("reported cause", render_weekly_report(r).lower())
        self.assertIn("timeout", render_weekly_report(r))
        self.assertIn("sanitized.txt", render_weekly_report(r))

    def test_late_escalation_judgment_keeps_original_request_period(self):
        item = record(events=[event("escalation", 1, request_id="r", rating=None),
                              event("escalation", 2, 10, request_id="r", rating="necessary")])
        self.assertEqual(report(item)["escalations"]["rated_requests"], 1)
        self.assertEqual(report(item, cutoff=at(8))["escalations"]["unrated"], 1)
        self.assertEqual(report(item, start="2026-11-09")["escalations"]["rated_requests"], 0)

    def test_suspected_escape_leaves_complete_followup_unknown(self):
        suspect = event("defect", 1, 6, defect_id="d", origin="suspected_escaped", discovered_at=at(6))
        suspect["candidate_sha"] = "a"*40
        covered = event("observation_coverage", 2, 20, scope="escaped_defects",
                        complete=True, window_start=at(5), window_end=at(16))
        item = record(events=[suspect, covered])
        self.assertIsNone(report(item)["escaped_defects"]["rate"]["percent"])

    def test_cost_conflicts_and_partial_billing_intervals(self):
        usage = event("usage_cost", 1, cost_type="incremental_charge", unit="currency",
                      amount="7.00", currency="USD", billing_item="shared")
        other = event("usage_cost", 2, cost_type="incremental_charge", unit="currency",
                      amount="8.00", currency="USD", billing_item="shared")
        month = event("usage_cost", 3, cost_type="subscription_charge", unit="currency",
                      amount="20.00", currency="USD", window_start=at(1), window_end=at(24))
        cost = report(record("a",events=[usage, month]), record("b",events=[other]))["costs"]
        self.assertIsNone(cost["totals"][0]["amount"])
        self.assertTrue(cost["totals"][0]["conflict"])
        self.assertEqual(len(cost["unattributed"]), 1)

    def test_period_requires_evidence_for_clean_guardrails(self):
        clean = dict(scope="guardrails", verified=True, complete=True,
                     window_start=at(1), window_end=at(10))
        job = record()
        job["guardrails"] = dict(incidents=dict(records=[], coverage_windows=[clean]))
        self.assertEqual(report(job)["guardrails"]["effective_total"], 0)
        job["guardrails"]["diagnostics"] = ["unstable snapshot"]
        self.assertIsNone(report(job)["guardrails"]["effective_total"])

    def test_effort_coverage_scope_must_include_every_entry(self):
        cost = event("owner_effort", 1, activity="correction", minutes=6, rework=True, day=8)
        coverage = event("owner_effort_coverage", 2, day=11, scope="owner_effort",
                         complete=True, window_start=at(1), window_end=at(5))
        r = report(record(events=[cost, coverage]))
        self.assertEqual(r["owner_effort"]["per_merge_median"]["sample"], 0)

    def test_escaped_window_requires_candidate_bound_coverage_and_classified_defects(self):
        missing_origin = event("defect", 1, 6, defect_id="x", origin=None, discovered_at=at(6))
        missing_origin["candidate_sha"] = "a" * 40
        coverage = event("observation_coverage", 2, 20, scope="escaped_defects",
                         complete=True, window_start=at(5), window_end=at(15))
        coverage["candidate_sha"] = "b" * 40
        for rows in ([coverage], [coverage, missing_origin]):
            self.assertEqual(report(record(events=rows))["escaped_defects"]["rate"]["unknown"], 1)
        coverage["candidate_sha"] = "a" * 40
        self.assertEqual(report(record(events=[coverage, missing_origin]))["escaped_defects"]["rate"]["unknown"], 1)
        self.assertEqual(report(record(events=[coverage]))["escaped_defects"]["rate"]["percent"], 0)
        self.assertIsNone(report(dict(record(events=[coverage]), merged_candidate_sha=None))[
            "escaped_defects"]["rate"]["percent"])

    def test_legacy_effort_with_completion_declaration_remains_unallocated(self):
        coverage = event("owner_effort_coverage", 1, day=6, scope="job", complete=True)
        result = report(record(events=[coverage], owner_minutes=24))
        self.assertEqual(result["legacy"]["one"]["owner_minutes"], 24)
        effort = result["owner_effort"]
        self.assertIsNone(effort["known_minutes"])
        self.assertEqual(effort["covered_jobs"], 0)
        self.assertIsNone(effort["per_merge_median"]["minutes"])
        self.assertEqual(effort["per_merge_median"]["excluded"], 1)

    def test_charge_for_inactive_job_uses_its_billing_window(self):
        charge = event("usage_cost", 1, 15, cost_type="subscription_charge", unit="currency",
                       amount="12.50", currency="USD", billing_item="xyz",
                       window_start=at(3), window_end=at(6))
        prior = record("prior", history=[(1, "intake"), (1, "merged")], events=[charge])
        result = report(prior, record("active"))
        self.assertEqual(result["active_jobs"], ["active"])
        costs = result["costs"]
        self.assertEqual(costs["totals"][0]["amount"], "12.50")
        self.assertEqual(costs["coverage"]["subscription_charge"]["eligible_jobs"], 1)
        self.assertEqual(costs["coverage"]["subscription_charge"]["jobs_with_amount"], 0)
        self.assertEqual(costs["coverage"]["subscription_charge"]["other_jobs_with_amount"], 1)
        self.assertEqual(report(prior, cutoff=at(12))["costs"]["totals"], [])

    def test_rework_unknown_amounts_and_flags_remain_visible(self):
        rows = [event("owner_effort", 1, activity="preparation", minutes=8, rework=False),
                event("owner_effort", 2, activity="review", minutes=17, rework=None),
                event("owner_effort", 3, activity="correction", minutes=None, rework=True)]
        result = report(record(events=rows))
        effort = result["owner_effort"]
        self.assertEqual(effort["rework_minutes"], 0)
        self.assertIsNone(effort["rework_total_minutes"])
        self.assertEqual(effort["unclassified_minutes"], 17)
        self.assertEqual(effort["unknown_rework_amount_entries"], 1)
        self.assertIn("17 known minutes", render_weekly_report(result))

    def test_interruption_minutes_use_deduplicated_explicit_links(self):
        rows = [event("interruption", 1, interruption_id="i", started_at=at(3), outcome="unresolved"),
                event("owner_effort", 2, activity="recovery", minutes=7, rework=True),
                event("owner_effort", 3, activity="recovery", minutes=2, rework=True)]
        item = record(events=rows)
        self.assertIsNone(report(item)["interruptions"]["known_owner_minutes"])
        rows.extend(event("recovery_decision", n, failure_id=f"f{n}", interruption_event_ids=["1"],
                          owner_effort_event_ids=["3"]) for n in (4, 5))
        self.assertEqual(report(record(events=rows))["interruptions"]["known_owner_minutes"], 2)

    def test_owner_disposition_must_bind_to_the_reviewed_candidate_and_round(self):
        decision = event("owner_decision", 1, 6, disposition="accepted", material_follow_up=False)
        for sha, rnd, expected in [("b"*40, 2, None), ("a"*40, 1, None), ("a"*40, 2, 100)]:
            decision.update(candidate_sha=sha, round=rnd)
            item = dict(record(events=[decision], round=2), merged_round=2)
            self.assertEqual(report(item)["acceptance"]["rate"]["percent"], expected)

    def test_merged_round_is_independent_of_the_current_checkout_round(self):
        owner = event("owner_decision", 1, 6, disposition="accepted", material_follow_up=False)
        owner.update(candidate_sha="a" * 40, round=2)
        item = record(events=[owner], round=2)
        item.update(merged_at=at(5), merge_evidence={"merged_at": at(5)},
                    guardrails={"candidate": {"candidate_sha": "b" * 40, "round": 2}})
        self.assertIsNone(report(item)["acceptance"]["rate"]["percent"])
        item["merged_round"] = 1
        self.assertIsNone(report(item)["acceptance"]["rate"]["percent"])
        item["merged_round"] = 2
        self.assertEqual(report(item)["acceptance"]["rate"]["percent"], 100)

    def test_merged_sha_without_a_corroborated_round_does_not_establish_owner_acceptance(self):
        owner = event("owner_decision", 1, 6, disposition="accepted", material_follow_up=False)
        owner["candidate_sha"] = "a" * 40
        item = record(events=[owner])
        self.assertEqual(report(item)["acceptance"]["rate"], dict(numerator=0, eligible=1, covered=0,
                                                                 unknown=1, percent=None))

    def test_owner_acceptance_uses_observation_diagnostics_separately_from_guard_diagnostics(self):
        owner = event("owner_decision", 1, 6, disposition="accepted", material_follow_up=False)
        owner.update(candidate_sha="a" * 40, round=1)
        item = dict(record(events=[owner]), merged_round=1, observation_diagnostics=[],
                    diagnostics=["technical receipt unavailable"],
                    guardrails={"diagnostics": ["technical receipt unavailable"]})
        self.assertEqual(report(item)["acceptance"]["rate"]["percent"], 100)
        item["observation_diagnostics"].append("unreadable observation file")
        self.assertIsNone(report(item)["acceptance"]["rate"]["percent"])

    def test_merge_refusal_submits_for_review_while_merge_conflict_is_a_blocker(self):
        item = record(history=[(1, "intake"), (3, "merging"), (4, "needs-human")])
        note = item["job"]["history"][-1]
        note["note"] = "auto-merge refused: changed outside the allowlist"
        self.assertEqual(report(item)["acceptance"]["rate"]["eligible"], 1)
        note["note"] = "merge conflict with base"
        self.assertEqual(report(item)["acceptance"]["rate"]["eligible"], 0)

    def test_equal_invoice_ids_require_issuer_to_deduplicate(self):
        a = event("usage_cost", 1, cost_type="subscription_charge", unit="currency", amount="7",
                  currency="USD", billing_item="001")
        b = event("usage_cost", 2, cost_type="subscription_charge", unit="currency", amount="7",
                  currency="USD", billing_item="001")
        a["source"]["provider"] = "provider-a"
        b["source"]["provider"] = "provider-b"
        def amount():
            return report(record("a", events=[a]), record("b", events=[b]))["costs"]["totals"][0]
        self.assertEqual(amount()["amount"], "14")
        b["source"]["provider"] = "provider-a"
        self.assertEqual(amount()["amount"], "7")
        del b["source"]["provider"]
        self.assertIsNone(amount()["amount"])
        self.assertTrue(amount()["ambiguous_billing_item"])

    def test_interruption_outcome_after_as_of_remains_pending(self):
        item = record()
        item["guardrails"] = dict(interruptions=dict(records=[
            dict(interruption_id="i", verified_safe=True, occurred_at=at(15), started_at=at(3),
                 ended_at=at(15), outcome="resumed")]))
        early = report(item, cutoff=at(6))["interruptions"]
        self.assertEqual(early["observed"], 1)
        self.assertEqual(early["verified_safe"]["numerator"], 0)
        self.assertEqual(early["pending"], 1)
        self.assertIsNone(early["elapsed_minutes"]["median"])
        self.assertEqual(report(item)["interruptions"]["verified_safe"]["numerator"], 1)

    def test_reported_recovery_needs_independent_evidence_for_safety_rate(self):
        job = record()
        interruption = dict(interruption_id="i", outcome="resumed", occurred_at=at(5),
                            started_at=at(3), ended_at=at(5), verified_safe=False,
                            source={"kind": "controller", "record_id": "r"}, event_ids=["r"])
        job["guardrails"] = {"interruptions": {"records": [interruption]}}
        result = report(job)
        summary = result["interruptions"]
        self.assertEqual(summary["resumed"], 1)
        self.assertEqual(summary["verified_safe"], dict(numerator=0, eligible=1, covered=0, unknown=1, percent=None))
        self.assertEqual(summary["unverified_outcomes"], 1)
        self.assertIn("1 reported resolution without independent verification", render_weekly_report(result))
        job["guardrails"]["interruptions"]["records"].append(dict(interruption, interruption_id="ii", verified_safe=True))
        summary = report(job)["interruptions"]
        self.assertEqual(summary["verified_safe"], dict(numerator=1, eligible=2, covered=1, unknown=1, percent=None))
        self.assertEqual(summary["source_coverage"]["independently_verified"], 1)
        interruption["verified_safe"] = True
        self.assertEqual(report(job)["interruptions"]["verified_safe"]["percent"], 100)

    def test_coverage_must_be_observed_and_recorded_through_window_end(self):
        for occurrence, ingestion, expected in [(5, 20, None), (20, 5, None), (15, 15, 0)]:
            coverage = event("observation_coverage", 1, occurrence, scope="escaped_defects",
                             complete=True, window_start=at(5), window_end=at(15))
            coverage.update(candidate_sha="a" * 40, recorded_at=at(ingestion))
            self.assertEqual(report(record(events=[coverage]))["escaped_defects"]["rate"]["percent"], expected)

    def test_whole_job_coverage_declared_before_completion_is_partial(self):
        early = event("owner_effort_coverage", 1, 4, scope="job", complete=True)
        effort = report(record(events=[early]))["owner_effort"]
        self.assertEqual(effort["covered_jobs"], 0)
        self.assertIsNone(effort["known_minutes"])
        self.assertEqual(effort["per_merge_median"]["sample"], 0)

    def test_preparation_before_intake_extends_required_weekly_coverage(self):
        preparation = event("owner_effort", 1, day=3, activity="preparation", rework=False, minutes=7)
        coverage = event("owner_effort_coverage", 2, day=8, scope="owner_effort", complete=True,
                         window_start=at(4), window_end=at(6))
        item = record(history=[(4, "intake"), (6, "merged")], events=[preparation, coverage])
        effort = report(item)["owner_effort"]
        self.assertEqual(effort["known_minutes"], 7)
        self.assertEqual(effort["covered_jobs"], 0)
        self.assertEqual(effort["per_merge_median"]["sample"], 0)

    def test_direct_noncode_job_level_owner_decision_ignores_unused_factory_round(self):
        context = event("task_context", 1, workflow_mode="direct", task_type="planning",
                        submitted_at=at(4))
        owner = event("owner_decision", 2, 6, disposition="accepted", material_follow_up=False)
        item = record(events=[context,owner], round=1)
        item["merged_candidate_sha"] = None
        self.assertEqual(report(item)["acceptance"]["rate"]["percent"], 100)
        item["events"][-1]["round"] = 1
        self.assertIsNone(report(item)["acceptance"]["rate"]["percent"])

    def test_historical_guardrail_path_is_labeled_local(self):
        item = record("001-one")
        item["guardrails"] = dict(requirements=[dict(id="gate", required=True, status="unverified",
                                                    evidence=[], legacy_paths=["gate-r1.md"])])
        markdown = render_weekly_report(report(item))
        self.assertIn("Local historical evidence reference `factory/jobs/001-one/gate-r1.md`", markdown)
        self.assertNotIn("](factory/jobs/", markdown)
        self.assertIn("unverified", markdown)

    def test_only_tracker_links_and_safe_local_references_are_rendered(self):
        item = record("001-one")
        item["url"] = "https://github.com/example/factory/pull/2"
        item["guardrails"] = dict(requirements=[dict(id="gate", required=True, status="unverified", evidence=[
            {"url": "https://example.test/demo", "verified": True},
            {"path": "../secret"},
        ])])
        markdown = render_weekly_report(report(item))
        self.assertIn("[001-one](https://github.com/example/factory/pull/2)", markdown)
        self.assertIn("Evidence link unavailable", markdown)
        self.assertNotIn("https://example.test/demo", markdown)
        self.assertNotIn("secret", markdown)

    def test_authenticated_merge_date_precedes_local_stage_update(self):
        item = record(history=[(1, "intake"), (4, "approve"), (4, "needs-human")])
        item["merged_at"] = at(6)
        item["merge_evidence"] = dict(url="https://github.com/example/repo/pull/5", merged_at=at(6))
        current = report(item)
        self.assertEqual(current["delivered"]["count"], 1)
        self.assertEqual(current["delivered"]["merge_evidence"]["one"], item["merge_evidence"])
        self.assertEqual(current["outcomes"]["merged"], 1)
        self.assertEqual(report(item, cutoff=at(5))["delivered"]["count"], 0)

    def test_authoritative_merge_week_overrides_stale_local_date(self):
        item = record(history=[(1, "intake"), (4, "merged")])
        item["merged_at"] = at(10)
        self.assertEqual(report(item)["delivered"]["count"], 0)
        self.assertEqual(report(item, start="2026-11-09")["delivered"]["count"], 1)
        self.assertEqual(report(item, cutoff=at(5))["delivered"]["count"], 0)

    def test_renderer_handles_unreadable_jobs_supplied_by_cli(self):
        result = report()
        result["diagnostics"]["001-broken"] = ["unreadable job.json"]
        self.assertIn("001-broken", render_weekly_report(result))

    def test_as_of_only_uses_verified_first_attempt(self):
        item = record(history=[(1, "intake"), (4, "gate"), (5, "review"), (6, "merged")])
        self.assertIsNone(report(item)["delivered"]["first_gate"]["percent"])
        item["first_gate"] = dict(attempted_at=at(4), verified=True, status="fail")
        self.assertEqual(report(item)["delivered"]["first_gate"]["percent"], 0)
        item["first_gate"]["status"] = "pass"
        self.assertEqual(report(item)["delivered"]["first_gate"]["percent"], 100)

    def test_escalations_guardrail_gaps_and_bottlenecks(self):
        item = record("001-one", events=[event("escalation", 1, request_id="a", rating="necessary"),
                              event("escalation", 2, request_id="b", rating="missed"),
                              event("escalation", 3, request_id="c", rating=None)])
        item["guardrails"] = dict(coverage=dict(eligible=2,covered=1,proven=0),
                                 requirements=[dict(id="test",required=True,status="fail"),
                                               dict(id="review",required=True,status="missing")])
        other = record("002-two")
        other["guardrails"] = item["guardrails"]
        result = report(item, other)
        self.assertEqual(result["escalations"]["rated_requests"], 1)
        self.assertEqual(result["escalations"]["unrated"], 1)
        self.assertEqual(result["escalations"]["missed"], 1)
        self.assertEqual(result["guardrails"]["covered_requirements"], 2)
        self.assertIn("review", render_weekly_report(result))
        self.assertIn("`002-two` (local job)", render_weekly_report(result))
        self.assertNotIn("job.json", render_weekly_report(result))


if __name__ == "__main__":
    unittest.main()
