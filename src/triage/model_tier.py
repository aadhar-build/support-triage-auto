"""A model-backed `larger` tier with hard spend caps.

ModelLargerTier labels a Thread by sending a versioned system prompt
(prompts/larger_tier.md) and the conversation, built only by
quoting.quote_conversation, to a ModelClient. Every call first reserves its worst-case
API-equivalent cost against the per-run cap and the durable monthly SpendLog. Bad output,
failed calls and calls a cap would block fall back to the cheap tier's labels, counted by
reason; a failed call or an unknown bill stops paid calls for the rest of the run.

ClaudeCodeClient runs headless Claude Code (`claude -p`) on the subscription login. The
customer text goes on stdin, never in argv, and API-key variables are stripped from the
child environment. FakeModelClient is the scripted stand-in for tests.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

from triage import quoting
from triage.model_prices import UnknownBillError
from triage.spend_log import MonthlyCapError, SpendLogError
from triage.tiers import ACTIONS, INTENTS, PLACEHOLDER_PRICES, URGENCIES, Labels, \
    LabellingTier

MAX_RUN_SPEND_USD = 2.50
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "larger_tier.md"
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
FALLBACK_REASONS = ("spend_cap", "monthly_cap", "spend_log", "unknown_bill",
                    "reservation_exceeded", "api_error", "too_long", "parse_error",
                    "invalid_labels")
_VERSION_RE = re.compile(r"<!-- prompt-version: ([a-z0-9][a-z0-9.-]*) -->")
_TimeoutExpired = subprocess.TimeoutExpired


class ModelCallError(Exception):
    """A model call failed. The message is fixed and never holds output or prompts."""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class ModelReply:
    text: str
    usage: Mapping[str, TokenUsage] = field(default_factory=dict)


# -- prompt ----------------------------------------------------------------------------

def parse_prompt_version(text: str) -> str:
    first = text.split("\n", 1)[0]
    m = _VERSION_RE.fullmatch(first.rstrip("\r"))
    if not m:
        raise ValueError("prompt file line 1 must be '<!-- prompt-version: <id> -->'")
    return m.group(1)


def load_prompt(path=PROMPT_PATH) -> str:
    """Return the rendered system prompt (label sets filled in). Raises ValueError."""
    text = Path(path).read_text(encoding="utf-8")
    parse_prompt_version(text)
    return (text.replace("{{INTENTS}}", ", ".join(INTENTS))
            .replace("{{URGENCIES}}", ", ".join(URGENCIES))
            .replace("{{ACTIONS}}", ", ".join(ACTIONS)))


PROMPT_VERSION = parse_prompt_version(PROMPT_PATH.read_text(encoding="utf-8"))


# -- clients ---------------------------------------------------------------------------

class ModelClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> ModelReply:
        """Return the model's reply. Raises ModelCallError on any failure."""


class FakeModelClient(ModelClient):
    """Returns scripted replies in order; a scripted ModelCallError is raised instead."""

    def __init__(self, replies: Iterable):
        self._replies = list(replies)
        self.calls = []

    def complete(self, system: str, user: str) -> ModelReply:
        self.calls.append((system, user))
        if not self._replies:
            raise ModelCallError("model call failed: fake has no more replies")
        reply = self._replies.pop(0)
        if isinstance(reply, ModelCallError):
            raise reply
        return reply


_USAGE_KEYS = (("input_tokens", "inputTokens", True), ("output_tokens", "outputTokens", True),
               ("cache_write_tokens", "cacheCreationInputTokens", False),
               ("cache_read_tokens", "cacheReadInputTokens", False))


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_usage(model_usage) -> Dict[str, TokenUsage]:
    if not isinstance(model_usage, dict) or not model_usage:
        raise ModelCallError("model call failed: bad usage")
    out = {}
    for model, u in model_usage.items():
        if not isinstance(model, str) or not model or not isinstance(u, dict):
            raise ModelCallError("model call failed: bad usage")
        values = {}
        for attr, key, required in _USAGE_KEYS:
            if key not in u:
                if required:
                    raise ModelCallError("model call failed: bad usage")
                values[attr] = 0
            elif not _count(u[key]):
                raise ModelCallError("model call failed: bad usage")
            else:
                values[attr] = u[key]
        out[model] = TokenUsage(**values)
    return out


class ClaudeCodeClient(ModelClient):
    """Headless Claude Code on the subscription login. Never reads any credential file."""

    def __init__(self, model: str, claude_bin: str = "claude", runner=None,
                 timeout_s: float = 120):
        self.model = model
        self.claude_bin = claude_bin
        self.runner = runner
        self.timeout_s = timeout_s

    def __repr__(self) -> str:
        return "ClaudeCodeClient(model=%r)" % (self.model,)

    def argv(self, system: str):
        return [self.claude_bin, "-p", "--output-format", "json", "--model", self.model,
                "--system-prompt", system, "--tools", "", "--safe-mode",
                "--strict-mcp-config", "--no-session-persistence"]

    def complete(self, system: str, user: str) -> ModelReply:
        run = self.runner if self.runner is not None else subprocess.run
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        error = None
        with tempfile.TemporaryDirectory(prefix="triage-model-") as cwd:
            try:
                proc = run(self.argv(system), input=user, capture_output=True, text=True,
                           timeout=self.timeout_s, cwd=cwd, env=env)
            except _TimeoutExpired:
                error = "model call failed: timeout"
            except OSError:
                error = "model call failed: cannot start"
        if error:
            raise ModelCallError(error) from None
        if proc.returncode != 0:
            raise ModelCallError("model call failed: exit %d" % proc.returncode) from None
        try:
            data = json.loads(proc.stdout)
        except (TypeError, ValueError):
            raise ModelCallError("model call failed: bad output") from None
        if not isinstance(data, dict) or data.get("is_error") is not False \
                or not isinstance(data.get("result"), str):
            raise ModelCallError("model call failed: bad output") from None
        usage = _parse_usage(data.get("modelUsage"))
        return ModelReply(text=data["result"], usage=usage)


