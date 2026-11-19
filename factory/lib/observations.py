"""Immutable v1 measurement records, independent of factory job operations.

Records are supplied by known producers. The store validates their structure
and provenance fields. Consumers independently verify evidence against its
source before granting technical coverage.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

MAX_EVENT_BYTES = 64 * 1024
# Bound optional advice to 4 MiB of ledger records while a controller operation is active.
_TRANSITION_ENTRY_LIMIT = 64
OBSERVATION_DIRECTORY = "observations"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
# Controller job IDs are ASCII directory names and may fill a 255-byte filename.
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")
_HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:[/%#A-Za-z0-9_.~:+@&=,-]*)")
_ANCHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEXDIGEST = re.compile(r"[0-9a-fA-F]{64}")
_GIT_OBJECT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_DECIMAL = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,18})?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_REQUIREMENT_VERDICTS = frozenset({
    "pass", "fail", "pending", "missing", "stale", "unverified", "not_required",
})
_SOURCE_KINDS = frozenset({"owner", "controller", "reviewer", "harness", "billing", "fixture", "legacy"})


class ObservationError(ValueError):
    """A refused observation or an invalid record in the store."""


def _pattern(value, label: str, regex: re.Pattern) -> str:
    if not isinstance(value, str) or regex.fullmatch(value) is None:
        raise ObservationError(f"invalid {label}")
    return value


def _identifier(value, label: str) -> str:
    return _pattern(value, label, _IDENTIFIER)


def _job_identifier(value, label: str) -> str:
    return _pattern(value, label, _JOB_ID)


def _uuid(value, label: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ObservationError(f"invalid {label}: expected a UUID")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ObservationError(f"invalid {label}: expected a UUID") from None
    if canonical != value.lower():
        raise ObservationError(f"invalid {label}: expected a hyphenated UUID")
    return canonical


def _parse_time(value: str, label: str) -> datetime:
    _pattern(value, label, _UTC_TIME)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ObservationError(f"invalid {label}: expected an ISO 8601 UTC timestamp") from None


def _format_time(value: datetime) -> str:
    return value.isoformat().removesuffix("+00:00") + "Z"


def _timestamp(value, label: str) -> str:
    return _format_time(_parse_time(value, label))


def _number(value, label: str) -> int | float:
    if type(value) not in (int, float) or value < 0 or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        raise ObservationError(f"invalid {label}: expected a finite nonnegative number")
    return value


def _positive_integer(value, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ObservationError(f"invalid {label}: expected a positive integer")
    return value


def _boolean(value, label: str) -> bool:
    if type(value) is not bool:
        raise ObservationError(f"invalid {label}: expected a boolean")
    return value


def _sha(value, label: str) -> str:
    return _pattern(value, label, _GIT_OBJECT).lower()


def _digest(value, label: str) -> str:
    return _pattern(value, label, _HEXDIGEST).lower()


def _url(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 1024 or not _HTTPS.fullmatch(value):
        raise ObservationError(f"invalid {label}: expected a stable HTTPS record link")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate the port, if present.
    except ValueError:
        raise ObservationError(f"invalid {label}: expected a stable HTTPS record link") from None
    if not parsed.hostname or parsed.username is not None:
        raise ObservationError(f"invalid {label}: expected a stable HTTPS record link")
    if parsed.fragment and not _ANCHOR.fullmatch(parsed.fragment):
        raise ObservationError(f"invalid {label}: expected a simple record anchor")
    return value


def _issue(value, label: str) -> int | str:
    if isinstance(value, str) and value.startswith("https://"):
        return _url(value, label)
    if isinstance(value, str):
        return _pattern(value, label, re.compile(r"[\w.-]+/[\w.-]+#[1-9][0-9]*", re.ASCII))
    return _positive_integer(value, label)


def _artifact_path(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        raise ObservationError(f"invalid {label}: expected a safe job-relative path")
    if any(part in {".", ".."} or not _PATH_COMPONENT.fullmatch(part) for part in value.split("/")):
        raise ObservationError(f"invalid {label}: expected a safe job-relative path")
    return value


def _object(value, schema: dict, label: str, *, required=(), fill=True) -> dict:
    if not isinstance(value, dict) or value.keys() - schema.keys():
        raise ObservationError(f"invalid {label}: expected an object containing only supported fields")
    for key in required:
        if value.get(key) is None:
            raise ObservationError(f"missing {label}.{key}")
    normalized = {}
    for key, check in schema.items():
        if not fill and key not in value:
            continue
        item = value.get(key)
        field = f"{label}.{key}"
        if item is None:
            normalized[key] = None
        elif isinstance(check, frozenset):
            if not isinstance(item, str) or item not in check:
                raise ObservationError(f"invalid {field}")
            normalized[key] = item
        else:
            normalized[key] = check(item, field)
    return normalized


_BINDING = {
    "candidate_sha": _sha, "base_sha": _sha, "spec_digest": _digest, "round": _positive_integer,
}


def _evidence_ref(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ObservationError(f"invalid {label}: expected an evidence reference")
    if "path" in value:
        schema = {"path": _artifact_path, "sha256": _digest, **_BINDING}
        return _object(value, schema, label, required=("path", "sha256"), fill=False)
    schema = {"url": _url, "verified": _boolean, **_BINDING}
    return _object(value, schema, label, required=("url",), fill=False)


def _evidence(value, label: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 32:
        raise ObservationError(f"invalid {label}: expected a bounded evidence list")
    return [_evidence_ref(item, label) for item in value]


def _evidence_or_event(value, label: str) -> dict | str:
    return _evidence_ref(value, label) if isinstance(value, dict) else _uuid(value, label)


def _event_ids(value, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise ObservationError(f"invalid {label}: expected a bounded observation ID list")
    ids = [_uuid(item, label) for item in value]
    if len(set(ids)) != len(ids):
        raise ObservationError(f"duplicate observation IDs in {label}")
    return ids


def _source(value, label: str) -> dict:
    fields = {
        "kind": _SOURCE_KINDS, "record_id": _identifier, "actor": _identifier,
        "provider": _identifier, "model": _identifier,
        "subscription_verified": _boolean,
    }
    return _object(value, fields, label, required=("kind", "record_id"), fill=False)


def _amount(value, label: str) -> str | int | float:
    return _pattern(value, label, _DECIMAL) if isinstance(value, str) else _number(value, label)


_COST_TYPES = frozenset({
    "subscription_usage", "subscription_charge", "incremental_charge", "api_equivalent_estimate",
})
_WINDOW = {"window_start": _timestamp, "window_end": _timestamp}
_COVERAGE = {"scope": _identifier, **_WINDOW, "complete": _boolean}
_RECOVERY = {
    "impact": _identifier, "proposed_recovery": _identifier, "outcome": _identifier,
    "cause": _identifier, "correction_event_id": _uuid, "regression_check_event_id": _uuid,
    "no_check_reason": _identifier, "owner_effort_event_ids": _event_ids,
    "interruption_event_ids": _event_ids,
}
_DATA_FIELDS = {
    "task_context": {
        "task_type": _identifier, "task_size": _identifier, "factory_version": _identifier,
        "workflow_mode": frozenset({"factory", "direct"}), "matched_task_key": _identifier,
        "provider": _identifier, "model": _identifier, "issue_ref": _issue,
        "started_at": _timestamp, "submitted_at": _timestamp, "decided_at": _timestamp,
        "ended_at": _timestamp, "status": _identifier,
    },
    "owner_effort": {
        "activity": frozenset({"preparation", "clarification", "supervision", "review", "recovery", "correction"}),
        "minutes": _number, "rework": _boolean,
    },
    "owner_effort_coverage": _COVERAGE,
    "owner_decision": {
        "disposition": frozenset({"accepted", "rejected", "abandoned"}), "material_follow_up": _boolean,
    },
    "escalation": {
        "request_id": _identifier, "rating": frozenset({"necessary", "unnecessary", "missed"}),
        "status": _identifier,
    },
    "usage_cost": {
        "cost_type": _COST_TYPES, "amount": _amount, "unit": _identifier,
        "currency": lambda value, label: _pattern(value, label, _CURRENCY),
        **_WINDOW, "billing_item": _identifier,
    },
    "criterion": {"criterion_id": _identifier, "verdict": _REQUIREMENT_VERDICTS},
    "demo": {"demo_id": _identifier, "criterion_id": _identifier, "verdict": _REQUIREMENT_VERDICTS},
    "limitation": {"code": _identifier},
    "check_evidence": {
        "check_id": _identifier, "verdict": _REQUIREMENT_VERDICTS, "base_sha": _sha, "spec_digest": _digest,
    },
    "incident": {
        "incident_id": _identifier, "category": _identifier,
        "status": frozenset({"suspected", "confirmed"}),
        "disposition": frozenset({"blocked", "effective", "unknown"}),
        "scope": _identifier, "resolution_ref": _evidence_or_event,
    },
    "interruption": {
        "interruption_id": _identifier, "started_at": _timestamp, "ended_at": _timestamp,
        "outcome": frozenset({"resumed", "stopped", "unresolved"}), "verification_ref": _evidence_or_event,
    },
    "observation_coverage": _COVERAGE,
    "defect": {"defect_id": _identifier, "origin": _identifier, "discovered_at": _timestamp, **_RECOVERY},
    "recovery_decision": {"failure_id": _identifier, "decision": _identifier, **_RECOVERY},
}
_DATA_REQUIRED = {
    "owner_effort": ("activity",),
    "owner_effort_coverage": ("scope",),
    "escalation": ("request_id",),
    "usage_cost": ("cost_type",),
    "criterion": ("criterion_id",),
    "demo": ("demo_id",),
    "limitation": ("code",),
    "check_evidence": ("check_id",),
    "incident": ("incident_id",),
    "interruption": ("interruption_id",),
    "observation_coverage": ("scope",),
    "defect": ("defect_id",),
    "recovery_decision": ("failure_id",),
}


def _validate_data(kind: str, data: dict, event: dict) -> None:
    start, end = data.get("window_start"), data.get("window_end")
    if start and end and _parse_time(end, "window_end") < _parse_time(start, "window_start"):
        raise ObservationError("observation window ends before it starts")
    if kind == "usage_cost" and data["amount"] is not None:
        if data["billing_item"] is None and (start is None or end is None):
            raise ObservationError("usage_cost amount requires a billing item or source window")
        if data["cost_type"] != "subscription_usage" or data["currency"] is not None:
            if data["currency"] is None or not isinstance(data["amount"], str):
                raise ObservationError("monetary usage_cost requires a decimal string amount and currency")
        elif data["unit"] is None:
            raise ObservationError("subscription_usage requires a unit")
    if kind == "check_evidence":
        if event["candidate_sha"] is None or event["round"] is None:
            raise ObservationError("check_evidence requires a full candidate SHA and round")
        for key in ("base_sha", "spec_digest"):
            if data[key] and event[key] and data[key] != event[key]:
                raise ObservationError(f"conflicting check_evidence {key}")
    if kind == "interruption" and data["started_at"] and data["ended_at"]:
        if _parse_time(data["ended_at"], "ended_at") < _parse_time(data["started_at"], "started_at"):
            raise ObservationError("interruption ends before it starts")
    if data.get("cause") and not event["evidence"]:
        raise ObservationError("a recorded cause requires an evidence reference")


def _normalize(event, job_id: str, *, stored: bool) -> dict:
    if not isinstance(event, dict):
        raise ObservationError("invalid observation: expected a JSON object")
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise ObservationError("unsupported schema version")
    fields = {
        "schema_version": _positive_integer, "event_id": _uuid, "kind": frozenset(_DATA_FIELDS),
        "job_id": _job_identifier, "occurred_at": _timestamp, "recorded_at": _timestamp,
        "origin": frozenset({"real", "controlled"}), "source": _source, **_BINDING,
        "evidence": _evidence, "supersedes_event_id": _uuid, "data": lambda value, label: value,
    }
    required = ("event_id", "kind", "job_id", "origin", "source", "data")
    if stored:
        required += ("recorded_at",)
    normalized = _object(event, fields, "observation", required=required)
    if normalized["job_id"] != job_id:
        raise ObservationError("observation job_id does not match this job")
    if normalized["source"]["kind"] == "fixture" and normalized["origin"] != "controlled":
        raise ObservationError("fixture observations require controlled origin")
    if not stored:
        # Recording time belongs to ingestion, even when the caller retries a saved record.
        normalized["recorded_at"] = None
    normalized["evidence"] = normalized["evidence"] or []
    kind = normalized["kind"]
    normalized["data"] = _object(
        normalized["data"], _DATA_FIELDS[kind], "data", required=_DATA_REQUIRED.get(kind, ()),
    )
    _validate_data(kind, normalized["data"], normalized)
    return normalized


@contextmanager
def _open_directory(path: Path | str, parent_fd: int | None = None):
    fd = os.open(path, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_job(job_dir: Path):
    path = job_dir.absolute()
    _job_identifier(path.name, "job directory name")
    if ".." in path.parts:
        raise ObservationError("invalid job directory path")
    # Anchor by descriptor across the job directory and its containing factory
    # directories. The caller supplies the checkout root (or a fixture root).
    parts = [path.name]
    anchor = path.parent
    if anchor.name:
        parts.append(anchor.name)
        anchor = anchor.parent
    if parts[-1] == "jobs" and anchor.name == "factory":
        parts.append(anchor.name)
        anchor = anchor.parent
    with ExitStack() as stack:
        fd = stack.enter_context(_open_directory(anchor))
        for part in reversed(parts):
            fd = stack.enter_context(_open_directory(part, fd))
        yield fd


def _check_local_reference(job_fd: int, path: str) -> None:
    """Reject existing links and non-regular artifacts. Missing evidence stays reportable."""
    parts = path.split("/")
    try:
        with ExitStack() as stack:
            parent = job_fd
            for part in parts[:-1]:
                parent = stack.enter_context(_open_directory(part, parent))
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ObservationError("unsafe local evidence artifact")
    except FileNotFoundError:
        pass
    except OSError:
        raise ObservationError("unsafe local evidence artifact") from None


def _json_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObservationError("duplicate JSON fields in observation")
        result[key] = value
    return result


def _nonfinite_json_number(_literal: str) -> None:
    raise ObservationError("nonfinite number in observation JSON")


def decode_event_json(raw: bytes) -> dict:
    """Decode a bounded UTF-8 JSON object with unambiguous field names."""
    if len(raw) > MAX_EVENT_BYTES:
        raise ObservationError("observation exceeds the maximum record size")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_nonfinite_json_number,
        )
    except ObservationError:
        raise
    except (ValueError, RecursionError):
        raise ObservationError("invalid observation JSON") from None
    if not isinstance(payload, dict):
        raise ObservationError("invalid observation JSON: expected an object")
    return payload


def _read_record(directory_fd: int, filename: str, job_id: str) -> dict:
    fd = os.open(filename, _READ_FLAGS, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_EVENT_BYTES:
            raise ObservationError("observation must be a bounded regular file")
        raw = handle.read(MAX_EVENT_BYTES + 1)
    record = _normalize(decode_event_json(raw), job_id, stored=True)
    if filename != f"{record['event_id']}.json":
        raise ObservationError("observation filename does not match its event ID")
    return record


def _read_records(directory_fd: int, job_id: str, *, for_transition: bool = False) -> tuple[list[dict], list[str]]:
    if for_transition:
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in islice(entries, _TRANSITION_ENTRY_LIMIT + 1)]
        if len(names) > _TRANSITION_ENTRY_LIMIT:
            return [], ["observation directory exceeds the transition read limit"]
    else:
        names = os.listdir(directory_fd)
    records, diagnostics = [], []
    for filename in sorted(names):
        if not filename.endswith(".json"):
            continue
        try:
            _uuid(filename[:-5], "observation filename")
        except ObservationError:
            diagnostics.append("invalid observation filename")
            continue
        try:
            records.append(_read_record(directory_fd, filename, job_id))
        except OSError:
            diagnostics.append(f"{filename}: unreadable observation file")
        except ValueError as exc:
            diagnostics.append(f"{filename}: {exc}")
    return records, diagnostics


def _correction_target(record: dict, current: dict, seen: set[str]) -> dict | None:
    target_id = record["supersedes_event_id"]
    if target_id is None:
        return None
    if target_id == record["event_id"]:
        raise ObservationError("an observation cannot supersede itself")
    if target_id in seen and target_id not in current:
        raise ObservationError("correction target has already been superseded")
    if target_id not in current:
        raise ObservationError("correction target is not a current observation in this job")
    target = current[target_id]
    if target["kind"] != record["kind"]:
        raise ObservationError("correction target must have the same kind")
    if target["origin"] != record["origin"]:
        raise ObservationError("correction target must have the same origin")
    return target


def _project(
    records: list[dict], cutoff: datetime | None, *, include_ancestry: bool = False,
) -> tuple[dict, set[str], list[str]]:
    ordered = sorted(
        ((_parse_time(record["recorded_at"], "recorded_at"), record["event_id"], record) for record in records),
        key=lambda item: (item[0], item[1]),
    )
    current, seen, diagnostics, previous = {}, set(), [], {}
    for recorded, event_id, record in ordered:
        if cutoff is not None and recorded > cutoff:
            break
        try:
            target = _correction_target(record, current, seen)
            if target is not None:
                target_id = target["event_id"]
                del current[target_id]
                if include_ancestry:
                    previous[event_id] = target_id
        except ObservationError as exc:
            diagnostics.append(f"{event_id}.json: {exc}")
            continue
        current[event_id] = record
        seen.add(event_id)
    if include_ancestry:
        for event_id, record in current.items():
            ancestry = []
            ancestor = previous.get(event_id)
            while ancestor is not None:
                ancestry.append(ancestor)
                ancestor = previous.get(ancestor)
            if ancestry:
                current[event_id] = {**record, "superseded_event_ids": ancestry[::-1]}
    return current, seen, diagnostics


def _check_directory_entry(job_fd: int, directory_fd: int) -> None:
    entry = os.stat(OBSERVATION_DIRECTORY, dir_fd=job_fd, follow_symlinks=False)
    opened = os.fstat(directory_fd)
    if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
        raise ObservationError("observation directory changed during access")


def _publish(directory_fd: int, record: dict, filename: str) -> None:
    try:
        raw = (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, OverflowError):
        raise ObservationError("observation cannot be serialized") from None
    if len(raw) > MAX_EVENT_BYTES:
        raise ObservationError("observation exceeds the maximum record size")
    stage = f".{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(stage, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Callers hold the directory flock from the ID lookup through publication.
        os.replace(stage, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(stage, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def append_event(job_dir: Path, event: dict) -> dict:
    """Record an event once and return the persisted event, including ingestion time."""
    job_id = job_dir.name
    incoming = _normalize(event, job_id, stored=False)
    filename = f"{incoming['event_id']}.json"
    try:
        with _open_job(job_dir) as job_fd:
            try:
                os.mkdir(OBSERVATION_DIRECTORY, mode=0o700, dir_fd=job_fd)
            except FileExistsError:
                pass
            with _open_directory(OBSERVATION_DIRECTORY, job_fd) as directory_fd:
                fcntl.flock(directory_fd, fcntl.LOCK_EX)
                _check_directory_entry(job_fd, directory_fd)
                try:
                    saved = _read_record(directory_fd, filename, job_id)
                except FileNotFoundError:
                    saved = None
                if saved is not None:
                    if {**saved, "recorded_at": None} != incoming:
                        raise ObservationError("conflicting reuse of observation event ID")
                    # A previous call may have published and then failed to sync.
                    os.fsync(directory_fd)
                    os.fsync(job_fd)
                    return saved
                if incoming["supersedes_event_id"] is None:
                    target = None
                else:
                    records, read_diagnostics = _read_records(directory_fd, job_id)
                    current, seen, projection_diagnostics = _project(records, None)
                    if read_diagnostics or projection_diagnostics:
                        raise ObservationError("cannot correct observation with incomplete history")
                    target = _correction_target(incoming, current, seen)
                refs = [
                    *incoming["evidence"],
                    incoming["data"].get("resolution_ref"), incoming["data"].get("verification_ref"),
                ]
                for ref in refs:
                    if isinstance(ref, dict) and "path" in ref:
                        _check_local_reference(job_fd, ref["path"])
                recorded = datetime.now(timezone.utc)
                if target is not None:
                    target_time = _parse_time(target["recorded_at"], "recorded_at")
                    if recorded <= target_time:
                        recorded = target_time + timedelta(microseconds=1)
                incoming["recorded_at"] = _format_time(recorded)
                _check_directory_entry(job_fd, directory_fd)
                _publish(directory_fd, incoming, filename)
                os.fsync(directory_fd)
                os.fsync(job_fd)
    except (OSError, OverflowError) as exc:
        message = os.strerror(exc.errno) if isinstance(exc, OSError) and exc.errno else "storage error"
        raise ObservationError(f"cannot record observation: {message}") from exc
    return incoming


def load_events(
    job_dir: Path, *, as_of: datetime | None = None, include_ancestry: bool = False,
    for_transition: bool = False,
) -> tuple[list[dict], list[str]]:
    """Read current observations. Transition advice skips busy or oversized ledgers."""
    if as_of is not None and (
        not isinstance(as_of, datetime) or as_of.utcoffset() is None
    ):
        raise ObservationError("as_of must be a timezone-aware datetime")
    try:
        with _open_job(job_dir) as job_fd:
            with ExitStack() as stack:
                try:
                    directory_fd = stack.enter_context(_open_directory(OBSERVATION_DIRECTORY, job_fd))
                except FileNotFoundError:
                    return [], []
                lock_mode = fcntl.LOCK_SH | fcntl.LOCK_NB if for_transition else fcntl.LOCK_SH
                fcntl.flock(directory_fd, lock_mode)
                _check_directory_entry(job_fd, directory_fd)
                records, diagnostics = _read_records(directory_fd, job_dir.name, for_transition=for_transition)
                _check_directory_entry(job_fd, directory_fd)
    except (OSError, ObservationError):
        return [], ["cannot read observation directory"]
    current, _, correction_diagnostics = _project(records, as_of, include_ancestry=include_ancestry)
    return list(current.values()), diagnostics + correction_diagnostics
