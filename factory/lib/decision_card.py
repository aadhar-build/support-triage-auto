"""Render the owner-facing evidence for one job without altering its state."""

import math
import re
from datetime import datetime
from functools import partial
from urllib.parse import quote, urlsplit

from observations import ObservationError, _url


_STATUSES = {"pass", "fail", "pending", "missing", "stale", "unverified", "not_required"}
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.IGNORECASE)
_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def _text(value):
    value = " ".join(str(value).split())
    return re.sub(r"([\\`*_[\]<>~|])", r"\\\1", value)


def _code(value):
    return "`" + " ".join(str(value).replace("`", "'").split()) + "`"


def _https_url(value):
    try:
        return _url(value, "decision card link")
    except ObservationError:
        return None


def _safe_path(path):
    return isinstance(path, str) and all(
        part not in {".", ".."} and _PATH_COMPONENT.fullmatch(part) for part in path.split("/")
    )


def _references(evidence, artifact_root):
    links = []
    for ref in evidence:
        url = _https_url(ref.get("url"))
        path = ref.get("path")
        if url:
            link = f"[{_text(ref['url'])}]({url})"
        elif _safe_path(path):
            link = f"[{_text(path)}]({quote(artifact_root + path, safe='/')})"
        else:
            continue
        if link not in links:
            links.append(link)
    return " · ".join(links)


def _with_links(line, evidence, links):
    references = links(evidence)
    return f"{line} · {references}" if references else line


def _candidate(guardrails):
    identity = guardrails.get("candidate") or {}
    sha = identity.get("candidate_sha")
    return sha if isinstance(sha, str) and _OBJECT_ID.fullmatch(sha) else None


def _candidate_status(event, candidate, current_round):
    sha = event.get("candidate_sha")
    if not candidate or not sha:
        return "unverified"
    if sha != candidate or (current_round and event.get("round") != current_round):
        return "stale"
    return None


def _row_evidence(row, candidate, links):
    status = row.get("status")
    if status not in _STATUSES:
        status = "unverified"
    evidence = row.get("evidence", [])
    if "observations" in row:
        evidence = []
        for event in row["observations"]:
            if status == "stale" or event.get("status") != status:
                continue
            if status == "pass" and not (event.get("verified") is True and event.get("candidate_sha") == candidate):
                continue
            evidence.extend(event.get("evidence", []))
    if status == "pass" and (not candidate or not links(evidence)):
        status = "unverified"
    return status, evidence


def _requirement_notes(row, candidate, links):
    notes = []
    for event in row.get("observations", []):
        status = event.get("status")
        if status == "stale":
            explanation = f"Stale evidence · candidate {_code(event.get('candidate_sha') or 'unknown')}"
            if event.get("verified") and event.get("verdict") in {"pass", "fail"}:
                explanation += f" · historical {event['verdict']}"
        elif row.get("required") is False:
            status, _ = _row_evidence({"status": status, "observations": [event]}, candidate, links)
            explanation = f"Optional evidence — {status.replace('_', ' ')}"
        else:
            continue
        notes.append(_with_links(f"  - {explanation}", event.get("evidence", []), links))
    legacy = links([{"path": path} for path in row.get("legacy_paths", [])])
    if legacy:
        notes.append(f"  - Historical evidence · {legacy}")
    return notes


