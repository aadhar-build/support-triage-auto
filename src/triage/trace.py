"""Audit trace: one JSON record per routed thread, written as JSON lines outside the repo.

A record holds the code version and threshold that produced a decision, each tier step
with its model id and labels, the final labels, the reason codes and the ids of any
always-escalate topics that forced the decision (`topic_id`, empty when none did). It holds the thread's
conversation id (root tweet id) but never tweet text, author ids or brand ids.

Known limits of the code version: it is read from git at run time. `code_dirty` ignores
untracked files (`--untracked-files=no`), so an untracked file the run reads is not
flagged, and `code_dirty=True` says the tracked tree had edits but cannot reproduce them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

from triage.router import ESCALATE_ALWAYS, RoutedThread, is_known_reason_code

TRACE_SCHEMA = 2
REPO_ROOT = Path(__file__).resolve().parents[2]

_GIT_TIMEOUT_SECONDS = 10
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RECORD_KEYS = ("trace_schema", "thread_id", "code_version", "code_dirty", "rules_version",
                "drift_config_version", "threshold", "split", "seed", "tiers", "final",
                "reason_codes", "topic_id")
_NULLABLE_KEYS = ("rules_version", "drift_config_version")
_LABEL_KEYS = ("intent", "urgency", "action", "confidence")
_TIER_KEYS = ("tier", "model_id") + _LABEL_KEYS
_TOPIC_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


class TraceError(ValueError):
    """A trace could not be built, validated or written. The message is one line."""


@dataclass(frozen=True)
class RunInfo:
    code_version: str
    code_dirty: bool
    rules_version: Optional[str]
    drift_config_version: Optional[str]
    threshold: float
    split: str
    seed: int


def _git(repo_dir, *args) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True,
                              text=True, timeout=_GIT_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise TraceError("git is not installed; cannot read the code version")
    except subprocess.TimeoutExpired:
        raise TraceError("git %s timed out in %s" % (args[0], repo_dir))
    except OSError as e:
        raise TraceError("cannot run git in %s: %s" % (repo_dir, e.strerror or e))
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["exit %d" % proc.returncode])[0]
        raise TraceError("git %s failed in %s: %s" % (args[0], repo_dir, first))
    return proc.stdout


def code_version(repo_dir) -> Tuple[str, bool]:
    """Return (commit, dirty) for repo_dir. Raises TraceError if git can't say."""
    commit = _git(repo_dir, "rev-parse", "HEAD").strip()
    if not _COMMIT_RE.match(commit):
        raise TraceError("git rev-parse HEAD in %s did not return a 40-hex commit" % repo_dir)
    status = _git(repo_dir, "status", "--porcelain", "--untracked-files=no")
    return commit, bool(status.strip())


def _labels(labels) -> dict:
    return {"intent": labels.intent, "urgency": labels.urgency, "action": labels.action,
            "confidence": labels.confidence}


def build_record(routed: RoutedThread, run: RunInfo) -> dict:
    """One audit record for a routed thread. Ids and labels only, never text."""
    return {
        "trace_schema": TRACE_SCHEMA,
        "thread_id": routed.thread_id,
        "code_version": run.code_version,
        "code_dirty": run.code_dirty,
        "rules_version": run.rules_version,
        "drift_config_version": run.drift_config_version,
        "threshold": run.threshold,
        "split": run.split,
        "seed": run.seed,
        "tiers": [dict(tier=s.tier, model_id=s.model_id, **_labels(s.labels))
                  for s in routed.steps],
        "final": _labels(routed.labels),
        "reason_codes": list(routed.reason_codes),
        "topic_id": list(routed.topic_id),
    }


def validate_record(record) -> None:
    """Raise TraceError naming the thread id and the key when the record is incomplete."""
    if not isinstance(record, dict):
        raise TraceError("trace record is not an object")
    where = "trace record for thread %s" % (record.get("thread_id"),)

    def fail(key, problem):
        raise TraceError("%s: %r %s" % (where, key, problem))

    for key in _RECORD_KEYS:
        if key not in record:
            fail(key, "is missing")
        if record[key] is None and key not in _NULLABLE_KEYS:
            fail(key, "is null")
    if record["trace_schema"] != TRACE_SCHEMA:
        fail("trace_schema", "is not %d" % TRACE_SCHEMA)
    tiers = record["tiers"]
    if not isinstance(tiers, list) or not tiers:
        fail("tiers", "is not a non-empty list")
    for i, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            fail("tiers", "entry %d is not an object" % i)
        for key in _TIER_KEYS:
            if tier.get(key) is None:
                fail(key, "is missing or null in tier entry %d" % i)
    final = record["final"]
    if not isinstance(final, dict):
        fail("final", "is not an object")
    for key in _LABEL_KEYS:
        if final.get(key) is None:
            fail(key, "is missing or null in 'final'")
    codes = record["reason_codes"]
    if not isinstance(codes, list) or not codes:
        fail("reason_codes", "is not a non-empty list")
    for code in codes:
        if not is_known_reason_code(code):
            fail("reason_codes", "holds an unregistered code %r" % (code,))
    topic_id = record["topic_id"]
    if not isinstance(topic_id, list) or not all(
            isinstance(t, str) and _TOPIC_ID_RE.fullmatch(t) for t in topic_id):
        fail("topic_id", "is not a list of kebab-case topic ids")
    if ESCALATE_ALWAYS in codes and not topic_id:
        fail("topic_id", "is empty but reason_codes holds %r" % ESCALATE_ALWAYS)
    if topic_id and ESCALATE_ALWAYS not in codes:
        fail("topic_id", "is not empty but reason_codes lacks %r" % ESCALATE_ALWAYS)


def _check_out_path(path) -> Path:
    resolved = Path(path).resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise TraceError("trace path %s is inside the repository %s; write it outside"
                         % (resolved, REPO_ROOT))
    return resolved


def check_trace_path(path) -> None:
    """Raise TraceError if path is inside the repository. Opens nothing."""
    _check_out_path(path)


def write_trace(path, records: Iterable[dict]) -> int:
    """Validate and write records as JSON lines, atomically. Returns the record count.

    The destination is replaced only after every record is written and fsynced; on any
    error the temp file is removed and the destination is left as it was."""
    dest = _check_out_path(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".%s." % dest.name, suffix=".tmp", dir=str(dest.parent))
    except OSError as e:
        raise TraceError("cannot write trace in %s: %s" % (dest.parent, e.strerror or e))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for record in records:
                validate_record(record)
                f.write(json.dumps(record, sort_keys=True) + "\n")
                count += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(dest))
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, TraceError):
            raise
        if isinstance(e, OSError):
            raise TraceError("cannot write trace %s: %s" % (dest, e.strerror or e))
        if isinstance(e, Exception):
            raise TraceError("cannot write trace %s: %s" % (dest, e))
        raise
    return count
