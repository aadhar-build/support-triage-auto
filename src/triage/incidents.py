"""Incident grouping: turn a burst of outage threads for one brand into one incident.

Usage: python3 -m triage.incidents --split {dev,eval,full} --report-out PATH
       [--format {json,md}] [--config evals/drift.json] [--data data/raw/twcs.csv]
       [--split-config data/split.json] [--incidents-out PATH]

Threads are replayed in (first-turn time, root_tweet_id) order. A thread's label comes
from its first turn only; its brands come from @mentions in that first turn and from
brand replies, each counted only from the moment it was known. For each brand a
trailing window counts outage threads; a burst that reaches min_burst and beats the
brand's recent baseline opens an incident. Every number comes from the `incidents`
section of the config (load_incident_config); there are no defaults.

The report holds aggregates only. Incident ids, brands and top terms go only to
--incidents-out, which must resolve outside the repository and is refused for the
eval split.

Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from triage.cheap_tier import CheapTier
from triage.context_signals import contact_record
from triage.drift import DEFAULT_CONFIG, DEFAULT_DATA, RESIDUAL_LOOK_AHEAD
from triage.loader import HeaderError
from triage.pick_hard_cases import REPO_ROOT
from triage.split import DEFAULT_SPLIT_PATH, SplitConfigError, load_split_config, split_threads
from triage.threads import Thread, build_threads
from triage.tiers import LabellingTier

_UTC = timezone.utc


class IncidentConfigError(ValueError):
    """The incident config is missing or malformed. The message names the file."""


@dataclass(frozen=True)
class IncidentConfig:
    path: str
    version: Optional[str]
    window_hours: float
    min_burst: int
    baseline_factor: float
    baseline_lookback_hours: float
    label: str


OUTAGE_LABEL = "outage_technical"


def _positive_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def load_incident_config(path) -> IncidentConfig:
    """Read `version` and the top-level `incidents` object of a config shaped like
    evals/drift.json. Any problem raises IncidentConfigError naming the file; there is
    no default and no fallback value."""
    def fail(problem: str):
        return IncidentConfigError("incident config %s: %s" % (path, problem))

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise fail("file not found") from None
    except (OSError, UnicodeDecodeError) as e:
        raise fail("cannot read file: %s" % e) from None
    except json.JSONDecodeError as e:
        raise fail("not valid JSON: %s" % e) from None
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    version = data.get("version")
    version = version if isinstance(version, str) else None
    section = data.get("incidents")
    if not isinstance(section, dict):
        raise fail("'incidents' is missing or is not an object")

    for key in ("window_hours", "min_burst", "baseline_factor", "baseline_lookback_hours",
                "label"):
        if key not in section:
            raise fail("'incidents.%s' is missing" % key)
    for key in ("window_hours", "baseline_factor", "baseline_lookback_hours"):
        if not _positive_number(section[key]):
            raise fail("'incidents.%s' must be a finite number above 0, got %r"
                       % (key, section[key]))
    min_burst = section["min_burst"]
    if not isinstance(min_burst, int) or isinstance(min_burst, bool) or min_burst < 2:
        raise fail("'incidents.min_burst' must be an int of at least 2, got %r" % (min_burst,))
    if section["baseline_lookback_hours"] < section["window_hours"]:
        raise fail("'incidents.baseline_lookback_hours' %r is shorter than "
                   "'incidents.window_hours' %r"
                   % (section["baseline_lookback_hours"], section["window_hours"]))
    if section["label"] != OUTAGE_LABEL:
        raise fail("'incidents.label' must be %r, got %r" % (OUTAGE_LABEL, section["label"]))
    return IncidentConfig(path=str(path), version=version,
                          window_hours=section["window_hours"], min_burst=min_burst,
                          baseline_factor=section["baseline_factor"],
                          baseline_lookback_hours=section["baseline_lookback_hours"],
                          label=OUTAGE_LABEL)


# ---------------------------------------------------------------- detection

TOP_TERMS = 5
STOP_WORDS = frozenset((
    "the", "and", "for", "you", "your", "this", "that", "with", "are", "was", "not",
    "but", "have", "has", "just", "can", "any", "our", "all", "get", "got", "why", "how",
    "what", "when", "its", "it's", "i'm", "can't", "don't", "from", "there", "they",
    "out", "now", "been", "still", "will", "who", "too", "very", "please"))
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]+")
_TERM_RE = re.compile(r"[a-z0-9']+")


def _status_reply() -> Dict[str, object]:
    return {"state": "awaiting_reply", "needs_human_approval": True, "text": None}


@dataclass(frozen=True)
class Incident:
    id: str
    brand: str
    start: datetime
    opened_at: datetime
    end: datetime
    minutes_to_open: float
    member_ids: Tuple[int, ...]
    members: int
    reopens: int
    top_terms: Tuple[str, ...]
    status_reply: Dict[str, object] = field(default_factory=_status_reply)


@dataclass(frozen=True)
class IncidentResult:
    incidents: Tuple[Incident, ...]
    memberships: Dict[int, str]
    outage_threads: int
    unattributed_outage_threads: int
    warm_up_outage_threads: int
    warm_up_events_at_min_burst: int


def _first_turn_copy(thread: Thread) -> Thread:
    return Thread(root_tweet_id=thread.root_tweet_id, brand_ids=(),
                  turns=thread.turns[:1], brand_replied=False)


def top_terms(texts: Sequence[str], k: int = TOP_TERMS) -> Tuple[str, ...]:
    """Up to k tokens ranked by how many texts contain them, ties alphabetical. Whole
    @handles are removed before tokenising; short, all-digit and stop-word tokens are
    dropped."""
    counts: Dict[str, int] = {}
    for text in texts:
        tokens = set(_TERM_RE.findall(_HANDLE_RE.sub(" ", text).lower()))
        for tok in tokens:
            if len(tok) < 3 or tok.isdigit() or tok in STOP_WORDS:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    return tuple(sorted(counts, key=lambda t: (-counts[t], t))[:k])


class _Open:
    """A brand's incident while it is being built."""

    def __init__(self, brand: str, opened_at: datetime):
        self.brand = brand
        self.opened_at = opened_at
        self.members: List[Tuple[datetime, int]] = []
        self.reopens = 0
        self.is_open = True

    def end(self) -> datetime:
        return max(t for t, _ in self.members)


