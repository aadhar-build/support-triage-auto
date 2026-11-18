"""Weekly drift monitor: compare each week's traffic with a frozen reference period.

Usage: python3 -m triage.drift --split {dev,eval,full} --report-out PATH [--format {json,md}]
       [--config evals/drift.json] [--data data/raw/twcs.csv] [--split-config data/split.json]
       [--samples-out PATH] [--seed 0]

Standard library only.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from triage.threads import Thread
from triage.tiers import INTENTS, LabellingTier

PSI_EPSILON = 1e-4
_UTC = timezone.utc
_WEEK = timedelta(days=7)


def psi(reference_shares: Mapping[str, float], window_shares: Mapping[str, float]) -> float:
    """Population stability index over the seven intents. Zero shares are floored at
    PSI_EPSILON; shares are not renormalised."""
    total = 0.0
    for intent in INTENTS:
        r = max(reference_shares[intent], PSI_EPSILON)
        w = max(window_shares[intent], PSI_EPSILON)
        total += (w - r) * math.log(w / r)
    return total


def cut_thread(thread: Thread, cutoff: datetime) -> Optional[Thread]:
    """Copy of `thread` keeping only turns with created_at < cutoff (exclusive), with
    brand_ids and brand_replied recomputed. None when no turn is kept. The input is
    never changed."""
    kept = tuple(t for t in thread.turns if t.created_at < cutoff)
    if not kept:
        return None
    brand_ids = tuple(sorted({t.author_id for t in kept if not t.inbound}))
    return replace(thread, turns=kept, brand_ids=brand_ids, brand_replied=len(brand_ids) > 0)


def _week_label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return "%04d-W%02d" % (year, week)


def _monday_of(dt: datetime) -> date:
    d = dt.astimezone(_UTC).date()
    return d - timedelta(days=d.weekday())


def _week_end(monday: date) -> datetime:
    return datetime(monday.year, monday.month, monday.day, tzinfo=_UTC) + _WEEK


@dataclass(frozen=True)
class WeekBucket:
    week: str
    week_end: datetime
    threads: Tuple[Thread, ...]
    partial: bool


def replay_weeks(threads: Sequence[Thread]) -> List[WeekBucket]:
    """Group threads by the ISO week (UTC) of their first turn, in (first-turn time,
    root_tweet_id) order. One bucket per week from the earliest to the latest, empty
    weeks included; only the first and last bucket are partial. Each bucket holds the
    threads cut at that week's end (next Monday 00:00 UTC)."""
    placed = [t for t in threads if t.turns]
    if not placed:
        return []
    placed.sort(key=lambda t: (t.turns[0].created_at.astimezone(_UTC), t.root_tweet_id))
    by_monday: Dict[date, List[Thread]] = {}
    for t in placed:
        by_monday.setdefault(_monday_of(t.turns[0].created_at), []).append(t)
    first, last = min(by_monday), max(by_monday)
    buckets: List[WeekBucket] = []
    monday = first
    while monday <= last:
        end = _week_end(monday)
        cut = tuple(c for c in (cut_thread(t, end) for t in by_monday.get(monday, ()))
                    if c is not None)
        buckets.append(WeekBucket(week=_week_label(monday), week_end=end, threads=cut,
                                  partial=monday in (first, last)))
        monday += _WEEK
    return buckets


@dataclass(frozen=True)
class WeekSignals:
    week: str
    partial: bool
    volume: int
    intent_counts: Dict[str, int]
    intent_shares: Dict[str, Optional[float]]
    confidence_sum: float
    mean_confidence: Optional[float]
    escalated_count: int
    escalated_share: Optional[float]
    other_count: int
    other_share: Optional[float]


def _share(count: int, total: int) -> Optional[float]:
    return count / total if total else None