def _criterion_rows(rows, events, candidate, current_round, links):
    reported = {}
    for event in events:
        if event["kind"] == "criterion":
            identifier = event["data"].get("criterion_id")
            if identifier:
                reported[f"criterion:{identifier}"] = event

    rendered = []
    blockers = []
    for row in rows:
        identifier = row["id"]
        criterion_id = _code(identifier.removeprefix("criterion:"))
        label = row.get("label")
        name = f"{_text(label)} ({criterion_id})" if label else criterion_id
        status, evidence = _row_evidence(row, candidate, links)
        observation = reported.pop(identifier, None)
        if status == "missing" and observation:
            status = _candidate_status(observation, candidate, current_round) or "unverified"
            evidence = observation.get("evidence", [])
        rendered.append(_render_row(name, status, row.get("required"), evidence, links))
        rendered.extend(_requirement_notes(row, candidate, links))
        if row.get("required") and status != "pass":
            blockers.append((identifier, status))

    if not rows:
        rendered.append("Agreed criteria are unrecorded.")
        if reported:
            rendered.append("")
    for identifier, event in reported.items():
        status = _candidate_status(event, candidate, current_round) or "unverified"
        rendered.append(_render_row(
            _code(identifier.removeprefix("criterion:")), status, None, event.get("evidence", []), links,
        ))
    return rendered, blockers


def _render_row(name, status, required, evidence, links):
    applicability = {True: "required", False: "optional", None: "requirement unconfirmed"}[required]
    return _with_links(f"- {name} — {status.replace('_', ' ')} ({applicability})", evidence, links)


def _check_rows(rows, candidate, links):
    rendered = []
    blockers = []
    for row in rows:
        status, evidence = _row_evidence(row, candidate, links)
        rendered.append(_render_row(_code(row["id"]), status, row.get("required"), evidence, links))
        rendered.extend(_requirement_notes(row, candidate, links))
        if row.get("required") and status != "pass":
            blockers.append((row["id"], status))
    return rendered or ["Applicable checks and reviews are unrecorded."], blockers


def _demo_rows(events, candidate, current_round, links, incomplete):
    rendered = []
    available = False
    for event in events:
        if event["kind"] != "demo":
            continue
        data = event["data"]
        evidence = event.get("evidence", [])
        status = _candidate_status(event, candidate, current_round)
        if status == "stale":
            status = f"stale · candidate {_code(event.get('candidate_sha') or 'unknown')}"
        elif status is None and data.get("verdict") == "pass" and links(evidence):
            available = True
            source = "owner-" if event.get("source", {}).get("kind") == "owner" else ""
            status = f"{source}reported demonstration"
        elif status is None and data.get("verdict") in {"fail", "pending", "missing"}:
            status = data["verdict"]
        else:
            status = "unverified"
        line = f"- {_code(data.get('demo_id') or 'demo')} — {status}"
        if data.get("criterion_id"):
            line += f" · Criterion {_code(data['criterion_id'])}"
        rendered.append(_with_links(line, evidence, links))
    if incomplete:
        status = "Current demonstration status is unknown because evidence collection is incomplete."
        rendered = [status, "", *rendered] if rendered else [status]
    elif not available:
        absent = "No working-software demonstration available for the current candidate."
        rendered = [absent, "", *rendered] if rendered else [absent]
    return rendered, available


def _limit_rows(events, candidate, current_round, links, incomplete):
    rendered = []
    for event in events:
        if event["kind"] != "limitation":
            continue
        line = f"- {_code(event['data'].get('code') or 'limitation')}"
        if event.get("candidate_sha"):
            status = _candidate_status(event, candidate, current_round)
            if status:
                line += f" · {status} for the current candidate"
        rendered.append(_with_links(line, event.get("evidence", []), links))
    if incomplete:
        if rendered:
            return ["The readable records may omit limitations.", "", *rendered]
        return ["Limitations unknown because evidence collection is incomplete."]
    return rendered or ["No limitations recorded."]


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _elapsed(job, events):
    history = job.get("history", [])
    context = [event["data"] for event in events if event["kind"] == "task_context"]
    started = _date(job.get("created_at")) or next((
        at for item in history if item.get("stage") == "intake" and (at := _date(item.get("at")))
    ), None)
    start_label = "intake"
    if started is None:
        start_label = "the recorded start"
        started = next((at for item in context if (at := _date(item.get("started_at")))), None)
    if started is None:
        return "unknown"

    ended = next((at for item in reversed(context) if (at := _date(item.get("ended_at")))), None)
    boundary = "recorded end"
    if ended is None and history and (len(history) > 1 or history[-1].get("stage") != "intake"):
        ended = _date(history[-1].get("at"))
        boundary = "the last recorded stage"
    if ended is None or ended < started:
        return "unknown"
    return f"{(ended - started).total_seconds() / 60:.2f}".rstrip("0").rstrip(".") + (
        f" minutes from {start_label} to {boundary}"
    )