# -- tier ------------------------------------------------------------------------------

def _parse_labels(text: str):
    """Return (Labels, None) or (None, reason)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None, "parse_error"
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None, "parse_error"
    if not isinstance(obj, dict):
        return None, "parse_error"
    try:
        intent, urgency, action, conf = (obj["intent"], obj["urgency"], obj["action"],
                                         obj["confidence"])
    except KeyError:
        return None, "invalid_labels"
    if intent not in INTENTS or urgency not in URGENCIES or action not in ACTIONS:
        return None, "invalid_labels"
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
            or not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        return None, "invalid_labels"
    return Labels(intent, urgency, action, conf), None


class ModelLargerTier(LabellingTier):
    name = "larger"

    def __init__(self, client: ModelClient, fallback: LabellingTier, prices, spend_log,
                 model: str, run_id: str, split: str, max_spend_usd: float,
                 max_input_tokens: int = 50_000, reserve_output_tokens: int = 1_000,
                 overhead_tokens: int = 20_000,
                 price_per_call: float = PLACEHOLDER_PRICES["larger"]):
        if isinstance(max_spend_usd, bool) or not isinstance(max_spend_usd, (int, float)) \
                or not math.isfinite(max_spend_usd) \
                or not 0 <= max_spend_usd <= MAX_RUN_SPEND_USD:
            raise ValueError("max_spend_usd must be a finite number in [0, %.2f]"
                             % MAX_RUN_SPEND_USD)
        self.client = client
        self.fallback = fallback
        self.prices = prices
        self.spend_log = spend_log
        self.model = model
        self.model_id = model
        self.run_id = run_id
        self.split = split
        self.max_spend_usd = float(max_spend_usd)
        self.max_input_tokens = max_input_tokens
        self.reserve_output_tokens = reserve_output_tokens
        self.overhead_tokens = overhead_tokens
        self.price_per_call = price_per_call
        self.prompt_version = PROMPT_VERSION
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_tokens = 0
        self.cache_read_tokens = 0
        self.confirmed_usd = 0.0
        self.unconfirmed_usd = 0.0
        self.stopped_by: Optional[str] = None
        self.answered = 0
        self.reservation_exceeded = 0
        self.fallbacks: Dict[str, int] = {k: 0 for k in FALLBACK_REASONS}

    @property
    def spent_usd(self) -> float:
        return self.confirmed_usd + self.unconfirmed_usd

    def _fall_back(self, reason: str, thread) -> Labels:
        self.fallbacks[reason] += 1
        return self.fallback.label(thread)

    def _stop(self, reason: str, thread) -> Labels:
        self.stopped_by = reason
        return self._fall_back(reason, thread)

    def label(self, thread) -> Labels:
        if self.stopped_by is not None:
            return self._fall_back(self.stopped_by, thread)
        system = load_prompt()
        user = quoting.quote_conversation(thread)
        estimate = len(system.encode("utf-8")) + len(user.encode("utf-8")) \
            + self.overhead_tokens
        if estimate > self.max_input_tokens:
            return self._fall_back("too_long", thread)
        try:
            band = self.prices.band(self.model, estimate)
        except UnknownBillError:
            return self._stop("unknown_bill", thread)
        reserve_usd = (estimate * band.input + self.reserve_output_tokens * band.output) \
            / 1_000_000
        if self.spent_usd + reserve_usd > self.max_spend_usd:
            return self._stop("spend_cap", thread)
        try:
            rid = self.spend_log.reserve(self.run_id, self.model, self.split, reserve_usd)
        except MonthlyCapError:
            return self._stop("monthly_cap", thread)
        except SpendLogError:
            return self._stop("spend_log", thread)

        try:
            reply = self.client.complete(system, user)
        except ModelCallError:
            self._unknown(rid, reserve_usd)
            self.fallbacks["api_error"] += 1
            return self.fallback.label(thread)

        self._account(rid, reserve_usd, reply)
        labels, reason = _parse_labels(reply.text)
        if labels is None:
            return self._fall_back(reason, thread)
        self.answered += 1
        return labels

    def _unknown(self, rid: str, reserve_usd: float) -> None:
        self.unconfirmed_usd += reserve_usd
        try:
            self.spend_log.mark_unknown(rid)
        except SpendLogError:
            self.stopped_by = "spend_log"
            return
        self.stopped_by = "unknown_bill"

    def _account(self, rid: str, reserve_usd: float, reply: ModelReply) -> None:
        try:
            cost = sum(self.prices.cost(m, u) for m, u in reply.usage.items())
            if not reply.usage:
                raise UnknownBillError("no usage reported")
        except UnknownBillError:
            self._unknown(rid, reserve_usd)
            return
        totals = {k: sum(getattr(u, k) for u in reply.usage.values())
                  for k in ("input_tokens", "output_tokens", "cache_write_tokens",
                            "cache_read_tokens")}
        try:
            self.spend_log.settle(rid, cost, totals)
        except SpendLogError:
            self.unconfirmed_usd += reserve_usd
            self.stopped_by = "spend_log"
            return
        self.confirmed_usd += cost
        for k, v in totals.items():
            setattr(self, k, getattr(self, k) + v)
        if cost > reserve_usd:
            self.reservation_exceeded += 1
            self.stopped_by = "reservation_exceeded"
