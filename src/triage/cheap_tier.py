"""Cheap tier: a transparent rule and keyword baseline. Standard library only.

Every rule is a constant in this file, so a reader can see why a label was given.
The keyword lists and confidences are hand-picked placeholders, not tuned on any split.

How a thread is labelled:
1. Join the text of the inbound (customer) turns in order, lowercase, split into
   tokens with [a-z0-9']+. Brand turns are ignored.
2. A single-word keyword matches a whole token; a multi-word keyword matches the
   same run of consecutive tokens. Each distinct keyword counts once.
3. Intent = most distinct keyword hits; ties go to the earliest in INTENT_PRIORITY;
   no hits gives "other".
4. Confidence: NO_MATCH_CONFIDENCE for no hits, TIE_CONFIDENCE for a tie at the top,
   otherwise CONFIDENCE_BY_HITS by the winning hit count (3 or more -> the top value).
5. Urgency: "high" on any URGENT_KEYWORDS or ESCALATE_KEYWORDS hit, else "low" for
   praise, else "normal".
6. Action, first match wins: escalate keyword -> escalate; account_access or
   outage_technical -> route; high-urgency complaint -> escalate; delivery, billing
   or other -> ask; complaint or praise -> answer.
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, Iterable, List, Sequence, Tuple

from triage.tiers import PLACEHOLDER_PRICES, Labels, LabellingTier

INTENT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "delivery": ("package", "delivery", "shipping", "tracking", "parcel", "delivered",
                 "shipment", "courier"),
    "billing": ("charged", "refund", "bill", "invoice", "payment", "overcharged", "billing"),
    "account_access": ("password", "login", "locked out", "log in", "2fa", "sign in",
                       "verification code"),
    "outage_technical": ("outage", "down", "not working", "error", "crash", "crashing",
                         "bug"),
    "complaint": ("terrible", "worst", "awful", "disappointed", "rude", "horrible",
                  "unacceptable"),
    "praise": ("thank you", "thanks", "love", "awesome", "great service", "amazing"),
}

INTENT_PRIORITY: Tuple[str, ...] = ("account_access", "outage_technical", "billing", "delivery",
                                    "complaint", "praise")

URGENT_KEYWORDS: Tuple[str, ...] = ("urgent", "asap", "immediately", "emergency")
ESCALATE_KEYWORDS: Tuple[str, ...] = ("lawyer", "legal action", "fraud", "hacked", "stolen")

NO_MATCH_CONFIDENCE = 0.2
TIE_CONFIDENCE = 0.4
CONFIDENCE_BY_HITS: Dict[int, float] = {1: 0.6, 2: 0.75, 3: 0.9}  # 3 or more -> 0.9

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def _ngrams(tokens: Sequence[str], max_n: int) -> FrozenSet[Tuple[str, ...]]:
    grams = set()
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            grams.add(tuple(tokens[i:i + n]))
    return frozenset(grams)


def _all_keywords() -> Iterable[str]:
    for kws in INTENT_KEYWORDS.values():
        yield from kws
    yield from URGENT_KEYWORDS
    yield from ESCALATE_KEYWORDS


_MAX_N = max(len(k.split()) for k in _all_keywords())


def _hits(grams: FrozenSet[Tuple[str, ...]], keywords: Iterable[str]) -> int:
    return sum(1 for k in set(keywords) if tuple(k.split()) in grams)


def _confidence(top: int, tied: bool) -> float:
    if top == 0:
        return NO_MATCH_CONFIDENCE
    if tied:
        return TIE_CONFIDENCE
    return CONFIDENCE_BY_HITS[min(top, max(CONFIDENCE_BY_HITS))]


def _action(intent: str, urgency: str, escalate_hit: bool) -> str:
    if escalate_hit:
        return "escalate"
    if intent in ("account_access", "outage_technical"):
        return "route"
    if intent == "complaint" and urgency == "high":
        return "escalate"
    if intent in ("delivery", "billing", "other"):
        return "ask"
    return "answer"  # complaint or praise


class CheapTier(LabellingTier):
    name = "cheap"

    def __init__(self, price_per_call: float = PLACEHOLDER_PRICES["cheap"]):
        self.price_per_call = price_per_call

    def label(self, thread) -> Labels:
        text = "\n".join(t.text for t in thread.turns if t.inbound is True)
        grams = _ngrams(_tokens(text), _MAX_N)

        counts = {intent: _hits(grams, INTENT_KEYWORDS[intent]) for intent in INTENT_PRIORITY}
        top = max(counts.values())
        leaders = [i for i in INTENT_PRIORITY if counts[i] == top]
        intent = leaders[0] if top > 0 else "other"
        confidence = _confidence(top, len(leaders) > 1)

        escalate_hit = _hits(grams, ESCALATE_KEYWORDS) > 0
        if escalate_hit or _hits(grams, URGENT_KEYWORDS) > 0:
            urgency = "high"
        elif intent == "praise":
            urgency = "low"
        else:
            urgency = "normal"

        return Labels(intent, urgency, _action(intent, urgency, escalate_hit), confidence)
