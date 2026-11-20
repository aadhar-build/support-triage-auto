"""Project a weekly scorecard into a local bundle without copying private records."""

import hashlib
import html
import json
import re
from collections import Counter
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


def _overview(data):
    m = data["metrics"]
    delivery, a, e, g = (m[key] for key in ("delivery", "acceptance", "owner_effort", "guardrails"))
    defects, recovery, escalation = (m[key] for key in ("escaped_defects", "recovery", "escalations"))
    timing, coverage = e["per_merge_median"], recovery["source_coverage"]
    per_delivery = {"median": timing["minutes"], "sample": timing["sample"], "excluded": timing["excluded"]}
    lines = [
        f"<p>{m['observed_active_jobs']} jobs in the observed active cohort. {'Small sample. ' if m['small_sample'] else ''}"
        f"{delivery['count']} recorded deliveries. GitHub merge verification is unavailable in this offline bundle.</p>",
        "<h2>Delivery and owner outcomes</h2>",
        f"<p>Median lead time {_duration(delivery['lead_hours'], 'hours')}. "
        f"Median elapsed time to ready {_duration(delivery['agent_elapsed_minutes'], 'minutes')}. "
        f"First gate passed {_frequency(delivery['first_gate'])}.</p>",
        f"<p>Owner accepted without material follow-up {_frequency(a['rate'])}. "
        f"Accepted with follow-up {a['accepted_with_follow_up']}, rejected {a['rejected']}, "
        f"abandoned {a['abandoned']}, failed {a['failed']}, pending {a['pending']}, unobserved {a['unobserved']}. "
        f"{a['no_recorded_submission']} terminal jobs with no recorded submission and "
        f"{a['submission_unobserved']} other jobs missing a submission time.</p>",
        "<h2>Owner effort and rework</h2>",
        f"<p>{_show(e['known_minutes'])} known active minutes. {e['covered_jobs']}/{e['eligible_jobs']} jobs fully "
        f"covered, {e['unknown_jobs']} unknown. Known rework {_show(e['rework_minutes'])} minutes, "
        f"total rework {_show(e['rework_total_minutes'])} minutes. "
        f"Median per delivery {_duration(per_delivery, 'minutes')}.</p>",
        "<p>Minutes by activity · " + ", ".join(f"{key} {_show(value)}" for key, value in e["activities"].items()) + ".</p>",
        "<h2>Defects, escalation and recovery</h2>",
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
        "<h2>Guardrails</h2>",
        f"<p>{g['covered_requirements']}/{g['eligible_requirements']} requirements verified, "
        f"{g['proven_requirements']} passing, {g['jobs_without_requirement_inventory']} jobs without a requirement "
        f"inventory. {g['requirement_gap_count']} requirement gaps, including {len(g['repeated_gap_groups'])} recurring "
        f"gap groups.</p>",
        f"<p>Effective violation total {_show(g['effective_total'])} "
        f"({g['confirmed_effective_observed']} confirmed, {g['incident_coverage_jobs']}/{g['eligible_jobs']} jobs covered). "
        f"{g['confirmed_blocked_observed']} confirmed blocked attempts and {g['suspected_observed']} suspected "
        "incidents observed. Independent incident and interruption sources are unavailable.</p>",
    ]
    lines += ["<h2>Recorded deliveries</h2>", *_preview(
        delivery["jobs"], lambda row: f"Job {row['job']} · {_receipt_links(row['selected_receipts'])}"),
        "<h2>Guardrail evidence gaps</h2>", *_preview(
            g["gaps"], lambda row: f"Job {row['job']} · {row['requirement']} · {row['status']} · "
                                   f"{_receipt_links(row['selected_receipts'])}"),
        "<h2>Recurring bottlenecks</h2>", *_preview(
            g["repeated_gap_groups"], lambda row: f"{row['requirement']} · {row['status']} on {row['jobs']} jobs"),
        *_preview(recovery["reported_recurring_cause_groups"], lambda row:
                  f"Reported cause group {row['group']} · {row['reported_failures']} failures on "
                  f"{len(row['jobs'])} jobs · cause unverified"), "<h2>Usage and cost</h2><ul>"]
    for category in m["costs"]["categories"]:
        pieces = []
        for row in category["totals"]:
            source_types = ", ".join(f"{count} {kind}" for kind, count in row["source_observations"].items())
            status = " · conflicting records" if row["conflicting"] else ""
            unit = row["currency"] or ("units" if category["cost_type"] == "subscription_usage" else "currency withheld")
            pieces.append(f"{_show(row['amount'])} {unit} (group {row['unit_group']}, "
                          f"{source_types or 'source unavailable'}{status})")
        cov = category["coverage"]
        lines.append(f"<li>{COST_LABELS[category['cost_type']]} · {', '.join(pieces) or 'unknown'} "
                     f"({cov['jobs_with_amount']}/{cov['eligible_jobs']} active jobs and "
                     f"{cov['other_jobs_with_amount']} other jobs with recorded amounts).</li>")
    lines += ["</ul>", f"<p>{m['costs']['unattributed_observations']} cost or usage observations could not be "
                       "attributed. Original source details stay in the private scorecard.</p>",
              "<h2>Task cohorts and matched work</h2>",
              f"<p>Small groups contain fewer than {SMALL_SAMPLE} jobs. Views overlap. Group numbers are local to "
              "this bundle. The private scorecard holds the source labels.</p>"]
    for key, label in (("type_and_size", "Type and size"), ("task_type", "Type"), ("task_size", "Size")):
        view = m["cohorts"][key]
        cov = view["coverage"]
        lines.append(f"<p>{label} · {cov['covered']}/{cov['eligible']} classified, {cov['unknown']} unknown "
                     f"and {view['small_groups']} small groups.</p>")
        lines.extend(_preview(view["groups"], lambda row:
                              f"Group {row['group']} · {row['jobs']} active, {row['merged_jobs']} delivered. "
                              f"Owner accepted as built {_frequency(row['acceptance']['rate'])}. "
                              f"{_show(row['owner_effort']['known_minutes'])} known owner minutes "
                              f"({row['owner_effort']['covered_jobs']}/{row['owner_effort']['eligible_jobs']} "
                              "jobs fully covered)."))
    match = m["matched_tasks"]
    lines.append(f"<p>{match['pairs']}/{match['candidate_keys']} matched-task pairs measured. Median observed "
                 f"factory minus direct time {_show(match['median_factory_minus_direct_minutes'])} minutes."
                 f"{' Small sample.' if match['pairs'] < SMALL_SAMPLE else ''} Observed comparisons are descriptive.</p>")
    return lines