def find_incidents(threads: Sequence[Thread], labeller: LabellingTier,
                   config: IncidentConfig) -> IncidentResult:
    """Replay `threads` and group per-brand bursts of outage threads into incidents,
    deciding each entry event once with only what was known at it."""
    w = timedelta(hours=config.window_hours)
    lookback = timedelta(hours=config.baseline_lookback_hours)
    scale = config.window_hours / config.baseline_lookback_hours

    replayed = [t for t in threads if t.turns and t.turns[0].inbound]
    replayed.sort(key=lambda t: (t.turns[0].created_at.astimezone(_UTC), t.root_tweet_id))
    time_of = {t.root_tweet_id: t.turns[0].created_at.astimezone(_UTC) for t in replayed}
    if not replayed:
        return IncidentResult((), {}, 0, 0, 0, 0)
    history_start = time_of[replayed[0].root_tweet_id]

    brand_seen: Dict[str, datetime] = {}
    for t in threads:
        for turn in t.turns:
            if not turn.inbound:
                when = turn.created_at.astimezone(_UTC)
                if turn.author_id not in brand_seen or when < brand_seen[turn.author_id]:
                    brand_seen[turn.author_id] = when
    known = tuple(sorted(brand_seen))

    outage: List[Thread] = []
    for t in replayed:
        if labeller.label(_first_turn_copy(t)).intent == config.label:
            outage.append(t)

    events: List[Tuple[datetime, datetime, int, str]] = []
    unattributed = 0
    warm_up_threads = 0
    for y in outage:
        yt = time_of[y.root_tweet_id]
        if yt - w - lookback < history_start:
            warm_up_threads += 1
        record = contact_record(y, known)
        entries: Dict[str, datetime] = {}
        if record is not None:
            for b in record.brands_at_start:
                entries[b] = max(yt, brand_seen[b])
            for b, when in record.brand_replies:
                if b not in entries or when < entries[b]:
                    entries[b] = when
        if not any(e <= yt + w for e in entries.values()):
            unattributed += 1
        for b, e in entries.items():
            events.append((e, yt, y.root_tweet_id, b))
    events.sort()

    assigned: Dict[int, _Open] = {}
    entered: Dict[str, List[Tuple[datetime, int]]] = {}
    latest: Dict[str, _Open] = {}
    all_incidents: List[_Open] = []
    warm_up_events = 0

    def in_window(b: str, lo: datetime, hi: datetime) -> List[Tuple[datetime, int]]:
        rows = entered[b]
        i = bisect.bisect_right(rows, (lo, float("inf")))
        j = bisect.bisect_right(rows, (hi, float("inf")))
        return rows[i:j]

    for T, yt, root, b in events:
        if root in assigned:
            continue
        bisect.insort(entered.setdefault(b, []), (yt, root))
        recent = in_window(b, T - w, T)
        inc = latest.get(b)
        if inc is not None and inc.is_open:
            n = sum(1 for _, r in recent if r not in assigned or assigned[r] is inc)
            if n >= config.min_burst:
                if T - w < yt <= T:
                    inc.members.append((yt, root))
                    assigned[root] = inc
                continue
            inc.is_open = False
        free = [(t, r) for t, r in recent if r not in assigned]
        if T - w - lookback < history_start:
            if len(free) >= config.min_burst:
                warm_up_events += 1
            continue
        base = sum(1 for _, r in in_window(b, T - w - lookback, T - w) if r not in assigned)
        if len(free) >= config.min_burst and len(free) > config.baseline_factor * base * scale:
            if inc is not None and T - w < inc.end() <= T:
                inc.reopens += 1
                inc.is_open = True
            else:
                inc = _Open(b, T)
                latest[b] = inc
                all_incidents.append(inc)
            for row in free:
                inc.members.append(row)
                assigned[row[1]] = inc

    text_of = {t.root_tweet_id: t.turns[0].text for t in replayed}
    built = []
    for inc in all_incidents:
        rows = sorted(inc.members)
        start = rows[0][0]
        built.append((start, inc.brand, rows[0][1], inc, rows))
    built.sort(key=lambda x: x[:3])
    out: List[Incident] = []
    memberships: Dict[int, str] = {}
    for n, (start, brand, _, inc, rows) in enumerate(built, 1):
        iid = "inc-%04d" % n
        ids = tuple(r for _, r in rows)
        for r in ids:
            memberships[r] = iid
        out.append(Incident(
            id=iid, brand=brand, start=start, opened_at=inc.opened_at, end=rows[-1][0],
            minutes_to_open=(inc.opened_at - start).total_seconds() / 60.0,
            member_ids=ids, members=len(ids), reopens=inc.reopens,
            top_terms=top_terms([text_of[r] for r in ids])))
    return IncidentResult(incidents=tuple(out), memberships=memberships,
                          outage_threads=len(outage),
                          unattributed_outage_threads=unattributed,
                          warm_up_outage_threads=warm_up_threads,
                          warm_up_events_at_min_burst=warm_up_events)