def weekly_signals(buckets: Sequence[WeekBucket], labeller: LabellingTier) -> List[WeekSignals]:
    """Label each cut thread once and work out the five signals for every week."""
    out: List[WeekSignals] = []
    for b in buckets:
        labels = [labeller.label(t) for t in b.threads]
        n = len(labels)
        counts = {i: 0 for i in INTENTS}
        for lab in labels:
            counts[lab.intent] += 1
        conf = sum(lab.confidence for lab in labels)
        esc = sum(1 for lab in labels if lab.action == "escalate")
        out.append(WeekSignals(
            week=b.week, partial=b.partial, volume=n, intent_counts=counts,
            intent_shares={i: _share(counts[i], n) for i in INTENTS},
            confidence_sum=conf, mean_confidence=(conf / n if n else None),
            escalated_count=esc, escalated_share=_share(esc, n),
            other_count=counts["other"], other_share=_share(counts["other"], n)))
    return out


SIGNAL_ORDER: Tuple[str, ...] = ("intent_psi", "volume", "mean_confidence", "escalated_share",
                                 "other_share")


class DriftConfigError(ValueError):
    """The drift config file is missing or malformed."""


class DriftDataError(ValueError):
    """The replayed data cannot support a drift check (for example, an empty reference)."""


@dataclass(frozen=True)
class DriftConfig:
    path: str
    version: Optional[str]
    start_week: str
    end_week: str
    min_volume: int
    samples_per_callout: int
    thresholds: Dict[str, float]


@dataclass(frozen=True)
class Reference:
    start_week: str
    end_week: str
    weeks_used: Tuple[str, ...]
    weeks_missing: Tuple[str, ...]
    volume: float
    intent_shares: Dict[str, float]
    mean_confidence: float
    escalated_share: float
    other_share: float


@dataclass(frozen=True)
class WeekResult:
    week: str
    phase: str
    partial: bool
    checked: bool
    low_volume: bool
    signals: WeekSignals
    volume_ratio: Optional[float] = None
    intent_psi: Optional[float] = None
    mean_confidence_delta: Optional[float] = None
    escalated_share_delta: Optional[float] = None
    other_share_delta: Optional[float] = None


@dataclass(frozen=True)
class CallOut:
    week: str
    signal: str
    value: float
    reference_value: float
    threshold: float


@dataclass(frozen=True)
class DriftResult:
    reference: Reference
    weeks: Tuple[WeekResult, ...]
    callouts: Tuple[CallOut, ...]


_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})", re.ASCII)


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _parse_week(label) -> Optional[date]:
    """Monday of a 'YYYY-Www' ISO week, or None if the label is not a real ISO week."""
    if not isinstance(label, str):
        return None
    m = _WEEK_RE.fullmatch(label)
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None


def load_drift_config(path) -> DriftConfig:
    """Read and validate a drift config in the shape of evals/drift.json. Only `version`
    and `drift` are read. Any problem raises DriftConfigError naming the file; there is
    no default config and no fallback value."""
    def fail(problem: str):
        return DriftConfigError("drift config %s: %s" % (path, problem))

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
    section = data.get("drift")
    if not isinstance(section, dict):
        raise fail("'drift' is missing or is not an object")

    reference = section.get("reference")
    if not isinstance(reference, dict):
        raise fail("'drift.reference' is missing or is not an object")
    mondays = {}
    for key in ("start_week", "end_week"):
        if key not in reference:
            raise fail("'drift.reference.%s' is missing" % key)
        monday = _parse_week(reference[key])
        if monday is None:
            raise fail("'drift.reference.%s' must be a real ISO week 'YYYY-Www', got %r"
                       % (key, reference[key]))
        mondays[key] = monday
    if mondays["start_week"] > mondays["end_week"]:
        raise fail("start_week %s is after end_week %s"
                   % (reference["start_week"], reference["end_week"]))

    ints = {}
    for key, low in (("min_volume", 1), ("samples_per_callout", 0)):
        if key not in section:
            raise fail("'drift.%s' is missing" % key)
        v = section[key]
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            raise fail("'drift.%s' must be an int of at least %d, got %r" % (key, low, v))
        ints[key] = v

    raw = section.get("thresholds")
    if not isinstance(raw, dict):
        raise fail("'drift.thresholds' is missing or is not an object")
    thresholds: Dict[str, float] = {}
    for key in SIGNAL_ORDER:
        if key not in raw:
            raise fail("'drift.thresholds.%s' is missing" % key)
        v = raw[key]
        if not _is_number(v) or not math.isfinite(v) or v <= 0:
            raise fail("'drift.thresholds.%s' must be a finite number above 0, got %r"
                       % (key, v))
        if key == "volume" and v <= 1.0:
            raise fail("'drift.thresholds.volume' is a ratio and must be above 1.0, got %r"
                       % (v,))
        thresholds[key] = v

    return DriftConfig(path=str(path), version=version,
                       start_week=reference["start_week"], end_week=reference["end_week"],
                       min_volume=ints["min_volume"],
                       samples_per_callout=ints["samples_per_callout"], thresholds=thresholds)