def _page(manifest):
    period, coverage = manifest["period"], manifest["source_coverage"]
    lines = page_header("Weekly factory evidence") + [
        f"<p>Week starting {period['start']} through {period['end_exclusive']} (exclusive). "
        f"Observation cutoff {period['as_of']}.</p>",
        "<p>Owner review pending. This bundle projects recorded data from the local scorecard. "
        "The selection and source associations are unverified. Technical verdicts and owner decisions are separate.</p>",
    ]
    if period["time_zone_configured"]:
        lines.append(f"<p>Cohort boundaries in UTC · {period['start_utc']} to {period['end_utc']}."
                     f"{' Partial week at the observation cutoff.' if period['partial'] else ''}</p>")
        if outside_active := coverage["recorded_deliveries_outside_active_cohort"]:
            deliveries = "delivery falls" if outside_active == 1 else "deliveries fall"
            lines.append(f"<p>Active job coverage is incomplete. {outside_active} recorded {deliveries} outside "
                         "the observed active cohort. Active-job measures describe the observed subset.</p>")
        lines.extend(_overview(manifest))
    else:
        lines.append("<p>The weekly cohort is unknown until a reporting time zone is supplied.</p>")
    lines += [
        "<h2>Source coverage</h2>",
        f"<p>{coverage['unwindowed_jobs']} jobs in the unwindowed inventory with "
        f"{coverage['unwindowed_real_observations']} real observations. "
        f"{coverage['controlled_jobs_excluded']} controlled jobs excluded. "
        f"{coverage['unreadable_jobs_omitted']} unreadable jobs and {coverage['invalid_job_directories']} "
        f"invalid job directories omitted. {coverage['jobs_without_start_time']} jobs lack a start time. "
        f"{coverage['jobs_with_incomplete_observations']} jobs have incomplete observation files. "
        f"{coverage['jobs_with_legacy_values']} jobs have unwindowed legacy values.</p>",
        "<h2>Selected local evidence</h2>",
        "<p>Only explicitly selected receipts appear here. Unselected job evidence and direct source links remain "
        "in the private scorecard. Local links open verdicts projected from current local artifacts. Their presence "
        "during the reporting week or by the reporting cutoff is unverified.</p>",
    ]
    rows = manifest["selected_evidence"]
    if rows:
        lines.append("<ul>")
        for row in rows:
            label = f"Job {row['job']} · {row['requirement']}"
            receipt = f' · <a href="{row["file"]}">Projected receipt</a>' if "file" in row else ""
            lines.append(f"<li>{label} · {row['reported_verdict']} · {row['artifact_status']} · "
                         f"current local artifact, week association unverified{receipt}</li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No receipts selected.</p>")
    lines += [f"<h2>Historical baseline</h2><p>{html.escape(BASELINE)}</p>",
              '<p><a href="manifest.json">Manifest and receipt digests</a></p></main></body></html>']
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