# ---------------------------------------------------------------- report

def build_report(result: IncidentResult, config: IncidentConfig, split: str) -> dict:
    """Aggregates only: no text, thread ids, author ids, brand names or top terms."""
    sizes = sorted(i.members for i in result.incidents)
    return {
        "config_version": config.version, "split": split,
        "window_hours": config.window_hours, "min_burst": config.min_burst,
        "baseline_factor": config.baseline_factor,
        "baseline_lookback_hours": config.baseline_lookback_hours, "label": config.label,
        "look_ahead": {"labels": "first_turn_only", "brands": "known_at_event_time",
                       "baseline": "earlier_unassigned_only",
                       "residual": list(RESIDUAL_LOOK_AHEAD)},
        "incidents": len(result.incidents),
        "members_per_incident": sizes,
        "members_total": sum(sizes),
        "drafts_avoided": sum(n - 1 for n in sizes),
        "minutes_first_message_to_incident": sorted(i.minutes_to_open
                                                    for i in result.incidents),
        "reopens": sum(i.reopens for i in result.incidents),
        "incidents_awaiting_approval": sum(
            1 for i in result.incidents if i.status_reply["state"] == "awaiting_reply"),
        "outage_threads": result.outage_threads,
        "unattributed_outage_threads": result.unattributed_outage_threads,
        "warm_up": {"hours": config.window_hours + config.baseline_lookback_hours,
                    "outage_threads": result.warm_up_outage_threads,
                    "events_at_min_burst": result.warm_up_events_at_min_burst},
    }


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return "%.4g" % v
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


