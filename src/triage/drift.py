"""Weekly drift monitor: compare each week's traffic with a frozen reference period.

Usage: python3 -m triage.drift --split {dev,eval,full} --report-out PATH [--format {json,md}]
       [--config evals/drift.json] [--data data/raw/twcs.csv] [--split-config data/split.json]
       [--samples-out PATH] [--seed 0]

Standard library only.
"""

from __future__ import annotations

import math
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
