"""Project a weekly scorecard into a local bundle without copying private records."""

import hashlib
import html
import json
import re
from collections import Counter
from datetime import date, timedelta
from statistics import median

import factory as F
from evidence_bundle import (ARTIFACT_SELECTION, MAX_SELECTED, BundleError, page_header,
                             project_native_receipt, write_projected_bundle)
from scorecard_report import ACTIVITIES, BASELINE, COST_TYPES, SMALL_SAMPLE
from scorecard_source import collect_weekly


COST_LABELS = {
    "subscription_usage": "Recorded subscription usage",
    "subscription_charge": "Actual subscription charge",
    "incremental_charge": "Actual incremental charge",
    "api_equivalent_estimate": "Estimated API equivalent",
}
CURRENCIES = frozenset({"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "HKD", "INR", "JPY", "NZD", "SGD", "USD"})
SOURCES = ("billing", "controller", "fixture", "harness", "legacy", "owner", "reviewer")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
EVIDENCE_LABELS = {
    "checks": "Round gate", "final_checks": "Final gate", "review:code": "Code review",
    "review:security": "Security review", "review:eval": "Eval review", "approval": "Technical decision",
}
ARTIFACT_REQUIREMENTS = {
    "checks.md": "checks", "final-checks.md": "final_checks", "review-code.md": "review:code",
    "review-security.md": "review:security", "review-eval.md": "review:eval", "decision.md": "approval",
}
GAP_STATUSES = frozenset({"fail", "pending", "missing", "stale", "unverified"})
PREVIEW_ROWS = 8
OUTCOME_LABELS = (
    ("accepted_as_built", "Accepted as built"), ("accepted_with_follow_up", "Accepted with follow-up"),
    ("rejected", "Rejected"), ("abandoned", "Abandoned"), ("failed", "Failed"),
    ("pending", "Pending"), ("unobserved", "Unobserved"),
)
_DASHBOARD_STYLE = """
:root {
  color-scheme: light;
  --canvas: #f0eff8;
  --paper: #fff;
  --ink: #211b39;
  --muted: #514a6a;
  --rule: #d9d3e8;
  --accent: #5b4092;
  --attention: #8a4c19;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--canvas);
  font: 1rem/1.5 "Avenir Next", "Segoe UI", system-ui, sans-serif;
}
main { max-width: 75rem; margin: auto; padding: clamp(1rem, 4vw, 3.5rem); }
h1 { margin: .25rem 0 .45rem; font-size: clamp(2rem, 5vw, 3.65rem); line-height: 1.04; letter-spacing: -.045em; }
h2 { margin: 0; font-size: 1.25rem; line-height: 1.25; }
h3 { margin: 1.15rem 0 .4rem; font-size: 1.05rem; }
p { max-width: 72ch; }
a { color: var(--accent); text-underline-offset: .18em; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
.report-period { margin: 0; color: var(--accent); font-size: 1.2rem; font-weight: 650; }
.report-context { margin: .55rem 0 1.2rem; color: var(--muted); font-size: .92rem; }
.report-context span { display: inline-block; margin-right: 1rem; }
.scope-note { color: var(--muted); font-size: .92rem; margin: 0 0 1.25rem; }
.coverage-alert { padding: .8rem 1rem; border-left: .25rem solid var(--attention); background: var(--paper); }
.headlines {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
  color: #fff;
  background: #292046;
  border-radius: 1.05rem;
  overflow: hidden;
}
.headlines > div { padding: 1.2rem clamp(.75rem, 1.65vw, 1.35rem); }
.headlines > div + div { border-left: 1px solid #565070; }
.headlines dt { color: #ebe3ff; font-size: .94rem; font-weight: 650; }
.headlines dd { margin: .45rem 0 0; }
.metric-value { display: block; font-size: clamp(1.65rem, 3vw, 2.75rem); font-weight: 700; line-height: 1.08; }
.metric-value.is-unavailable { color: #e6dbff; font-size: clamp(1.1rem, 2vw, 1.55rem); padding-block: .25rem; }
.metric-note { display: block; color: #ede8f8; margin-top: .5rem; font-size: .83rem; }
.metric-note > span { display: block; }
.metric-value, .bar-value { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.charts { display: grid; grid-template-columns: 1.2fr 1fr; gap: 1rem; margin-top: 1rem; }
figure { min-width: 0; margin: 0; padding: clamp(.9rem, 2.4vw, 1.45rem); background: var(--paper);
         border: 1px solid var(--rule); border-radius: .7rem; }
figcaption p, .chart-note { margin: .55rem 0 .8rem; color: var(--muted); font-size: .89rem; }
.chart-rows { display: grid; gap: .48rem; list-style: none; margin: 1rem 0 0; padding: 0; }
.chart-rows li { display: grid; align-items: center; gap: .25rem .65rem; font-size: .87rem; }
.outcome-chart li { grid-template-columns: minmax(7rem, 1fr) minmax(2rem, 1.3fr) minmax(1.6rem, auto); }
.coverage-chart li { grid-template-columns: minmax(0, 1fr) auto; margin-bottom: .28rem; }
.coverage-chart meter { grid-column: 1 / -1; grid-row: 2; }
.bar-value { text-align: right; }
.bar-value-empty { color: var(--muted); font-weight: 500; }
meter { display: block; width: 100%; height: .66rem; }
meter::-webkit-meter-bar { border: none; border-radius: .2rem; background: #e8e1f4; }
meter::-webkit-meter-optimum-value { background: var(--accent); }
meter::-moz-meter-bar { background: var(--accent); }
.pending meter::-webkit-meter-optimum-value { background: var(--attention); }
.pending meter::-moz-meter-bar { background: var(--attention); }
.details-section { margin-top: 2rem; }
.details-section > h2 { margin-bottom: .65rem; }
details { border-top: 1px solid var(--rule); background: var(--paper); }
details:last-child { border-bottom: 1px solid var(--rule); }
summary { display: list-item; padding: .7rem 1rem; color: var(--ink); font-weight: 650; cursor: pointer; }
summary:hover { color: var(--accent); }
.detail-content { padding: 0 1rem .7rem 1.8rem; line-height: 1.6; overflow-wrap: anywhere; }
.detail-content p { margin: .25rem 0 .7rem; }
.detail-content ul { padding-left: 1.3rem; }
footer { margin-top: 1.15rem; font-size: .9rem; }
@media (max-width: 55rem) {
  .headlines { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .headlines > div:nth-child(3) { border-left: 0; }
  .headlines > div:nth-child(n+3) { border-top: 1px solid #565070; }
}
@media (max-width: 44rem) { .charts { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 23rem) {
  .headlines { grid-template-columns: minmax(0, 1fr); }
  .headlines > div + div { border: 0; border-top: 1px solid #565070; }
}
"""


def _numbers(values, fields):
    """Fixed field names and numeric values only."""
    result = {}
    for key in fields.split():
        value = values.get(key)
        result[key] = value if type(value) in (int, float) else None
    return result


def _rate(metric):
    return _numbers(metric, "numerator eligible covered unknown percent") if metric is not None else None


def _sample(metric):
    return _numbers(metric, "median sample excluded")


def _acceptance(row):
    return {**_numbers(row, "accepted_as_built accepted_with_follow_up rejected abandoned failed pending unobserved "
                            "no_recorded_submission submission_unobserved"), "rate": _rate(row["rate"])}


def _effort(row):
    return {**_numbers(row, "known_minutes covered_jobs eligible_jobs unknown_jobs rework_minutes rework_covered_jobs "
                            "rework_total_minutes unknown_rework_entries unclassified_minutes unknown_rework_amount_entries"),
            "activities": _numbers(row["activities"], " ".join(ACTIVITIES)),
            "per_merge_median": _numbers(row["per_merge_median"], "minutes sample excluded")}


def _defects(row):
    return {**_numbers(row, "window_days immature unwindowed_observations"), "rate": _rate(row["rate"])}


def _guardrails(row):
    return _numbers(row, "eligible_requirements covered_requirements proven_requirements applicable_jobs "
                         "fully_covered_jobs jobs_without_requirement_inventory confirmed_blocked_observed "
                         "confirmed_effective_observed suspected_observed effective_total incident_coverage_jobs "
                         "eligible_jobs uncertain_incidents")


def _recovery(row):
    return {**_numbers(row, "observed resumed stopped unresolved pending unknown unverified_outcomes known_owner_minutes "
                            "linked_effort_entries untimed_effort_entries"),
            "verified_safe": _rate(row["verified_safe"]), "elapsed_minutes": _sample(row["elapsed_minutes"]),
            "source_coverage": _numbers(row["source_coverage"], "independently_verified observed_interruptions "
                                                               "eligible_jobs jobs_with_complete_observation")}


def _escalations(row):
    return {**_numbers(row, "rated_requests unrated unnecessary missed covered_jobs eligible_jobs"),
            "precision": _rate(row["precision"])}


def _costs(report):
    categories = []
    for kind in COST_TYPES:
        totals = []
        rows = [row for row in report["totals"] if row["cost_type"] == kind]
        for number, row in enumerate(rows, 1):
            sources = Counter((item.get("source") or {}).get("kind") for item in row["items"])
            amount = row["amount"]
            totals.append({
                "unit_group": number,
                "currency": row["currency"] if row["currency"] in CURRENCIES else None,
                "amount": amount if isinstance(amount, str) and DECIMAL.fullmatch(amount) else None,
                "conflicting": row["conflict"] is True,
                "ambiguous_billing_item": row["ambiguous_billing_item"] is True,
                "source_observations": {source: sources[source] for source in SOURCES if sources[source]},
            })
        categories.append({"cost_type": kind, "totals": totals, "coverage": _numbers(
            report["coverage"][kind], "jobs_with_amount eligible_jobs other_jobs_with_amount")})
    return {"categories": categories, "unattributed_observations": len(report["unattributed"])}


def _cohorts(report):
    views = {"type_and_size": (report["task_cohorts"], report["task_cohort_coverage"])}
    for dimension in ("task_type", "task_size"):
        view = report["task_cohorts_by"][dimension]
        views[dimension] = view["cohorts"], view["coverage"]
    return {dimension: {
        "coverage": _numbers(coverage, "eligible covered unknown"),
        "small_groups": len(coverage["small_groups"]),
        "groups": [{"group": number, **_numbers(row, "jobs merged_jobs"),
                    "acceptance": _acceptance(row["acceptance"]),
                    "owner_effort": _effort(row["owner_effort"]),
                    "lead_hours": _sample(row["lead_hours"]),
                    "first_gate": _rate(row["first_gate"]),
                    "escaped_defects": _defects(row["escaped_defects"]),
                    "guardrails": _guardrails(row["guardrails"])}
                   for number, row in enumerate(rows, 1)],
    } for dimension, (rows, coverage) in views.items()}


def _finding_label(requirement):
    if requirement.startswith("criterion:"):
        return "Acceptance criterion"
    return EVIDENCE_LABELS.get(requirement, "Additional review")


def _gap_status(value):
    return value if value in GAP_STATUSES else "unknown"


def _metrics(report, aliases, receipts, outside_active):
    if report["delivered"] is None:
        return None
    delivery, guard, matched = (report[key] for key in ("delivered", "guardrails", "matched_tasks"))
    pairs = [pair["factory_minus_direct_minutes"] for pair in matched["pairs"]]
    links = [row for row in receipts if "file" in row]
    delivered = [{"job": aliases[job], "selected_receipts": [row["file"] for row in links if row["job"] == aliases[job]]}
                 for job in delivery["jobs"]]
    gaps = []
    for gap in guard["gaps"]:
        job, label = aliases[gap["job"]], _finding_label(gap["requirement"])
        related = [row["file"] for row in links if row["job"] == job and row["requirement"] == label]
        gaps.append({"job": job, "requirement": label, "status": _gap_status(gap["status"]),
                     "selected_receipts": related})
    repeated = [{"requirement": _finding_label(item["requirement"]), "status": _gap_status(item["status"]),
                 "jobs": item["jobs"]} for item in guard["repeated_gaps"]]
    causes = [{"group": number, "reported_failures": item["count"],
               "jobs": sorted({aliases[row["job"]] for row in report["recovery"]["records"]
                               if row["reported_cause"] == item["cause"]})}
              for number, item in enumerate(report["recovery"]["reported_recurring"], 1)]
    return {
        "active_jobs": None if outside_active else len(report["active_jobs"]),
        "observed_active_jobs": len(report["active_jobs"]),
        "small_sample": report["small_sample"] is True,
        "delivery": {**_numbers(delivery, "count"), "jobs": delivered, "independently_verified_count": None,
                     "first_gate": _rate(delivery["first_gate"]),
                     "lead_hours": _sample(delivery["lead_hours"]),
                     "agent_elapsed_minutes": _sample(delivery["agent_elapsed_minutes"])},
        "acceptance": _acceptance(report["acceptance"]),
        "owner_effort": _effort(report["owner_effort"]),
        "escaped_defects": _defects(report["escaped_defects"]),
        "escalations": _escalations(report["escalations"]),
        "guardrails": {**_guardrails(guard), "requirement_gap_count": len(guard["gaps"]),
                       "gaps": gaps, "repeated_gap_groups": repeated},
        "recovery": {**_recovery(report["interruptions"]), "reported_recurring_cause_groups": causes},
        "costs": _costs(report["costs"]),
        "cohorts": _cohorts(report),
        "matched_tasks": {**_numbers(matched, "candidate_keys excluded_keys"), "pairs": len(pairs),
                          "median_factory_minus_direct_minutes": median(pairs) if pairs else None},
    }


def _selected_receipts(root, aliases, selections, cutoff):
    selected, seen, files, snapshots = [], set(), {}, []
    if len(selections) > MAX_SELECTED:
        raise BundleError("Select at most 32 weekly receipts.")
    for number, selection in enumerate(selections, 1):
        job, _, artifact = selection.partition(":")
        match = ARTIFACT_SELECTION.fullmatch(artifact)
        if not match or not F.valid_reporting_job_id(job) or job not in aliases:
            raise BundleError("Select a supported artifact from an active job in the reported cohort.")
        key = job, match[1]
        if key in seen:
            raise BundleError("Select each weekly job artifact once.")
        seen.add(key)
        source, status, data, snapshot = project_native_receipt(root, job, match, cutoff)
        requirement = ARTIFACT_REQUIREMENTS[match[1].rsplit("/", 1)[-1]]
        row = {"job": aliases[job], "kind": source["kind"], "requirement": EVIDENCE_LABELS[requirement],
               "reported_verdict": source["reported_verdict"], "artifact_status": status,
               "claim_verification": "unverified", "candidate_binding": source["candidate_binding"],
               "source_timing": "current_at_export", "week_association": "unverified"}
        if data is not None:
            name = f"receipt-{number:03d}.json"
            row.update(file=name, sha256=hashlib.sha256(data).hexdigest())
            files[name] = data
        selected.append(row)
        snapshots.append(snapshot)
    return selected, files, snapshots


def _receipt_links(names):
    return ", ".join(f'<a href="{name}">Projected receipt</a>' for name in names) or "No selected receipt"


def _preview(rows, render):
    if not rows:
        return []
    lines = ["<ul>", *(f"<li>{render(row)}</li>" for row in rows[:PREVIEW_ROWS]), "</ul>"]
    if len(rows) > PREVIEW_ROWS:
        lines.append(f"<p>{len(rows) - PREVIEW_ROWS} more rows in the manifest.</p>")
    return lines


def _show(value):
    return "unknown" if value is None else html.escape(str(value))


def _headline(label, value, note):
    state = " is-unavailable" if value == "Unavailable" else ""
    return (f'<div><dt>{label}</dt><dd><span class="metric-value{state}">{value}</span>'
            f'<span class="metric-note">{note}</span></dd></div>')


def _rate_value(metric):
    return "Unavailable" if metric is None or metric["percent"] is None else f"{metric['percent']:g}%"


def _headlines(metrics):
    delivered = metrics["delivery"]["count"]
    acceptance = metrics["acceptance"]["rate"]
    effort, defects = metrics["owner_effort"], metrics["escaped_defects"]
    defect_rate = defects["rate"]
    defect_note = "Follow-up window unset."
    if defect_rate is not None:
        mature = defect_rate["eligible"]
        deliveries = "delivery" if mature == 1 else "deliveries"
        defect_note = (
            f"<span>{defects['window_days']}-day follow-up</span>"
            f"<span>{defect_rate['numerator']} confirmed affected among {mature} mature {deliveries}.</span>"
            f"<span>{defect_rate['covered']} observed, {defect_rate['unknown']} unknown. "
            f"{defects['immature']} still in follow-up.</span>"
        )
    effort_value = "Unavailable" if effort["known_minutes"] is None else f"{effort['known_minutes']} min"
    eligible = acceptance["eligible"]
    submissions = "submission" if eligible == 1 else "submissions"
    return [
        '<dl class="headlines" id="weekly-headlines" aria-label="Weekly headline metrics">',
        _headline("Recorded deliveries", delivered, "GitHub merge verification is unavailable offline."),
        _headline("Owner accepted as built", _rate_value(acceptance),
                  f"<span>{acceptance['numerator']} recorded as accepted among {eligible} eligible {submissions}.</span>"
                  f"<span>{acceptance['covered']} observed, {acceptance['unknown']} unknown.</span>"),
        _headline("Known owner time", effort_value,
                  f"{effort['covered_jobs']}/{effort['eligible_jobs']} jobs fully covered. "
                  f"{effort['unknown_jobs']} jobs with incomplete or missing records."),
        _headline("Escaped defects", _rate_value(defect_rate), defect_note),
        "</dl>",
    ]


def _chart_row(label, count, eligible, value, *, pending=False):
    # The visible text gives assistive technology the same values as the graphical bar.
    bar = f'<meter min="0" max="{eligible}" value="{count}" aria-hidden="true"></meter>' if eligible else ""
    row_class = ' class="pending"' if pending else ""
    value_class = " bar-value-empty" if not eligible else ""
    return f'<li{row_class}><span>{label}</span>{bar}<strong class="bar-value{value_class}">{value}</strong></li>'


def _coverage_row(label, covered, eligible):
    value = f"{covered} of {eligible}" if eligible else "0 of 0 eligible"
    return _chart_row(label, covered, eligible, value)


def _charts(metrics):
    acceptance, effort, defects, guardrails = (metrics[key] for key in
                                              ("acceptance", "owner_effort", "escaped_defects", "guardrails"))
    submissions = acceptance["rate"]["eligible"]
    defect_rate = defects["rate"]
    lines = [
        '<div class="charts">',
        '<figure class="outcome-chart" aria-labelledby="outcome-heading">',
        '<figcaption><h2 id="outcome-heading">Submission outcomes</h2>',
        f'<p>{submissions} eligible submissions in the reporting week. Each bar uses that total.</p></figcaption>',
    ]
    if submissions:
        lines += ['<ul class="chart-rows" role="list">', *(
            _chart_row(label, acceptance[key], submissions, acceptance[key], pending=key in ("pending", "unobserved"))
            for key, label in OUTCOME_LABELS
        ), "</ul>"]
    else:
        lines.append('<p class="chart-note">No submissions with a recorded date in this week.</p>')
    if undated := acceptance["submission_unobserved"]:
        jobs = "job has" if undated == 1 else "jobs have"
        lines.append(f'<p class="chart-note">{undated} active {jobs} no recorded submission time.</p>')
    lines += [
        '</figure><figure class="coverage-chart" aria-labelledby="coverage-heading">',
        '<figcaption><h2 id="coverage-heading">Evidence coverage</h2>',
        '<p>Observed or fully covered out of the eligible total for each measure.</p></figcaption>',
        '<ul class="chart-rows" role="list">',
        _coverage_row("Submission outcomes", acceptance["rate"]["covered"], submissions),
        _coverage_row("Jobs with complete owner time", effort["covered_jobs"], effort["eligible_jobs"]),
        (_coverage_row("Mature deliveries with known defect status", defect_rate["covered"], defect_rate["eligible"])
         if defect_rate is not None else _chart_row("Escaped-defect coverage", 0, 0, "Window unset")),
        _coverage_row("Verified inventoried requirements", guardrails["covered_requirements"],
                      guardrails["eligible_requirements"]),
        "</ul>",
    ]
    if without_inventory := guardrails["jobs_without_requirement_inventory"]:
        jobs = "job has" if without_inventory == 1 else "jobs have"
        lines.append(f'<p class="chart-note">{without_inventory} active {jobs} no requirement inventory.</p>')
    lines.append("</figure></div>")
    return lines


def _detail(title, content):
    return [f'<details><summary>{title}</summary><div class="detail-content">', *content, "</div></details>"]


def _frequency(metric):
    if metric is None:
        return "Rate unavailable"
    percentage = "Rate unavailable" if metric["percent"] is None else f"{metric['percent']:g}%"
    note = ", small sample" if metric["eligible"] < SMALL_SAMPLE else ""
    return (f"{metric['numerator']}/{metric['eligible']} · {percentage} "
            f"({metric['covered']} observed, {metric['unknown']} unknown{note})")


def _duration(metric, unit):
    return (f"{_show(metric['median'])} {unit} ({metric['sample']} measured, {metric['excluded']} unknown"
            f"{', small sample' if metric['sample'] < SMALL_SAMPLE else ''})")


def _metric_details(data):
    m = data["metrics"]
    delivery, a, e, g = (m[key] for key in ("delivery", "acceptance", "owner_effort", "guardrails"))
    defects, recovery, escalation = (m[key] for key in ("escaped_defects", "recovery", "escalations"))
    timing, coverage = e["per_merge_median"], recovery["source_coverage"]
    per_delivery = {"median": timing["minutes"], "sample": timing["sample"], "excluded": timing["excluded"]}
    lines = _detail("Delivery and owner outcomes", [
        f"<p>Median lead time {_duration(delivery['lead_hours'], 'hours')}. "
        f"Median elapsed time to ready {_duration(delivery['agent_elapsed_minutes'], 'minutes')}. "
        f"First gate passed {_frequency(delivery['first_gate'])}.</p>",
        f"<p>Owner accepted without material follow-up {_frequency(a['rate'])}. "
        f"Accepted with follow-up {a['accepted_with_follow_up']}, rejected {a['rejected']}, "
        f"abandoned {a['abandoned']}, failed {a['failed']}, pending {a['pending']}, unobserved {a['unobserved']}. "
        f"{a['no_recorded_submission']} terminal jobs with no recorded submission and "
        f"{a['submission_unobserved']} other jobs missing a submission time.</p>",
        "<h3>Recorded deliveries</h3>",
        *(_preview(delivery["jobs"], lambda row: f"Job {row['job']} · {_receipt_links(row['selected_receipts'])}")
          or ["<p>No recorded deliveries.</p>"]),
    ])
    lines += _detail("Owner effort and rework", [
        f"<p>{_show(e['known_minutes'])} known active minutes. {e['covered_jobs']}/{e['eligible_jobs']} jobs fully "
        f"covered, {e['unknown_jobs']} unknown. Known rework {_show(e['rework_minutes'])} minutes, "
        f"total rework {_show(e['rework_total_minutes'])} minutes. "
        f"Median per delivery {_duration(per_delivery, 'minutes')}.</p>",
        "<p>Minutes by activity · " + ", ".join(f"{key} {_show(value)}" for key, value in e["activities"].items()) + ".</p>",
    ])
    lines += _detail("Defects, escalation and recovery", [
        f"<p>Escaped defects {_frequency(defects['rate'])}. Follow-up window "
        f"{_show(defects['window_days'])} days. {_show(defects['immature'])} recorded deliveries still maturing. "
        f"{defects['unwindowed_observations']} defect observations on delivered jobs.</p>",
        f"<p>Escalation precision among owner-rated requests {_frequency(escalation['precision'])}. "
        f"{escalation['unrated']} unrated and {escalation['missed']} missed decisions observed. "
        f"Request coverage {escalation['covered_jobs']}/{escalation['eligible_jobs']} jobs.</p>",
        f"<p>Independently verified safe recovery {_frequency(recovery['verified_safe'])}. "
        f"{recovery['resumed']} resumed, {recovery['stopped']} stopped, {recovery['unresolved']} unresolved, "
        f"{recovery['pending']} pending, {recovery['unknown']} unknown. "
        f"{recovery['unverified_outcomes']} reported resolutions without independent verification. "
        f"Observation coverage {coverage['jobs_with_complete_observation']}/{coverage['eligible_jobs']} jobs. "
        f"Median elapsed {_duration(recovery['elapsed_minutes'], 'minutes')}. "
        f"Linked owner effort {_show(recovery['known_owner_minutes'])} known minutes.</p>",
    ])
    lines += _detail("Guardrails and recurring bottlenecks", [
        f"<p>{g['covered_requirements']}/{g['eligible_requirements']} requirements verified, "
        f"{g['proven_requirements']} passing, {g['jobs_without_requirement_inventory']} jobs without a requirement "
        f"inventory. {g['requirement_gap_count']} requirement gaps, including {len(g['repeated_gap_groups'])} recurring "
        f"gap groups.</p>",
        f"<p>Effective violation total {_show(g['effective_total'])} "
        f"({g['confirmed_effective_observed']} confirmed, {g['incident_coverage_jobs']}/{g['eligible_jobs']} jobs covered). "
        f"{g['confirmed_blocked_observed']} confirmed blocked attempts and {g['suspected_observed']} suspected "
        "incidents observed. Independent incident and interruption sources are unavailable.</p>",
        "<h3>Guardrail evidence gaps</h3>",
        *(_preview(g["gaps"], lambda row: f"Job {row['job']} · {row['requirement']} · {row['status']} · "
                                         f"{_receipt_links(row['selected_receipts'])}")
          or ["<p>No requirement gaps recorded.</p>"]),
        "<h3>Recurring bottlenecks</h3>",
        *([*_preview(g["repeated_gap_groups"], lambda row:
                     f"{row['requirement']} · {row['status']} on {row['jobs']} jobs"),
           *_preview(recovery["reported_recurring_cause_groups"], lambda row:
                     f"Reported cause group {row['group']} · {row['reported_failures']} failures on "
                     f"{len(row['jobs'])} jobs · cause unverified")]
          or ["<p>No recurring bottlenecks recorded.</p>"]),
    ])
    costs = ["<ul>"]
    for category in m["costs"]["categories"]:
        pieces = []
        for row in category["totals"]:
            source_types = ", ".join(f"{count} {kind}" for kind, count in row["source_observations"].items())
            status = " · conflicting records" if row["conflicting"] else ""
            unit = row["currency"] or ("units" if category["cost_type"] == "subscription_usage" else "currency withheld")
            pieces.append(f"{_show(row['amount'])} {unit} (group {row['unit_group']}, "
                          f"{source_types or 'source unavailable'}{status})")
        cov = category["coverage"]
        costs.append(f"<li>{COST_LABELS[category['cost_type']]} · {', '.join(pieces) or 'unknown'} "
                     f"({cov['jobs_with_amount']}/{cov['eligible_jobs']} active jobs and "
                     f"{cov['other_jobs_with_amount']} other jobs with recorded amounts).</li>")
    costs += ["</ul>", f"<p>{m['costs']['unattributed_observations']} cost or usage observations could not be "
                       "attributed. Original source details stay in the private scorecard.</p>"]
    lines += _detail("Usage and cost", costs)

    cohorts = [f"<p>Small groups contain fewer than {SMALL_SAMPLE} jobs. Views overlap. Group numbers are local to "
               "this bundle. The private scorecard holds the source labels.</p>"]
    for key, label in (("type_and_size", "Type and size"), ("task_type", "Type"), ("task_size", "Size")):
        view = m["cohorts"][key]
        cov = view["coverage"]
        cohorts.append(f"<p>{label} · {cov['covered']}/{cov['eligible']} classified, {cov['unknown']} unknown "
                       f"and {view['small_groups']} small groups.</p>")
        cohorts.extend(_preview(view["groups"], lambda row:
                                f"Group {row['group']} · {row['jobs']} active, {row['merged_jobs']} delivered. "
                                f"Owner accepted as built {_frequency(row['acceptance']['rate'])}. "
                                f"{_show(row['owner_effort']['known_minutes'])} known owner minutes "
                                f"({row['owner_effort']['covered_jobs']}/{row['owner_effort']['eligible_jobs']} "
                                "jobs fully covered)."))
    match = m["matched_tasks"]
    cohorts.append(f"<p>{match['pairs']}/{match['candidate_keys']} matched-task pairs measured. Median observed "
                   f"factory minus direct time {_show(match['median_factory_minus_direct_minutes'])} minutes."
                   f"{' Small sample.' if match['pairs'] < SMALL_SAMPLE else ''} Observed comparisons are descriptive.</p>")
    lines += _detail("Task cohorts and matched work", cohorts)
    return lines


def _page(manifest):
    period, coverage = manifest["period"], manifest["source_coverage"]
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end_exclusive"]) - timedelta(days=1)
    lines = page_header("Weekly factory evidence", style=_DASHBOARD_STYLE) + [
        f'<p class="report-period"><time datetime="{start}">{start:%d %b %Y}</time> to '
        f'<time datetime="{end}">{end:%d %b %Y}</time></p>',
        f'<p class="report-context"><span>Observation cutoff <time datetime="{period["as_of"]}">'
        f'{period["as_of"]}</time></span><span>Owner review pending</span></p>',
    ]
    metrics = manifest["metrics"]
    if metrics is not None:
        observed = metrics["observed_active_jobs"]
        lines += [
            f'<p class="scope-note">Observed active jobs: <strong>{observed}</strong>.'
            f'{" Small sample." if metrics["small_sample"] else ""}'
            f'{" Partial week at the observation cutoff." if period["partial"] else ""}</p>',
        ]
        if outside_active := coverage["recorded_deliveries_outside_active_cohort"]:
            deliveries = "delivery falls" if outside_active == 1 else "deliveries fall"
            lines.append(f'<p class="coverage-alert">Active job coverage is incomplete. {outside_active} recorded '
                         f'{deliveries} outside the observed active cohort. Active-job measures describe the '
                         "observed subset.</p>")
        lines.extend(_headlines(metrics))
        lines.extend(_charts(metrics))
    else:
        lines.append('<p class="coverage-alert">The weekly cohort is unknown until a reporting time zone is supplied.</p>')

    lines.append('<section class="details-section" id="report-details"><h2>Report details</h2>')
    if metrics is not None:
        lines.extend(_metric_details(manifest))
    coverage_details = ["<p>This bundle projects recorded data from the local scorecard. The selection and source "
                        "associations are unverified. Technical verdicts and owner decisions are separate.</p>"]
    if period["time_zone_configured"]:
        coverage_details.append(f"<p>UTC cohort boundaries are {period['start_utc']} through {period['end_utc']} "
                                "(exclusive).</p>")
    coverage_details.append(
        f"<p>{coverage['unwindowed_jobs']} jobs in the unwindowed inventory with "
        f"{coverage['unwindowed_real_observations']} real observations. "
        f"{coverage['controlled_jobs_excluded']} controlled jobs excluded. "
        f"{coverage['unreadable_jobs_omitted']} unreadable jobs and {coverage['invalid_job_directories']} "
        f"invalid job directories omitted. {coverage['jobs_without_start_time']} jobs lack a start time. "
        f"{coverage['jobs_with_incomplete_observations']} jobs have incomplete observation files. "
        f"{coverage['jobs_with_legacy_values']} jobs have unwindowed legacy values.</p>")
    lines += _detail("Source coverage and reporting period", coverage_details)
    evidence_details = [
        "<p>Only explicitly selected receipts appear here. Unselected job evidence and direct source links remain "
        "in the private scorecard. Local links open verdicts projected from current local artifacts. Their presence "
        "during the reporting week or by the reporting cutoff is unverified.</p>",
    ]
    rows = manifest["selected_evidence"]
    if rows:
        evidence_details.append("<ul>")
        for row in rows:
            label = f"Job {row['job']} · {row['requirement']}"
            receipt = f' · <a href="{row["file"]}">Projected receipt</a>' if "file" in row else ""
            evidence_details.append(f"<li>{label} · {row['reported_verdict']} · {row['artifact_status']} · "
                                    f"current local artifact, week association unverified{receipt}</li>")
        evidence_details.append("</ul>")
    else:
        evidence_details.append("<p>No receipts selected.</p>")
    lines += _detail("Selected local evidence", evidence_details)
    lines += _detail("Historical baseline", [f"<p>{html.escape(BASELINE)}</p>"])
    lines += ['</section><footer><a href="manifest.json">Manifest and receipt digests</a></footer></main></body></html>']
    return "\n".join(lines).encode("utf-8")


def export_week(root, week, cutoff, timezone, defect_window_days, output, selections):
    try:
        def collect():
            return collect_weekly(root, F.load_config(root), week, cutoff, timezone, defect_window_days)

        source = collect()
        report, unreadable, controlled = source
        active = {job: number for number, job in enumerate(report.get("active_jobs", ()), 1)}
        outside = [job for job in (report["delivered"] or {}).get("jobs", ()) if job not in active]
        aliases = {**active, **{job: number for number, job in enumerate(outside, len(active) + 1)}}
        selected, receipts, snapshots = _selected_receipts(root, active, selections, cutoff)

        def verify_sources():
            if collect() != source or _selected_receipts(root, active, selections, cutoff) != (selected, receipts, snapshots):
                raise BundleError("Weekly source evidence changed during export. Choose an unused directory and retry.")

        period = report["period"]
        manifest = {
            "schema_version": 1, "scope": "weekly_factory_evidence", "owner_review": "pending",
            "source_association_verification": "unverified",
            "period": {key: period.get(key) for key in ("start", "end_exclusive", "as_of", "start_utc", "end_utc", "partial")},
            "defect_window_days": defect_window_days, "baseline": BASELINE, "small_sample_threshold": SMALL_SAMPLE,
            "source_coverage": {
                "unwindowed_jobs": report["unwindowed"]["jobs"],
                "unwindowed_real_observations": sum(report["unwindowed"]["observations"].values()),
                "controlled_jobs_excluded": controlled,
                "unreadable_jobs_omitted": len(unreadable),
                "invalid_job_directories": report["invalid_job_directories"],
                "jobs_without_start_time": len(report["undated_jobs"]),
                "jobs_with_incomplete_observations": len(report["diagnostics"].keys() - unreadable.keys()),
                "jobs_with_legacy_values": len(report["legacy"]),
                "recorded_deliveries_outside_active_cohort": len(outside) if report["delivered"] is not None else None,
                "incident_and_interruption_source_verification": "unavailable",
                "github_merge_verification": "unavailable",
            },
            "metrics": _metrics(report, aliases, selected, outside), "selected_evidence": selected,
        }
        manifest["period"]["time_zone_configured"] = bool(timezone)
        files = {**receipts, "manifest.json": (json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode("ascii"),
                 "index.html": _page(manifest)}
        write_projected_bundle(root, output, files, verify_sources)
    except (F.FactoryError, OSError, ValueError, OverflowError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError("Cannot export the weekly evidence. Check the reporting inputs and local destination.") from error
