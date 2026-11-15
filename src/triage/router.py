"""Cascade router: cheap tier first, larger tier for low-confidence or high-risk
threads, agent tier for threads whose action is actionable."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from triage.tiers import ACTIONABLE_ACTIONS, AgentTier, Labels, LabellingTier

# Hand-picked placeholder, not tuned. Not read from or written to evals/thresholds.json.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True)
class RoutedThread:
    thread_id: int
    labels: Labels
    cheap_labels: Labels
    tiers: Tuple[str, ...]


def is_high_risk(labels: Labels) -> bool:
    return (labels.urgency == "high"
            or labels.intent in ("account_access", "outage_technical")
            or labels.action == "escalate")


def _check_threshold(threshold) -> None:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not math.isfinite(threshold) or not (0.0 <= threshold <= 1.0):
        raise ValueError("threshold must be a number in [0, 1], got %r" % (threshold,))


def _route(thread, cheap: LabellingTier, larger: LabellingTier, agent: AgentTier,
           threshold: float) -> RoutedThread:
    cheap_labels = cheap.label(thread)
    labels = cheap_labels
    tiers = [cheap.name]
    if cheap_labels.confidence < threshold or is_high_risk(cheap_labels):
        labels = larger.label(thread)
        tiers.append(larger.name)
    if labels.action in ACTIONABLE_ACTIONS:
        labels = agent.act(thread, labels)
        tiers.append(agent.name)
    return RoutedThread(thread_id=thread.root_tweet_id, labels=labels,
                        cheap_labels=cheap_labels, tiers=tuple(tiers))


def route_thread(thread, cheap: LabellingTier, larger: LabellingTier, agent: AgentTier,
                 threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> RoutedThread:
    _check_threshold(threshold)
    return _route(thread, cheap, larger, agent, threshold)


def route_threads(threads: Iterable, cheap: LabellingTier, larger: LabellingTier,
                  agent: AgentTier,
                  threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> List[RoutedThread]:
    _check_threshold(threshold)
    return [_route(t, cheap, larger, agent, threshold) for t in threads]