def report_markdown(report: dict) -> str:
    out = ["# Incident report", "", "| field | value |", "|---|---|"]
    for key, value in report.items():
        if isinstance(value, dict):
            for sub, v in value.items():
                out.append("| %s.%s | %s |" % (key, sub, _fmt(v)))
        else:
            out.append("| %s | %s |" % (key, _fmt(value)))
    return "\n".join(out) + "\n"


def incident_record(inc: Incident) -> dict:
    return {"id": inc.id, "brand": inc.brand, "start": inc.start.isoformat(),
            "opened_at": inc.opened_at.isoformat(), "end": inc.end.isoformat(),
            "minutes_to_open": inc.minutes_to_open, "member_ids": list(inc.member_ids),
            "members": inc.members, "reopens": inc.reopens, "top_terms": list(inc.top_terms),
            "status_reply": dict(inc.status_reply)}


# ---------------------------------------------------------------- command line

class _Fail(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.incidents",
                                description=__doc__.splitlines()[0])
    p.add_argument("--split", required=True, choices=("dev", "eval", "full"))
    p.add_argument("--report-out", required=True)
    p.add_argument("--format", default="json", choices=("json", "md"))
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--split-config", default=str(DEFAULT_SPLIT_PATH))
    p.add_argument("--incidents-out")
    return p


def _check_outside_repo(out) -> None:
    resolved = Path(out).resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise _Fail("--incidents-out %s is inside the repository %s; write incidents "
                    "outside it" % (resolved, REPO_ROOT))


def _write(path, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise _Fail("cannot write %s: %s" % (path, e.strerror or e))


def _load_threads(path) -> List[Thread]:
    try:
        threads, _ = build_threads(path)
    except FileNotFoundError:
        raise _Fail("data file not found: %s (hint: run scripts/download_data.sh)" % path)
    except OSError as e:
        raise _Fail("cannot read data file %s: %s" % (path, e.strerror or e))
    except HeaderError as e:
        raise _Fail("bad header in %s: %s" % (path, e))
    except UnicodeDecodeError as e:
        raise _Fail("%s is not valid UTF-8 (byte offset %d)" % (path, e.start))
    return threads


def _run(args) -> str:
    if args.incidents_out is not None:
        if args.split == "eval":
            raise _Fail("--incidents-out is refused for the eval split: eval threads never "
                        "feed labelling or tuning")
        _check_outside_repo(args.incidents_out)
    try:
        config = load_incident_config(args.config)
    except IncidentConfigError as e:
        raise _Fail(str(e))
    try:
        split_config = load_split_config(args.split_config)
    except SplitConfigError as e:
        raise _Fail("bad split config: %s" % e)

    threads = _load_threads(args.data)
    mapping, _ = split_threads(threads, split_config)
    chosen = [t for t in threads if mapping[t.root_tweet_id] == args.split]
    result = find_incidents(chosen, CheapTier(), config)

    report = build_report(result, config, args.split)
    text = (report_markdown(report) if args.format == "md"
            else json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.report_out, text)
    if args.incidents_out is not None:
        _write(args.incidents_out, "".join(
            json.dumps(incident_record(i), ensure_ascii=False, sort_keys=True) + "\n"
            for i in result.incidents))
    return "incidents=%d members=%d drafts_avoided=%d unattributed=%d split=%s" % (
        report["incidents"], report["members_total"], report["drafts_avoided"],
        report["unattributed_outage_threads"], args.split)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        line = _run(args)
    except _Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
