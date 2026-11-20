"""Render one private, offline decision view from a captured job snapshot."""

import re
from collections import defaultdict
from html import escape

from decision_card import _elapsed, _next_step, _owner_minutes, _technical_status
from factory_recovery import report_recovery_summary
from scorecard_report import (
    latest_owner_decision, owner_decision_evidence_incomplete, owner_decision_matches, owner_decision_reports,
    owner_disposition, submission_times, task_context,
)


_RECEIPT_NAME = re.compile(
    r"(?:receipt-[0-9]{3}|gate-round-[1-9][0-9]{0,5}|review-(?:code|security|eval)|final-checks|decision)\.json"
)
_REQUIREMENT_LABELS = {"checks": "Round gate", "final_checks": "Final gate", "approval": "Technical decision"}
_BINDING_LABELS = {"candidate_sha": "candidate", "round": "round", "base_sha": "base", "spec_digest": "spec"}
_COST_LABELS = {
    "subscription_usage": "Subscription usage",
    "subscription_charge": "Subscription charge",
    "incremental_charge": "Incremental charge",
    "api_equivalent_estimate": "API-equivalent estimate",
}
OWNER_VIEW_STYLE = """
main { overflow-wrap: anywhere; }
dt { font-weight: 600; margin-block-start: .75rem; }
dd { margin-inline-start: 0; }
td ul { margin: 0; padding-inline-start: 1.15rem; }
"""


def observation_key(event):
    """Keep associations inside the exporter. Only its private page uses them."""
    kind, data = event["kind"], event["data"]
    if kind == "check_evidence":
        return "requirement", data["check_id"]
    if kind == "criterion":
        return "requirement", f"criterion:{data['criterion_id']}"
    return "demo", event["event_id"]


def _reported(rows, native_available=None):
    entries = []
    for row in rows:
        filename = row.get("file")
        local_link = (f'<a href="{escape(filename)}">Open receipt</a>'
                      if filename and _RECEIPT_NAME.fullmatch(filename) else "")
        detail = f"{row['reported_verdict']} · {row['artifact_status']}"
        coverage = row["_owner_bindings"]
        detail += " · " + ", ".join(f"{label} {coverage[key]}" for key, label in _BINDING_LABELS.items())
        if row.get("_owner_event_blocked") and not {"unknown", "stale"}.intersection(coverage.values()):
            detail += " · another reference has a stale or unresolved binding"
        entries.append(f"<li>{escape(detail)}{' ' + local_link if local_link else ''}</li>")
    if native_available:
        entries.append(f"<li>{escape(native_available)}</li>")
    if not entries:
        return "unknown"
    return "<ul>" + "".join(dict.fromkeys(entries)) + "</ul>"


def _local_receipt_status(requirement, incomplete):
    if incomplete or not requirement:
        return "unknown"
    results = {claim["verdict"] for claim in requirement.get("observations", ())
               if claim.get("verified") is True and claim.get("status") in {"pass", "fail"}
               and claim.get("verdict") == claim["status"]}
    if len(results) > 1:
        return "conflicting"
    return next(iter(results), "unknown")


def _requirement_label(requirement):
    identifier = requirement["id"]
    if identifier.startswith("criterion:"):
        identifier = identifier.removeprefix("criterion:")
        label = requirement.get("label")
        return f"{label} ({identifier})" if label else identifier
    if identifier.startswith("review:"):
        reviewer = identifier.removeprefix("review:")
        return f"{reviewer.capitalize()} review"
    return requirement.get("label") or _REQUIREMENT_LABELS.get(identifier, identifier)


def _links_to_criterion(row, requirement):
    coverage = row["_owner_bindings"]
    return (not row["_owner_event_blocked"]
            and coverage["candidate_sha"] == coverage["round"] == "matched"
            and (requirement["criterion_identity"] == "explicit" or coverage["spec_digest"] == "matched"))


def _native_availability(requirement, selected):
    existing = set(requirement.get("legacy_paths", ())) if requirement else set()
    existing -= {row["reference"] for row in selected if row.get("file")}
    if not existing:
        return None
    if any(row["reference"] in existing for row in selected):
        return "Native report available locally · projection unavailable"
    return "Native report available locally · unselected · bindings unknown"