def _owner_minutes(job, events, incomplete):
    effort = [event["data"].get("minutes") for event in events if event["kind"] == "owner_effort"]
    measured = [minutes for minutes in effort if _valid_minutes(minutes)]
    legacy = job.get("owner_minutes")
    if incomplete:
        if measured:
            return f"{sum(measured):g} minutes in readable observations (coverage unknown)"
        if not effort and _valid_minutes(legacy):
            return f"unknown · legacy aggregate {legacy:g} minutes"
        return "unknown"
    coverage_records = [event["data"] for event in events if event["kind"] == "owner_effort_coverage"]
    complete = any(
        data.get("complete") is True and data.get("scope") in {"job", "owner_effort"}
        and data.get("window_start") is None and data.get("window_end") is None
        for data in coverage_records
    )
    if measured or (complete and not effort):
        coverage = "complete coverage" if complete and len(effort) == len(measured) else "partial coverage"
        return f"{sum(measured):g} minutes recorded ({coverage})"
    if not effort and _valid_minutes(legacy):
        return f"{legacy:g} minutes (legacy aggregate)"
    return "unknown"


def _valid_minutes(value):
    return type(value) in {int, float} and value >= 0 and math.isfinite(value)


def _decisions(job, events, candidate, current_round, links, incomplete):
    owner_events = [
        event for event in events if event["kind"] == "owner_decision"
        and event.get("source", {}).get("kind") == "owner"
        and event["data"].get("disposition") in {"accepted", "rejected", "abandoned"}
    ]
    current = [event for event in owner_events if not event.get("candidate_sha") or (
        _candidate_status(event, candidate, current_round) is None
    )]
    owner = "- **Owner decision** — " + ("current status unknown" if incomplete else "unrecorded")
    disposition = None
    if current:
        event = current[-1]
        disposition = event["data"]["disposition"]
        if incomplete:
            owner += f" · last readable record reported {disposition}"
        else:
            owner = f"- **Owner decision** — {disposition}"
        if disposition == "accepted" and not incomplete:
            follow_up = {True: "material follow-up recorded", False: "no material follow-up"}
            owner += " · " + follow_up.get(event["data"].get("material_follow_up"), "follow-up unknown")
            if not event.get("candidate_sha"):
                owner += " · candidate unrecorded"
        owner = _with_links(owner, event.get("evidence", []), links)
    elif owner_events:
        event = owner_events[-1]
        owner = _with_links(
            owner + f" for the current candidate · older decision {event['data']['disposition']}"
            f" for {_code(event['candidate_sha'])}", event.get("evidence", []), links,
        )

    stage = job.get("stage")
    technical = {
        "approve": "pending approver decision",
        "merging": "controller recorded approval at the merging stage",
        "merged": "merged in the controller history",
        "rejected": "job rejected in the controller history",
        "needs-human": "pending human action",
        "recovery": "pending recovery",
    }.get(stage, "pending")
    return [owner, f"- **Technical decision** — {technical}"], disposition


