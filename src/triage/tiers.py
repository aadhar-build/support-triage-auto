"""Label vocabularies, the Labels record and the labelling-tier interfaces.

Every tier has a `name` and a `price_per_call`. The fakes here exist so the
cascade can be exercised offline: no network code, no API client, no keys.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

INTENTS = ("delivery", "billing", "account_access", "outage_technical", "complaint", "praise",
           "other")
URGENCIES = ("low", "normal", "high")
ACTIONS = ("answer", "ask", "route", "escalate")
ACTIONABLE_ACTIONS = ("answer", "ask")

# PLACEHOLDERS ONLY: these are NOT real prices. They are made-up per-call figures in
# an unstated unit, so the cost arithmetic can be tested. Real prices are the owner's call.
PLACEHOLDER_PRICES: Dict[str, float] = {"cheap": 0.0, "larger": 0.002, "agent": 0.01}


@dataclass(frozen=True)
class Labels:
    intent: str
    urgency: str
    action: str
    confidence: float

    def __post_init__(self):
        if self.intent not in INTENTS:
            raise ValueError("unknown intent %r" % (self.intent,))
        if self.urgency not in URGENCIES:
            raise ValueError("unknown urgency %r" % (self.urgency,))
        if self.action not in ACTIONS:
            raise ValueError("unknown action %r" % (self.action,))
        c = self.confidence
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
            raise ValueError("confidence must be a number in [0, 1], got %r" % (c,))


class LabellingTier(ABC):
    name: str
    price_per_call: float

    @abstractmethod
    def label(self, thread) -> Labels:
        """Return labels for one Thread."""


class AgentTier(ABC):
    name: str
    price_per_call: float

    @abstractmethod
    def act(self, thread, labels: Labels) -> Labels:
        """Act on an actionable thread and return the final labels."""


class FakeLargerTier(LabellingTier):
    """Stand-in for a larger model. Returns an override by root_tweet_id, else the
    cheap tier's labels with confidence 1.0. Not a model of real quality."""

    name = "larger"

    def __init__(self, overrides: Optional[Mapping[int, Labels]] = None,
                 price_per_call: float = PLACEHOLDER_PRICES["larger"]):
        from triage.cheap_tier import CheapTier  # local import: cheap_tier imports this module
        self.overrides = dict(overrides or {})
        self.price_per_call = price_per_call
        self._base = CheapTier()

    def label(self, thread) -> Labels:
        override = self.overrides.get(thread.root_tweet_id)
        if override is not None:
            return override
        base = self._base.label(thread)
        return Labels(base.intent, base.urgency, base.action, 1.0)


class FakeAgentTier(AgentTier):
    """Stand-in for a tool-using agent. Returns the labels unchanged and counts calls."""

    name = "agent"

    def __init__(self, price_per_call: float = PLACEHOLDER_PRICES["agent"]):
        self.price_per_call = price_per_call
        self.calls = 0

    def act(self, thread, labels: Labels) -> Labels:
        self.calls += 1
        return labels


def check_price(name: str, price) -> None:
    """Raise ValueError unless price is a finite number >= 0."""
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) \
            or price < 0:
        raise ValueError("price for %r must be a finite number >= 0, got %r" % (name, price))