def _table(title, rows):
    lines = [f"<h2>{escape(title)}</h2>",
             "<table><thead><tr><th>Requirement</th><th>Source reports</th>"
             "<th>Local receipt validation</th></tr></thead><tbody>"]
    for label, reported, local_status in rows:
        lines.append(f'<tr><td data-label="Requirement">{escape(label)}</td>'
                     f'<td data-label="Source reports">{reported}</td>'
                     f'<td data-label="Local receipt validation">{escape(local_status)}</td></tr>')
    lines.append("</tbody></table>")
    return lines


def _event_binding(event, bindings, binding_status):
    status = binding_status(bindings, (event, event["data"], *event["evidence"]))
    if status == "stale":
        return "stale"
    if status or not event.get("candidate_sha") or not event.get("round"):
        return "binding unknown"
    return "current binding"


def _owner_decision(job, events, bindings, cutoff, incomplete):
    if incomplete:
        return "Current owner decision unknown · evidence collection incomplete", None
    context = task_context(events, cutoff)
    submitted_at, _ = submission_times(job, context, cutoff)
    reports = owner_decision_reports(events, cutoff, submitted_at)
    if not reports:
        return "Owner decision unrecorded", None
    current = [event for event in reports if owner_decision_matches(event, bindings, context)]
    if not current:
        verdict = owner_disposition(reports[-1]) or "unknown"
        return f"Current owner decision unknown · most recently received report {verdict}", None
    event = latest_owner_decision(current)
    if event is None:
        return "Current owner decision unknown · owner report dates incomplete", None
    verdict = owner_disposition(event)
    if verdict is None:
        return "Current owner decision unknown · owner disposition unrecorded", None
    follow_up = {True: "material follow-up recorded", False: "no material follow-up"}
    summary = f"Owner reported {verdict}"
    if verdict == "accepted":
        summary += " · " + follow_up.get(event["data"].get("material_follow_up"), "follow-up unknown")
    if len(current) != len(reports):
        summary += " · other owner reports have stale or unknown bindings"
    return summary, verdict


def _metric_lines(job, events, cutoff):
    rows = [f"<dt>Owner active time</dt><dd>{escape(_owner_minutes(job, events, False))}</dd>",
            f"<dt>Elapsed time</dt><dd>{escape(_elapsed(job, events, cutoff=cutoff))}</dd>"]
    recorded_costs = []
    for event in events:
        if event["kind"] != "usage_cost":
            continue
        data = event["data"]
        label = _COST_LABELS[data["cost_type"]]
        amount = data.get("amount")
        if amount is None:
            value = "amount unknown"
        else:
            unit = data.get("currency") or data.get("unit")
            value = f"{amount} {unit}" if unit else f"{amount} · unit unknown"
        recorded_costs.append(f"<li>{escape(label)} · {escape(value)}</li>")
    if recorded_costs:
        costs = "<ul>" + "".join(recorded_costs) + "</ul>"
    elif job.get("cost_usd") is not None:
        costs = escape(f"Legacy amount {job['cost_usd']:g} USD · cost type unknown")
    else:
        costs = "unknown"
    rows.append(f"<dt>Reported usage and cost records</dt><dd>{costs}</dd>")
    return rows


