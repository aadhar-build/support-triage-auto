"""Owner-edited list prices for model tiers, in USD per million tokens.

The code holds no price defaults: every price comes from the file passed to load_prices
(by default policies/model_prices.json, which the owner creates). A model or token kind
with no price is an unknown bill, and callers must fail closed on it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

_PRICE_KEYS = (("input", "input_usd_per_mtok", True), ("output", "output_usd_per_mtok", True),
               ("cache_write", "cache_write_usd_per_mtok", False),
               ("cache_read", "cache_read_usd_per_mtok", False))


class PricesError(ValueError):
    """The price file is missing, unreadable or malformed. The message names the file."""


class UnknownBillError(ValueError):
    """A model or token kind has no price, so the cost of a call is unknown."""


@dataclass(frozen=True)
class Band:
    max_input_tokens: Optional[int]
    input: float
    output: float
    cache_write: Optional[float] = None
    cache_read: Optional[float] = None


@dataclass(frozen=True)
class Prices:
    version: str
    models: Dict[str, Tuple[Band, ...]]

    def band(self, model: str, input_tokens: int) -> Band:
        """The first band whose limit is null or >= input_tokens (all input-side tokens)."""
        bands = self.models.get(model)
        if not bands:
            raise UnknownBillError("model %r has no price" % (model,))
        for band in bands:
            if band.max_input_tokens is None or band.max_input_tokens >= input_tokens:
                return band
        raise UnknownBillError("model %r has no band for %d input tokens"
                               % (model, input_tokens))

    def cost(self, model: str, usage) -> float:
        """USD for one model's usage (a TokenUsage). Raises UnknownBillError."""
        total_in = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
        band = self.band(model, total_in)
        usd = 0.0
        for count, price, kind in ((usage.input_tokens, band.input, "input"),
                                   (usage.output_tokens, band.output, "output"),
                                   (usage.cache_write_tokens, band.cache_write, "cache_write"),
                                   (usage.cache_read_tokens, band.cache_read, "cache_read")):
            if count:
                if price is None:
                    raise UnknownBillError("model %r has no %s price in its band"
                                           % (model, kind))
                usd += count * price
        return usd / 1_000_000


def _price(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) and value > 0


def load_prices(path) -> Prices:
    """Read and validate a price file. Raises PricesError naming the file and the problem."""
    def fail(problem):
        raise PricesError("price file %s: %s" % (path, problem))

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        fail("not found")
    except OSError as e:
        fail("cannot read: %s" % (e.strerror or e))
    except (ValueError, UnicodeDecodeError):
        fail("not valid JSON")
    if not isinstance(data, dict):
        fail("top level is not an object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        fail("version is missing or empty")
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        fail("models is missing or empty")
    parsed: Dict[str, Tuple[Band, ...]] = {}
    for model, entry in models.items():
        bands = entry.get("bands") if isinstance(entry, dict) else None
        if not isinstance(bands, list) or not bands:
            fail("model %r has no bands" % (model,))
        out = []
        prev = None
        for i, b in enumerate(bands):
            if not isinstance(b, dict):
                fail("model %r band %d is not an object" % (model, i))
            limit = b.get("max_input_tokens", "missing")
            if limit is None:
                if i != len(bands) - 1:
                    fail("model %r band %d: null max_input_tokens is only allowed last"
                         % (model, i))
            elif isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                fail("model %r band %d: max_input_tokens must be a positive int or null"
                     % (model, i))
            elif prev is not None and limit <= prev:
                fail("model %r band %d: bands are out of order" % (model, i))
            prev = limit if limit is not None else prev
            values = {}
            for attr, key, required in _PRICE_KEYS:
                if key not in b:
                    if required:
                        fail("model %r band %d: %s is missing" % (model, i, key))
                    values[attr] = None
                elif not _price(b[key]):
                    fail("model %r band %d: %s must be a finite number > 0" % (model, i, key))
                else:
                    values[attr] = float(b[key])
            out.append(Band(max_input_tokens=limit, **values))
        parsed[model] = tuple(out)
    return Prices(version=version, models=parsed)
