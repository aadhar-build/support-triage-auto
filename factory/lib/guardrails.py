"""Read-only guardrail reporting for the current, as-of-projected observations."""
import hashlib
import os
import re
import stat
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .observations import _open_job
else:
    from observations import _open_job


_GIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 4 * 1024 * 1024
_BINDINGS = ("candidate_sha", "base_sha", "spec_digest", "round")


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(timezone.utc) if timestamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def read_job_artifact(job_dir, path, *, max_bytes=_MAX_BYTES):
    """Bound reads and refuse symlinks/special files at every job-relative component."""
    if not isinstance(path, str) or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    parts = path.split("/")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(_open_job(Path(job_dir)))
            for part in parts[:-1]:
                directory = os.open(part, directory_flags, dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= max_bytes:
                    content = stream.read(max_bytes + 1)
                    after = os.fstat(fd)
                    if (len(content) == info.st_size and after.st_nlink == 1
                            and info.st_ctime_ns == after.st_ctime_ns):
                        return content
    except (OSError, ValueError):
        pass
    return None


def _verified(job_dir, event, verifier):
    if not callable(verifier) or not event.get("evidence"):
        return False
    validated = False
    reads = []
    for ref in event["evidence"]:
        if not isinstance(ref, dict):
            return False
        if "path" in ref:
            content = read_job_artifact(job_dir, ref["path"])
            if content is None or hashlib.sha256(content).hexdigest() != ref.get("sha256"):
                return False
            reads.append((ref["path"], content))
        else:
            try:
                url = urlsplit(ref.get("url", ""))
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    return False
            except (TypeError, ValueError):
                return False
            content = None
        # Only trusted installation code can install this callback. It must compare the
        # observation's facts to its independent source, including all applicable bindings.
        try:
            if verifier(event, ref, content) is True:
                validated = True
        except Exception:
            # A missing/unreachable source or an adapter error leaves its claim unverified.
            return False
    return validated and all(read_job_artifact(job_dir, path) == payload for path, payload in reads)


def _binding(event, candidate):
    data = event["data"]
    stale = False
    for key in _BINDINGS:
        expected = candidate.get(key)
        values = [record[key] for record in (event, data, *event.get("evidence", ()))
                  if isinstance(record, dict) and record.get(key) is not None]
        if key in ("base_sha", "spec_digest") and expected is None and not values:
            continue
        if key == "round":
            valid = all(type(v) is int and v > 0 for v in [expected, *values])
        else:
            pattern = _DIGEST if key == "spec_digest" else _GIT_ID
            valid = all(isinstance(v, str) and pattern.fullmatch(v) for v in [expected, *values])
        if not values or not valid or any(v != values[0] for v in values):
            return "unverified"
        stale |= values[0] != expected
    return "stale" if stale else "current"


def _provenance(events):
    evidence = []
    for event in events:
        for ref in event.get("evidence", []):
            clean = {k: ref[k] for k in ("path", "sha256", "url") if k in ref}
            if clean and clean not in evidence:
                evidence.append(clean)
    return {"event_ids": sorted({e["event_id"] for e in events}), "evidence": evidence}


def _requirements(job_dir, events, required, candidate):
    checks = defaultdict(list)
    for e in events:
        if e["kind"] == "check_evidence":
            checks[e["data"].get("check_id")].append(e)
        elif e["kind"] == "criterion" and e["data"].get("criterion_id"):
            checks["criterion:" + e["data"]["criterion_id"]].append(e)
    rows = []
    for requirement in required:
        rule = {"id": requirement} if isinstance(requirement, str) else requirement
        relevant = checks[rule["id"]]
        statuses = set()
        observations = []
        for e in relevant:
            binding = _binding(e, candidate)
            verdict = e["data"].get("verdict")
            status = binding
            verified = (not candidate.get("unstable") and binding in ("current", "stale")
                        and verdict in ("pass", "fail") and _verified(job_dir, e, candidate.get("verify_evidence")))
            if binding == "current":
                if verdict == "pending":
                    status = "pending"
                elif verified:
                    status = verdict
                else:
                    status = "unverified"
            statuses.add(status)
            observations.append({"event_id": e["event_id"], "candidate_sha": e.get("candidate_sha"),
                                 "round": e.get("round"), "status": status, "verdict": verdict,
                                 "base_sha": e.get("base_sha") or e["data"].get("base_sha"),
                                 "spec_digest": e.get("spec_digest") or e["data"].get("spec_digest"),
                                 "verified": verified, "occurred_at": e.get("occurred_at"),
                                 "source": e.get("source"), **_provenance([e])})
        legacy = [path for path in rule.get("legacy_paths", ()) if read_job_artifact(job_dir, path) is not None]
        status = next((s for s in ("fail", "pending", "unverified", "pass", "stale") if s in statuses),
                      "unverified" if legacy else "missing")
        applicable = rule.get("required", True)
        rows.append({"id": rule["id"], "required": applicable, "status": status if applicable else "not_required",
                     "label": rule.get("label"), "observations": observations, "legacy_paths": legacy, **_provenance(relevant)})
    return rows


def _group(events, id_field):
    groups = defaultdict(list)
    for e in events:
        if e["data"].get(id_field):
            groups[e["data"][id_field]].append(e)
    return sorted(groups.items())


def _incident_records(job_dir, events, verifier):
    records = []
    for identifier, group in _group(events, "incident_id"):
        confirmed = [e for e in group if e["data"].get("status") == "confirmed"]
        verified = [e for e in confirmed if _verified(job_dir, e, verifier)]
        selected = verified or confirmed or group
        first = selected[0]
        data = first["data"]
        # Conflicting claims require adjudication, even when one competing claim
        # has yet to acquire evidence. A blocked attempt cannot clear that conflict.
        dispositions = {e["data"].get("disposition") or "unknown" for e in group}
        disposition = next(iter(dispositions)) if len(dispositions) == 1 else "unknown"
        # All unsuperseded claims matter for attribution, including uncertain ones.
        # Keep scope and occurrence paired so the requested period can be evaluated.
        claims = [{"event_id": e["event_id"], "scope": e["data"].get("scope"),
                   "occurred_at": e.get("occurred_at")} for e in group]
        scopes = {claim["scope"] for claim in claims}
        occurrences = {claim["occurred_at"] for claim in claims}
        status = "unknown"
        if confirmed:
            status = "confirmed"
        elif any(e["data"].get("status") == "suspected" for e in group):
            status = "suspected"
        records.append({"incident_id": identifier, "status": status, "claims": claims,
                        "disposition": disposition, "verified": bool(verified) and len(dispositions) == 1,
                        "category": data.get("category"), "scope": data.get("scope") if len(scopes) == 1 else None,
                        "occurred_at": first.get("occurred_at") if len(occurrences) == 1 else None,
                        "source": first.get("source"), "resolution_ref": data.get("resolution_ref"),
                        **_provenance(group)})
    return records


def _period(record):
    return _instant(record.get("window_start")), _instant(record.get("window_end"))


def _coverage_windows(job_dir, events, verifier, as_of):
    cutoff = _instant(as_of.isoformat() if isinstance(as_of, datetime) else as_of)
    if as_of is not None and cutoff is None:
        raise ValueError("as_of must include its time zone")
    windows = []
    for event in events:
        if event["kind"] != "observation_coverage":
            continue
        observed, recorded = _instant(event.get("occurred_at")), _instant(event.get("recorded_at"))
        if cutoff and any(t is not None and t > cutoff for t in (observed, recorded)):
            continue
        data = event["data"]
        left, right = _period(data)
        end = min(right, observed, recorded) if right and observed and recorded else None
        if left is None or end is None or end <= left:
            end = None
        complete = data.get("complete") is True
        verified = bool(complete and end and data.get("scope") and _verified(job_dir, event, verifier))
        windows.append({"scope": data.get("scope"), "window_start": data.get("window_start"),
                        "window_end": end.isoformat().replace("+00:00", "Z") if end else None,
                        "declared_window_end": data.get("window_end"), "occurred_at": event.get("occurred_at"),
                        "recorded_at": event.get("recorded_at"), "complete": complete, "verified": verified,
                        **_provenance([event])})
    return windows


def _effective_total(window, windows, records):
    if not window or not window.get("scope"):
        return None
    start, end = _period(window)
    if start is None or end is None or start >= end:
        return None
    # Adjacent complete observation windows can jointly cover a requested interval.
    intervals = sorted(_period(w) for w in windows if w["verified"] and w["scope"] == window["scope"])
    through = start
    for left, right in intervals:
        if left <= through:
            through = max(through, right)
    if through < end:
        return None
    count = 0
    for r in records:
        membership = set()
        for claim in r["claims"]:
            scope = claim["scope"]
            when = _instant(claim["occurred_at"])
            if ((scope is not None and scope != window["scope"])
                    or (when is not None and not start <= when < end)):
                membership.add(False)
            elif when is None or scope is None:
                membership.add(None)
            else:
                membership.add(True)
        if membership == {False} or (r["verified"] and r["disposition"] == "blocked"):
            continue
        if membership != {True} or not r["verified"] or r["disposition"] != "effective":
            return None
        count += 1
    return count


def _interruptions(job_dir, events, verifier):
    records = []
    for identifier, group in _group(events, "interruption_id"):
        # load_events already projects corrections. Among the remaining independent
        # reports, recording order cannot establish when an interruption resumed.
        dates = [(e, _instant(e.get("occurred_at"))) for e in group]
        latest = max((when for _, when in dates if when is not None), default=None)
        current = [e for e, when in dates if when is None or when == latest]
        e = current[0]
        data = e["data"]
        outcomes = {item["data"].get("outcome") for item in current}
        outcome = data.get("outcome") if len(outcomes) == 1 else "unresolved"
        starts = {item["data"].get("started_at") for item in current}
        ends = {item["data"].get("ended_at") for item in current}
        started_at = data.get("started_at") if len(starts) == 1 else None
        ended_at = data.get("ended_at") if len(ends) == 1 and len(outcomes) == 1 else None
        start, end = _instant(started_at), _instant(ended_at)
        seconds = (end - start).total_seconds() if start and end and start <= end else None
        safe = seconds is not None and outcome in ("resumed", "stopped") and any(
            _verified(job_dir, item, verifier) for item in current)
        records.append({"interruption_id": identifier, "outcome": outcome, "verified_safe": safe,
                        "started_at": started_at, "ended_at": ended_at,
                        "occurred_at": e.get("occurred_at") if all(when is not None for _, when in dates) else None,
                        "elapsed_seconds": seconds,
                        "source": e.get("source"), "verification_ref": data.get("verification_ref"),
                        **_provenance(group)})
    return {"records": records, "observed": len(records), "verified_safe": sum(r["verified_safe"] for r in records),
            "resumed": sum(r["verified_safe"] and r["outcome"] == "resumed" for r in records),
            "stopped": sum(r["verified_safe"] and r["outcome"] == "stopped" for r in records),
            "unresolved": sum(r["outcome"] == "unresolved" for r in records),
            "unknown": sum(r["outcome"] not in ("resumed", "stopped", "unresolved") for r in records),
            "unverified_outcomes": sum(r["outcome"] in ("resumed", "stopped") and not r["verified_safe"]
                                       for r in records)}


def evaluate_guardrails(job_dir, job, events, required, candidate):
    """Report requirements from trusted routing and facts from load_events' as-of view.

    candidate carries candidate_sha, base_sha, spec_digest, round, and optionally
    verify_evidence(event, ref, bytes_or_none), incident_window, as_of and load diagnostics.
    The verifier must authenticate facts independently; event labels grant no trust.
    required is a sequence of IDs or {id, required, legacy_paths} descriptors.
    """
    candidate = candidate or {}
    job_dir = Path(job_dir)
    real = sorted((e for e in events if e.get("job_id") == job["id"] and e.get("origin") == "real"),
                  key=lambda e: e["event_id"])
    verifier = candidate.get("verify_evidence")
    diagnostics = list(candidate.get("diagnostics", ()))
    if candidate.get("unstable"):
        diagnostics.append("Candidate bindings changed during extraction.")
    rows = _requirements(job_dir, real, required, candidate)
    eligible = sum(r["required"] for r in rows)
    covered = sum(r["status"] in ("pass", "fail") for r in rows)
    records = _incident_records(job_dir, [e for e in real if e["kind"] == "incident"], verifier)
    windows = _coverage_windows(job_dir, real, verifier, candidate.get("as_of"))
    window = candidate.get("incident_window")
    incidents = {"records": records, "coverage_windows": windows, "window": window,
                 "confirmed_blocked": sum(r["verified"] and r["disposition"] == "blocked" for r in records),
                 "confirmed_effective_observed": sum(r["verified"] and r["disposition"] == "effective" for r in records),
                 "suspected": sum(r["status"] == "suspected" for r in records),
                 "effective_total": None if diagnostics else _effective_total(window, windows, records)}
    return {"candidate": {k: candidate.get(k) for k in _BINDINGS}, "requirements": rows,
            "coverage": {"eligible": eligible, "covered": covered, "proven": sum(r["status"] == "pass" for r in rows),
                         "jobs_eligible": int(eligible > 0), "jobs_covered": int(eligible > 0 and covered == eligible and not diagnostics)},
            "incidents": incidents, "diagnostics": diagnostics,
            "interruptions": _interruptions(job_dir, [e for e in real if e["kind"] == "interruption"], verifier)}