def render_owner_view(job, events, bindings, context, projected_rows, cutoff, header, binding_status):
    """Render only escaped text and links to explicitly materialized receipt names."""
    guardrails = context["guardrails"]
    owner_incomplete = owner_decision_evidence_incomplete(guardrails)
    incomplete = bool(guardrails.get("diagnostics")) or owner_incomplete
    verified_rows = {row["id"]: row for row in guardrails["requirements"]}
    typed = defaultdict(list)
    native = {}
    for row in projected_rows:
        if key := row.get("_owner_key"):
            typed[key].append(row)
        else:
            native[row["reference"]] = row
    criteria, checks, demos = [], [], []
    criteria_blockers, checks_blockers = [], []
    for requirement in context["required"]:
        identifier = requirement["id"]
        label = _requirement_label(requirement)
        required = requirement.get("required", True)
        status_row = verified_rows.get(identifier)
        local_status = _local_receipt_status(status_row, incomplete)
        key = ("requirement", identifier)
        source_rows = typed.pop(key, [])
        is_criterion = identifier.startswith("criterion:")
        if is_criterion:
            linked, unmatched = [], []
            for row in source_rows:
                group = linked if _links_to_criterion(row, requirement) else unmatched
                group.append(row)
            if unmatched:
                typed[key] = unmatched
            source_rows = linked
        native_rows = [native.pop(path) for path in requirement.get("legacy_paths", ()) if path in native]
        reported = _reported(source_rows + native_rows, _native_availability(status_row, native_rows))
        row_label = label if required else f"{label} · Optional"
        (criteria if is_criterion else checks).append((row_label, reported, local_status))
        if required and local_status != "pass":
            (criteria_blockers if is_criterion else checks_blockers).append((label, local_status))

    for event in events:
        if event["kind"] != "demo":
            continue
        key = observation_key(event)
        data = event["data"]
        status = _event_binding(event, bindings, binding_status)
        label = data["demo_id"]
        if data.get("criterion_id"):
            label += f" · Reported criterion ID {data['criterion_id']}"
        verdict = data["verdict"] or "unknown"
        label += f" · {verdict} · {status}"
        demos.append((label, _reported(typed.pop(key, [])), "unknown"))

    additional = [
        (f"{identifier} · current wording association unknown" if identifier.startswith("criterion:") else identifier,
         _reported(rows), "unknown")
        for (_, identifier), rows in typed.items()
    ]
    additional.extend((path, _reported([row]), "unknown") for path, row in native.items())
    limitations = [(event["data"]["code"], _event_binding(event, bindings, binding_status))
                   for event in events if event["kind"] == "limitation"]
    owner, disposition = _owner_decision(job, events, bindings, cutoff, owner_incomplete)
    recovery = report_recovery_summary({"job": job, "events": events, "guardrails": guardrails})
    stage = job.get("stage") or "unknown"
    action = ("Resolve the evidence collection diagnostics and regenerate this view before acting on the decision."
              if incomplete else _next_step(stage, disposition, bindings["candidate_sha"],
                                            criteria_blockers + checks_blockers, recovery,
                                            False, bool(criteria), bool(checks), text=str, code=str))
    lines = [*header,
             "<p>This local export contains private job details. Keep the directory on the owner’s device.</p>",
             f"<p>{escape(str(job.get('title') or job['id']))}</p>",
             f"<p>Issue {escape(str(job.get('issue') or 'unknown'))} · Stage {escape(stage)}"
             f" · Round {escape(str(bindings['round'] or 'unknown'))}<br>"
             f"Local candidate <code>{escape(bindings['candidate_sha'] or 'unknown')}</code><br>"
             f"Observation cutoff <time>{escape(cutoff.isoformat())}</time></p>",
             "<h2>Next owner action</h2>", f"<p>{escape(action)}</p>",
             "<p>Local receipt validation uses factory-host records for the current candidate. "
             "Independent verification is unknown because this view uses only factory-host evidence.</p>"]
    if criteria:
        lines.extend(_table("Acceptance criteria", criteria))
    else:
        status = "unreadable" if context["spec_status"] == "withheld" else "unavailable"
        lines.extend(["<h2>Acceptance criteria</h2>", f"<p>Agreed criteria {status}.</p>"])
    lines.extend(_table("Checks and technical reviews", checks))
    if demos:
        lines.extend(_table("Working-software demonstration", demos))
        lines.append("<p>These receipts record demonstration reports. Working software is unavailable in this export.</p>")
    else:
        lines.extend(["<h2>Working-software demonstration</h2>",
                      "<p>No working-software demonstration available for the current candidate.</p>"])
    if additional:
        lines.extend(_table("Additional evidence", additional))
    lines.append("<h2>Known limitations</h2>")
    if limitations:
        lines.append("<ul>" + "".join(f"<li>{escape(code)} · {escape(status)}</li>"
                                      for code, status in limitations) + "</ul>")
    else:
        lines.append("<p>Limitations unrecorded.</p>")
    lines.extend(["<h2>Decisions and metrics</h2>", "<dl>",
                  f"<dt>Technical decision</dt><dd>{escape(_technical_status(stage))}</dd>",
                  f"<dt>Owner decision</dt><dd>{escape(owner)}</dd>",
                  *_metric_lines(job, events, cutoff), "</dl>",
                  '<p><a href="manifest.json">Manifest and digests</a></p></main></body></html>'])
    return "\n".join(lines).encode("utf-8")
