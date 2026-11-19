"""Verify referenced GitHub merge deliveries for an explicitly enabled report."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from evidence_adapter import github_repo


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DELIVERY = re.compile(
    r"https://api\.github\.com/repos/([a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+)/hooks/([1-9][0-9]{0,18})/deliveries/([1-9][0-9]{0,18})"
)
_CLOSED_PULL_REQUEST = """.request.payload as $p | $p.pull_request as $pr | {
  id, event, action, repository_id, delivered_at,
  payload_action: $p.action, repo_id: $p.repository.id,
  repository: $p.repository.full_name, pr: $p.number, pr_number: $pr.number,
  merged: $pr.merged, merged_at: $pr.merged_at, merge_commit_sha: $pr.merge_commit_sha,
  candidate_sha: $pr.head.sha, head_ref: $pr.head.ref, head_repository: $pr.head.repo.full_name,
  base_ref: $pr.base.ref, base_repository: $pr.base.repo.full_name, base_sha: $pr.base.sha
}"""


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _read_delivery(root: Path, endpoint: str) -> dict | None:
    # Select only provenance and merge bindings. The full payload can contain
    # private PR content and is neither stored nor included in the report.
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
    env.pop("GH_DEBUG", None)
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", "--jq", _CLOSED_PULL_REQUEST, endpoint],
            cwd=root, env=env, capture_output=True, timeout=8,
        )
        if result.returncode or len(result.stdout) > 64 * 1024:
            return None
        delivery = json.loads(result.stdout.decode("utf-8"))
    except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
        return None
    return delivery if isinstance(delivery, dict) else None


def _match_delivery(delivery: dict, delivery_id: int, repo: str, cfg: dict,
                    job: dict, event: dict, ref: dict, as_of: datetime) -> bool:
    merged = _time(delivery.get("merged_at"))
    delivered = _time(delivery.get("delivered_at"))
    observed = _time(event.get("occurred_at"))
    repo_id = delivery.get("repository_id")
    base_bindings = [part.get("base_sha") for part in (event, event["data"], ref) if part.get("base_sha")]
    return (
        type(delivery.get("id")) is int and delivery["id"] == delivery_id
        and delivery.get("event") == "pull_request" and delivery.get("action") == "closed"
        and delivery.get("payload_action") == "closed" and delivery.get("merged") is True
        and type(repo_id) is int and repo_id > 0 and type(delivery.get("repo_id")) is int
        and delivery["repo_id"] == repo_id
        and all(isinstance(delivery.get(field), str) and delivery[field].casefold() == repo.casefold()
                for field in ("repository", "base_repository", "head_repository"))
        and type(delivery.get("pr")) is int and type(delivery.get("pr_number")) is int
        and delivery["pr"] == delivery["pr_number"] == job["pr"]
        and delivery.get("head_ref") == job.get("branch") == f"factory/{job['id']}"
        and delivery.get("base_ref") == cfg.get("base_branch")
        and isinstance(delivery.get("base_sha"), str) and _SHA.fullmatch(delivery["base_sha"])
        and isinstance(delivery.get("merge_commit_sha"), str) and _SHA.fullmatch(delivery["merge_commit_sha"])
        and isinstance(delivery.get("candidate_sha"), str) and _SHA.fullmatch(delivery["candidate_sha"])
        and delivery["candidate_sha"] == event.get("candidate_sha")
        and ref.get("candidate_sha") in (None, delivery["candidate_sha"])
        and all(delivery.get("base_sha") == base for base in base_bindings)
        and merged is not None and delivered is not None and merged <= delivered <= as_of
        and observed in (None, merged)
    )


def _local_round(record: dict, facts: dict, pointers: list, as_of: datetime) -> int | None:
    guardrails = record["guardrails"]
    candidate = guardrails["candidate"]
    round_number = candidate["round"]
    if (not round_number or candidate["candidate_sha"] != facts["candidate_sha"] or guardrails["diagnostics"]
            or not all(event.get("round") == round_number for event, _, _, _ in pointers)):
        return None
    merged = _time(facts["merged_at"])
    history = [(step.get("stage"), _time(step.get("at"))) for step in record["job"].get("history", [])]
    if sum(stage == "build" and when is not None and when <= merged for stage, when in history) != round_number:
        return None
    locally_merged = any(stage == "merged" and when is not None and merged <= when <= as_of
                         for stage, when in history)
    bound_gate = any(observation.get("verified") and observation.get("status") == "pass"
                     and observation.get("candidate_sha") == facts["candidate_sha"]
                     and observation.get("round") == round_number
                     and (when := _time(observation.get("occurred_at"))) is not None and when <= merged
                     for row in guardrails["requirements"] if row["id"] == "checks"
                     for observation in row["observations"])
    return round_number if locally_merged or bound_gate else None


def verify_merge_delivery(root: Path, cfg: dict, record: dict, as_of: datetime) -> dict | None:
    """Return a sanitized verified/missing result, updating the reporting record on success."""
    job = record["job"]
    if job.get("origin") == "controlled":
        return None
    pointers = [event for event in record["events"]
                if event["kind"] == "check_evidence" and event["data"].get("check_id") == "merged_candidate"
                and event["data"].get("verdict") == "pass" and event["source"]["kind"] == "controller"]
    if not pointers:
        return None
    repo = github_repo(root)
    if not repo or type(job.get("pr")) is not int or job["pr"] <= 0 or not cfg.get("base_branch"):
        return {"status": "reference_unverified"}
    destinations = []
    for event in pointers:
        for ref in event.get("evidence", []):
            match = _DELIVERY.fullmatch(ref.get("url", ""))
            if match and match[1].casefold() == repo.casefold():
                destinations.append((event, ref, match[2], match[3]))
    # Duplicate submissions of one delivery can agree, but do not select a
    # winner among conflicting references or asserted candidate identities.
    if not destinations:
        return {"status": "reference_unverified"}
    if len({(hook, delivery, event.get("candidate_sha")) for event, _, hook, delivery in destinations}) != 1:
        return {"status": "ambiguous"}
    _, _, hook, identifier = destinations[0]
    endpoint = f"repos/{repo}/hooks/{hook}/deliveries/{identifier}"
    delivery = _read_delivery(root, endpoint)
    if delivery is None:
        return {"status": "unavailable"}
    if not all(_match_delivery(delivery, int(identifier), repo, cfg, job, event, ref, as_of)
               for event, ref, _, _ in destinations):
        return {"status": "mismatch"}
    clean = {"status": "verified", "url": f"https://api.github.com/{endpoint}", "pr": job["pr"],
             "merged_at": delivery["merged_at"], "candidate_sha": delivery["candidate_sha"],
             "merge_commit_sha": delivery["merge_commit_sha"], "base_ref": delivery["base_ref"],
             "base_sha": delivery["base_sha"]}
    round_number = _local_round(record, clean, destinations, as_of)
    if round_number:
        clean["round"] = round_number
        record["merged_round"] = round_number
    record.update(merged_at=clean["merged_at"], merged_candidate_sha=clean["candidate_sha"], merge_evidence=clean)
    return clean
