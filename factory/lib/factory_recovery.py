"""Read-only recovery advice and failure-to-regression traceability."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path


_CAP_NOTE = re.compile(
    r"(?P<reason>gate failed|changes requested by [^\r\n]+); (?P<rounds>[1-9]\d*) rounds used"
)
_PROPOSALS = {
    "split_scope": "scope split",
    "permitted_alternative": "alternative approach",
    "follow_up_debt": "follow-up debt",
}
# A requested owner question is still pending, even when written by the owner.
_OWNER_CHOICES = {*_PROPOSALS, "retry", "stop"}
_PENDING_OUTCOMES = {"failed", "unresolved", "no_safe_recovery"}
_CLOSED_DEFECTS = {"resolved", "corrected", "fixed"}


def _failure_history(job: dict) -> tuple[list[dict], dict | None]:
    history = job.get("history", [])
    attempts = []
    exhaustion = None
    for index, transition in enumerate(history):
        stage = transition.get("stage")
        note = transition.get("note") or ""
        cap = _CAP_NOTE.fullmatch(note) if stage == "recovery" else None
        if stage != "build" and not cap:
            continue
        reason = cap["reason"] if cap else note
        if reason == "gate failed":
            kind = "gate_failure"
        elif reason.startswith("changes requested by "):
            kind = "review_changes_requested"
        else:
            continue
        attempt = {"artifact": "job.json", "history_index": index,
                   "at": transition.get("at"), "kind": kind}
        attempts.append(attempt)
        if cap and index == len(history) - 1:
            round_number, round_cap = job.get("round"), int(cap["rounds"])
            if type(round_number) is int and round_number >= round_cap:
                exhaustion = {**attempt, "round": round_number, "round_cap": round_cap}
    return attempts, exhaustion


def _reported(records: list[dict], fields: tuple[str, ...]) -> dict:
    """Combine compatible duplicate reports, leaving disagreements visible."""
    row = {"job_id": records[0]["job_id"], "observations": records, "conflicts": []}
    for field in fields:
        values = {record["data"][field] for record in records
                  if record["data"].get(field) is not None}
        row[field] = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            row["conflicts"].append(field)
    return row


def _group(events: list[dict], kind: str, field: str) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for event in events:
        if event["kind"] == kind and event["data"].get(field):
            groups[event["data"][field]].append(event)
    return groups


def _elapsed(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        seconds = (last - first).total_seconds()
    except (ValueError, TypeError):
        return None
    return seconds if seconds >= 0 else None


def _trace(events: list[dict]) -> dict:
    by_id = {event["event_id"]: event for event in events}
    for event in events:
        for predecessor in event.get("superseded_event_ids", ()):
            by_id.setdefault(predecessor, event)
        if predecessor := event.get("supersedes_event_id"):
            by_id.setdefault(predecessor, event)

    interruptions = {}
    for key, records in _group(events, "interruption", "interruption_id").items():
        row = _reported(records, ("started_at", "ended_at", "outcome"))
        interruptions[key] = {"interruption_id": key, **row,
                              "elapsed_seconds": _elapsed(row["started_at"], row["ended_at"])}

    def linked(event_id, kind=None):
        event = by_id.get(event_id)
        if event and (kind is None or event["kind"] == kind):
            return event
        return None

    def linked_many(records, field, kind):
        found = {}
        for record in records:
            for event_id in record["data"].get(field) or []:
                if target := linked(event_id, kind):
                    found[target["event_id"]] = target
        return list(found.values())

    def trace_rows(kind, key, fields):
        rows = []
        for value, records in _group(events, kind, key).items():
            row = _reported(records, (*fields, "impact", "proposed_recovery", "outcome", "cause",
                                      "correction_event_id", "regression_check_event_id", "no_check_reason"))
            row["reported_cause"] = row["cause"]
            row["cause"] = None  # The observation store has no independent evidence verdict.
            correction = linked(row["correction_event_id"])
            check = linked(row["regression_check_event_id"], "check_evidence")
            record_ids = {record["event_id"] for record in records}
            if correction and (correction["event_id"] in record_ids or not correction.get("evidence")):
                correction = None
            if check and (not check.get("candidate_sha") or not check.get("evidence")):
                check = None
            no_check_reason = row["no_check_reason"]
            if check and no_check_reason:
                regression_status = "conflicting"
            elif check:
                regression_status = "reported_link"
            elif no_check_reason:
                regression_status = "no_suitable_check"
            else:
                regression_status = "pending"
            effort = linked_many(records, "owner_effort_event_ids", "owner_effort")
            minutes = [entry["data"]["minutes"] for entry in effort if entry["data"].get("minutes") is not None]
            incident_ids = dict.fromkeys(
                entry["data"].get("interruption_id")
                for entry in linked_many(records, "interruption_event_ids", "interruption")
            )
            rows.append({key: value, **row, "correction": correction, "regression_check": check,
                         "correction_status": "reported_link" if correction else "pending",
                         "regression_status": regression_status,
                         "owner_effort": effort, "known_owner_minutes": sum(minutes) if minutes else None,
                         "linked_interruptions": [interruptions[i] for i in incident_ids if i in interruptions]})
        return rows

    defects = trace_rows("defect", "defect_id", ("origin", "discovered_at"))
    return {
        "defects": defects,
        "unresolved_defects": [row["defect_id"] for row in defects if row["outcome"] not in _CLOSED_DEFECTS],
        "decisions": trace_rows("recovery_decision", "failure_id", ("decision",)),
        "interruptions": list(interruptions.values()),
        "failed_checks": [event for event in events
                          if event["kind"] == "check_evidence" and event["data"].get("verdict") == "fail"],
    }


def _suggest(job: dict, events: list[dict], candidate_sha: str | None, *,
             include_failure_id: bool = True) -> tuple[list[dict], str | None]:
    current = defaultdict(list)
    for failure_id, records in _group(events, "recovery_decision", "failure_id").items():
        for record in records:
            if record.get("round") != job.get("round"):
                continue
            bindings = [record, *record.get("evidence", ())]
            if any(ref.get("candidate_sha") not in (None, candidate_sha)
                   or ref.get("round") not in (None, job.get("round")) for ref in bindings):
                continue
            current[failure_id, record.get("candidate_sha")].append(record)

    pending = []
    selected = set()
    conflicting = False
    for records in current.values():
        choices = {record["data"].get("decision") for record in records
                   if record["source"]["kind"] == "owner"} & _OWNER_CHOICES
        needs_follow_up = any(record["data"].get("outcome") in _PENDING_OUTCOMES for record in records)
        if len(choices) == 1 and not needs_follow_up:
            selected.update(choices)
        else:
            pending.extend(records)
            conflicting |= len(choices) > 1

    conflicting |= len(selected) > 1
    if current and not pending and not conflicting:
        return [], None

    relevant = pending or [record for records in current.values() for record in records]
    scope = ""
    if include_failure_id:
        failures = {record["data"]["failure_id"] for record in relevant}
        if len(failures) == 1:
            scope = f" for {next(iter(failures))}"
    proposal_ids = []
    proposals = [record for record in relevant if record["data"].get("proposed_recovery")]
    proposed = {record["data"]["proposed_recovery"] for record in proposals}
    proposal = _PROPOSALS.get(next(iter(proposed))) if len(proposed) == 1 else None
    blocked = any(record["data"].get("outcome") == "no_safe_recovery" for record in relevant)
    if conflicting:
        question = f"Which recovery choice should job {job['id']} use{scope}?"
    elif proposal and not blocked:
        question = f"Should job {job['id']} stay paused while the reported {proposal}{scope} is reviewed?"
        proposal_ids = [record["event_id"] for record in proposals]
    else:
        question = f"Should job {job['id']} stay paused and its failed scope{scope} be recorded as follow-up debt?"
    recommendation = {"action": "ask_owner", "advice": question}
    if proposal_ids:
        recommendation["reported_proposal_event_ids"] = proposal_ids
    return [recommendation], question


def recovery_summary(job: dict, events: list[dict], *, candidate_sha: str | None = None) -> dict:
    """Summarize a job and its current v1 observations.

    Use ``load_events(..., include_ancestry=True)`` to resolve older links through
    corrections. Outcomes and evidence references remain reported observations.
    """
    job_id = job.get("id")
    scoped = {event["event_id"]: event for event in events
              if event.get("job_id") == job_id and event.get("schema_version") == 1}
    attempts, exhaustion = _failure_history(job)
    real_events = [event for event in scoped.values() if event.get("origin") == "real"]
    result = _trace(real_events)
    in_recovery = job.get("stage") == "recovery"
    recommendations, question = _suggest(job, real_events, candidate_sha) if in_recovery and exhaustion else ([], None)
    return deepcopy({
        "job_id": job_id,
        "stage": job.get("stage"),
        "retry_exhausted": (True if exhaustion else None) if in_recovery else False,
        "retry_exhaustion": exhaustion if in_recovery else None,
        "failed_attempts": attempts,
        "recommendations": recommendations,
        "owner_question": question,
        **result,
        "controlled": _trace([event for event in scoped.values() if event.get("origin") == "controlled"]),
    })


def report_recovery_summary(record: dict) -> dict:
    guardrails = record["guardrails"]
    checked = any(row["id"] == "checks" and row["status"] in {"pass", "fail"}
                  for row in guardrails["requirements"])
    candidate = guardrails["candidate"]["candidate_sha"] if checked and not guardrails["diagnostics"] else None
    return recovery_summary(record["job"], record["events"], candidate_sha=candidate)


def recovery_transition_output(status: str, job_dir: Path, job: dict, *, candidate_sha: str | None = None) -> str:
    """Add one owner question after a gate or tally exhausts the retry cap."""
    if job["stage"] != "recovery":
        return status

    from observations import load_events

    events, diagnostics = load_events(job_dir, for_transition=True)
    job_id = job["id"]
    if diagnostics:
        question = f"Should job {job_id} stay paused until its recovery observations can be checked?"
    else:
        current = [event for event in events if event["origin"] == "real" and event["job_id"] == job_id]
        _, question = _suggest(job, current, candidate_sha, include_failure_id=False)
        if question is None:
            question = f"Should job {job_id} stay paused while its recorded recovery decision is reviewed?"
    return f"{status}\nRecovery: {question}"
