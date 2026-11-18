"""Cascade router: cheap tier first, larger tier for low-confidence or high-risk
threads, agent tier for threads whose action is actionable.

Every routed thread records the tier steps it took and its reason codes. Reason codes
are plain, fixed strings registered in REASON_CODES; extra detail goes in its own field,
never inside the code string. A later job adds a code by defining its constant,
appending it to REASON_CODES and adding a test that produces it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from triage.tiers import ACTIONABLE_ACTIONS, AgentTier, Labels, LabellingTier

# Hand-picked placeholder, not tuned. Not read from or written to evals/thresholds.json.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5

CHEAP_CONFIDENT = "cheap_confident"
LOW_CONFIDENCE_TO_LARGER = "low_confidence_to_larger"
HIGH_RISK_TO_LARGER = "high_risk_to_larger"
CASCADE_CODES: Tuple[str, ...] = (CHEAP_CONFIDENT, LOW_CONFIDENCE_TO_LARGER, HIGH_RISK_TO_LARGER)

# The registry of every reason code any module may emit. Open: later jobs append to it.
REASON_CODES: Tuple[str, ...] = CASCADE_CODES


def is_known_reason_code(code, codes: Tuple[str, ...] = REASON_CODES) -> bool:
    """True only when code is a string in codes."""
    return isinstance(code, str) and code in codes


@dataclass(frozen=True)
class TierStep:
    tier: str
    model_id: str
    labels: Labels


@dataclass(frozen=True)
class RoutedThread:
    thread_id: int
    labels: Labels
    cheap_labels: Labels
    tiers: Tuple[str, ...]
    steps: Tuple[TierStep, ...] = ()
    reason_codes: Tuple[str, ...] = ()


def is_high_risk(labels: Labels) -> bool:
    return (labels.urgency == "high"
            or labels.intent in ("account_access", "outage_technical")
            or labels.action == "escalate")


def _check_threshold(threshold) -> None:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not math.isfinite(threshold) or not (0.0 <= threshold <= 1.0):
        raise ValueError("threshold must be a number in [0, 1], got %r" % (threshold,))


def _check_model_ids(*tiers) -> None:
    for tier in tiers:
        model_id = getattr(tier, "model_id", None)
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("tier %r has no model_id: every tier must set a non-empty string"
                             % (getattr(tier, "name", type(tier).__name__),))


def _cascade_code(cheap_labels: Labels, threshold: float) -> str:
    if is_high_risk(cheap_labels):
        return HIGH_RISK_TO_LARGER
    if cheap_labels.confidence < threshold:
        return LOW_CONFIDENCE_TO_LARGER
    return CHEAP_CONFIDENT


def _route(thread, cheap: LabellingTier, larger: LabellingTier, agent: AgentTier,
           threshold: float) -> RoutedThread:
    cheap_labels = cheap.label(thread)
    labels = cheap_labels
    steps = [TierStep(cheap.name, cheap.model_id, cheap_labels)]
    code = _cascade_code(cheap_labels, threshold)
    if code != CHEAP_CONFIDENT:
        labels = larger.label(thread)
        steps.append(TierStep(larger.name, larger.model_id, labels))
    if labels.action in ACTIONABLE_ACTIONS:
        labels = agent.act(thread, labels)
        steps.append(TierStep(agent.name, agent.model_id, labels))
    return RoutedThread(thread_id=thread.root_tweet_id, labels=labels,
                        cheap_labels=cheap_labels, tiers=tuple(s.tier for s in steps),
                        steps=tuple(steps), reason_codes=(code,))


def route_thread(thread, cheap: LabellingTier, larger: LabellingTier, agent: AgentTier,
                 threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> RoutedThread:
    _check_threshold(threshold)
    _check_model_ids(cheap, larger, agent)
    return _route(thread, cheap, larger, agent, threshold)


def route_threads(threads: Iterable, cheap: LabellingTier, larger: LabellingTier,
                  agent: AgentTier,
                  threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> List[RoutedThread]:
    _check_threshold(threshold)
    _check_model_ids(cheap, larger, agent)
    return [_route(t, cheap, larger, agent, threshold) for t in threads]