def _next_step(stage, disposition, candidate, blockers, recovery, has_demo, has_criteria, has_checks):
    if stage in {"merged", "rejected"} or disposition in {"rejected", "abandoned"}:
        return "Review the recorded outcome before starting follow-up work."
    if recovery.get("owner_question"):
        return _text(recovery["owner_question"])
    for recommendation in recovery.get("recommendations", []):
        if recommendation.get("advice"):
            return _text(recommendation["advice"])
    if stage in {"needs-info", "needs-input"}:
        return "Answer the outstanding intake or product question."
    if not candidate:
        return "Capture the exact candidate for evidence review."
    if blockers:
        identifier, status = next((row for row in blockers if row[1] == "fail"), blockers[0])
        if status == "fail":
            return f"Resolve failed evidence for {_code(identifier)}."
        return f"Obtain verified current-candidate evidence for {_code(identifier)}."
    if not (has_criteria and has_checks):
        return "Record the agreed criteria and applicable checks for this job."
    if not has_demo:
        return "Record a working-software demonstration for the current candidate."
    if disposition is None:
        return "Review the demonstration and limitations, then record the owner decision."
    return "Review the recorded decisions and evidence in the normal approval workflow."


def render_decision_card(job: dict, events: list[dict], guardrails: dict, recovery: dict) -> str:
    """Return a Markdown card using current observations and read-only guardrail results."""
    events = sorted(
        (event for event in events if event.get("origin") == "real"),
        key=lambda event: event.get("recorded_at") or "",
    )
    identifier = _text(job.get("id") or "unknown job")
    title = job.get("title")
    stage = job.get("stage") or "unknown"
    root = job.get("artifact_root")
    artifact_root = f"{root}/" if _safe_path(root) else ""
    links = partial(_references, artifact_root=artifact_root)
    candidate = _candidate(guardrails)
    current_round = (guardrails.get("candidate") or {}).get("round")
    commit_url = _https_url(job.get("candidate_url"))
    candidate_display = _code(candidate) if candidate else "unknown"
    if candidate and commit_url and urlsplit(commit_url).path.endswith(f"/{candidate}"):
        candidate_display = f"[{candidate_display}]({commit_url})"
    heading = f"# Decision card · {identifier}"
    if title:
        heading += f" · {_text(title)}"
    details = [heading, "", f"- **Stage** — {_code(stage)}", f"- **Candidate** — {candidate_display}"]
    if current_round is not None:
        details.append(f"- **Round** — {current_round}")
    diagnostics = guardrails.get("diagnostics", [])
    incomplete = bool(diagnostics)
    if incomplete:
        details.append("- **Evidence collection incomplete** — The latest observations and evidence may be unavailable.")
        details.extend(f"  - {_text(diagnostic)}" for diagnostic in diagnostics)

    requirements = guardrails.get("requirements", [])
    criteria = [row for row in requirements if row["id"].startswith("criterion:")]
    checks = [row for row in requirements if not row["id"].startswith("criterion:")]
    criterion_rows, criteria_blocked = _criterion_rows(criteria, events, candidate, current_round, links)
    check_rows, checks_blocked = _check_rows(checks, candidate, links)
    demo_rows, has_demo = _demo_rows(events, candidate, current_round, links, incomplete)
    decision_rows, disposition = _decisions(job, events, candidate, current_round, links, incomplete)
    metrics_url = _https_url(job.get("metrics_url")) or quote(artifact_root + "job.json", safe="/")
    time_rows = [
        f"- **Owner active time** — {_owner_minutes(job, events, incomplete)}",
        f"- **Elapsed time** — {_elapsed(job, [] if incomplete else events)}",
        f"- [Job metrics]({metrics_url})",
    ]
    if incomplete:
        next_step = "Resolve the evidence collection diagnostics and regenerate this card before acting on the decision."
    else:
        next_step = _next_step(
            stage, disposition, candidate, criteria_blocked + checks_blocked, recovery, has_demo,
            bool(criteria), bool(checks),
        )
    for section, rows in (
        ("Acceptance criteria", criterion_rows),
        ("Checks and technical reviews", check_rows),
        ("Working-software demonstration", demo_rows),
        ("Known limitations", _limit_rows(events, candidate, current_round, links, incomplete)),
        ("Decision and time", decision_rows + time_rows),
        ("Next step", [next_step]),
    ):
        details.extend(["", f"## {section}", "", *rows])
    return "\n".join(details) + "\n"