def _week_monday(label: str) -> date:
    year, week = label.split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def _reference(signals: Sequence[WeekSignals], config: DriftConfig) -> Reference:
    by_week = {s.week: s for s in signals}
    used: List[WeekSignals] = []
    missing: List[str] = []
    monday, last = _week_monday(config.start_week), _week_monday(config.end_week)
    while monday <= last:
        label = _week_label(monday)
        s = by_week.get(label)
        if s is None or s.partial:
            missing.append(label)
        else:
            used.append(s)
        monday += _WEEK
    total = sum(s.volume for s in used)
    if not total:
        raise DriftDataError("reference range %s..%s holds no usable threads"
                             % (config.start_week, config.end_week))
    counts = {i: sum(s.intent_counts[i] for s in used) for i in INTENTS}
    return Reference(
        start_week=config.start_week, end_week=config.end_week,
        weeks_used=tuple(s.week for s in used), weeks_missing=tuple(missing),
        volume=total / len(used),
        intent_shares={i: counts[i] / total for i in INTENTS},
        mean_confidence=sum(s.confidence_sum for s in used) / total,
        escalated_share=sum(s.escalated_count for s in used) / total,
        other_share=counts["other"] / total)


def check_drift(signals: Sequence[WeekSignals], config: DriftConfig) -> DriftResult:
    """Compare every non-partial week after the reference range with the reference,
    which is computed once from the reference weeks only."""
    ref = _reference(signals, config)
    start, end = _week_monday(config.start_week), _week_monday(config.end_week)
    th = config.thresholds
    weeks: List[WeekResult] = []
    found: List[CallOut] = []
    for s in signals:
        monday = _week_monday(s.week)
        phase = ("before_reference" if monday < start
                 else "reference" if monday <= end else "monitored")
        checked = phase == "monitored" and not s.partial
        if not checked:
            weeks.append(WeekResult(week=s.week, phase=phase, partial=s.partial,
                                    checked=False, low_volume=False, signals=s))
            continue
        low = s.volume < config.min_volume
        ratio = s.volume / ref.volume
        values: Dict[str, Optional[float]] = {"volume": ratio, "intent_psi": None,
                                              "mean_confidence": None,
                                              "escalated_share": None, "other_share": None}
        if s.volume:
            values["intent_psi"] = psi(ref.intent_shares, s.intent_shares)
            values["mean_confidence"] = abs(s.mean_confidence - ref.mean_confidence)
            values["escalated_share"] = abs(s.escalated_share - ref.escalated_share)
            values["other_share"] = abs(s.other_share - ref.other_share)
        ref_values = {"intent_psi": 0.0, "volume": 1.0,
                      "mean_confidence": ref.mean_confidence,
                      "escalated_share": ref.escalated_share, "other_share": ref.other_share}
        for name in SIGNAL_ORDER:
            v = values[name]
            if v is None:
                continue
            if name == "volume":
                breach = v >= th["volume"] or v <= 1 / th["volume"]
            else:
                breach = not low and v > th[name]
            if breach:
                found.append(CallOut(week=s.week, signal=name, value=v,
                                     reference_value=ref_values[name], threshold=th[name]))
        weeks.append(WeekResult(
            week=s.week, phase=phase, partial=s.partial, checked=True, low_volume=low,
            signals=s, volume_ratio=ratio, intent_psi=values["intent_psi"],
            mean_confidence_delta=values["mean_confidence"],
            escalated_share_delta=values["escalated_share"],
            other_share_delta=values["other_share"]))
    return DriftResult(reference=ref, weeks=tuple(weeks), callouts=tuple(found))
