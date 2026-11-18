"""Context signals: how often a customer has already contacted a brand recently.

A ContactRecord holds what was known about one thread when it started: the customer (the
author of its inbound first turn), the start time, the brands the first turn @mentions, and
the brands' reply times. contact_counts gives, for each thread, how many conversations the
same customer started with the same brand within a window, up to and including this one,
using only what was known at this thread's start. Customer ids stay in memory; only counts
leave this module.

load_repeat_contact_rule reads the owner's `repeat_contact` block from the rules file and
fails closed: there are no defaults.

Standard library only. No I/O except reading the rules file in load_repeat_contact_rule;
repeat_contact_from_data validates an already-parsed object, and brand_attribution says
which brand a thread belongs to at its decision moment.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]+)")


@dataclass(frozen=True)
class ContactRecord:
    root_tweet_id: int
    customer: str
    start: datetime
    brands_at_start: FrozenSet[str]
    brand_replies: Tuple[Tuple[str, datetime], ...]


def contact_record(thread, known_brands) -> Optional[ContactRecord]:
    """The record for one real thread, or None when it has no turns, its first turn is
    outbound, or it has neither a known-brand mention nor a known-brand reply."""
    if not thread.turns:
        return None
    first = thread.turns[0]
    if not first.inbound:
        return None
    by_lower = {b.lower(): b for b in known_brands}
    mentioned = frozenset(by_lower[h.lower()] for h in _MENTION_RE.findall(first.text)
                          if h.lower() in by_lower)
    known = set(by_lower.values())
    replies = tuple((t.author_id, t.created_at.astimezone(timezone.utc))
                    for t in thread.turns if not t.inbound and t.author_id in known)
    if not mentioned and not replies:
        return None
    return ContactRecord(root_tweet_id=thread.root_tweet_id, customer=first.author_id,
                         start=first.created_at.astimezone(timezone.utc),
                         brands_at_start=mentioned, brand_replies=replies)


def _key(r: ContactRecord) -> Tuple[datetime, int]:
    return (r.start, r.root_tweet_id)


def _counts_for(x: ContactRecord, earlier: List[ContactRecord]) -> int:
    best = 0
    for b in x.brands_at_start:
        n = 0
        for y in earlier:
            if y is x or b in y.brands_at_start \
                    or any(brand == b and at <= x.start for brand, at in y.brand_replies):
                n += 1
        best = max(best, n)
    return best


def contact_counts(records: Iterable[ContactRecord], window: timedelta) -> Dict[int, int]:
    """For each record's root id, the largest count over its brands at start of conversations
    by the same customer with that brand, started within `window` up to and including it."""
    by_customer: Dict[str, List[ContactRecord]] = {}
    for r in sorted(records, key=lambda r: (r.customer, _key(r))):
        by_customer.setdefault(r.customer, []).append(r)
    result: Dict[int, int] = {}
    for group in by_customer.values():
        lo = 0
        for i, x in enumerate(group):
            while group[lo].start < x.start - window:
                lo += 1
            result[x.root_tweet_id] = _counts_for(x, group[lo:i + 1])
    return result


@dataclass(frozen=True)
class RepeatContactRule:
    window_days: float
    min_conversations: int
    effect: str
    report_window_hours: float


class RepeatContactError(ValueError):
    """The rules file's repeat_contact block is missing or invalid. The message names the path."""


def _positive_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) and value > 0


def load_repeat_contact_rule(path) -> RepeatContactRule:
    """Read the top-level `repeat_contact` object of the rules file. Raises RepeatContactError."""
    def fail(problem):
        return RepeatContactError("repeat_contact rule in %s: %s" % (path, problem))

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise fail("cannot read file (%s)" % (e.strerror or e))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise fail("not valid JSON")
    return repeat_contact_from_data(data, path)


def repeat_contact_from_data(data, source) -> RepeatContactRule:
    """Validate the `repeat_contact` block of an already-parsed rules object. Same checks
    and messages as `load_repeat_contact_rule`; `source` names where `data` came from."""
    def fail(problem):
        return RepeatContactError("repeat_contact rule in %s: %s" % (source, problem))

    block = data.get("repeat_contact") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise fail("'repeat_contact' is missing or is not an object")
    for key in ("window_days", "report_window_hours"):
        if key not in block:
            raise fail("'%s' is missing" % key)
        if not _positive_number(block[key]):
            raise fail("'%s' must be a finite number above 0" % key)
    if "min_conversations" not in block:
        raise fail("'min_conversations' is missing")
    n = block["min_conversations"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise fail("'min_conversations' must be an int of at least 2")
    if "effect" not in block:
        raise fail("'effect' is missing")
    if block["effect"] != "escalate":
        raise fail("'effect' must be \"escalate\"")
    return RepeatContactRule(window_days=block["window_days"], min_conversations=n,
                             effect="escalate",
                             report_window_hours=block["report_window_hours"])


def brand_attribution(thread, brand, known_brands) -> str:
    """Which brand a real thread belongs to at its decision moment: "brand", "other_brand"
    or "unattributed".

    The decision moment is the UTC time of the thread's last inbound turn. The thread is
    the brand's when a brand handle (a member of `known_brands` equal to `brand` ignoring
    case) is @mentioned in the first tweet, or replied at or before the decision moment.
    It is another brand's when some other known brand was mentioned or replied by then.
    Replies after the decision moment are never used.
    """
    inbound = [t for t in thread.turns if t.inbound]
    if not inbound:
        return "unattributed"
    record = contact_record(thread, known_brands)
    if record is None:
        return "unattributed"
    moment = inbound[-1].created_at.astimezone(timezone.utc)
    handles = {b for b in known_brands if b.lower() == brand.lower()}
    known_by_then = set(record.brands_at_start) | {
        who for who, at in record.brand_replies if at <= moment}
    if known_by_then & handles:
        return "brand"
    if known_by_then - handles:
        return "other_brand"
    return "unattributed"
