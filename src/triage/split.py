"""Assign conversation threads to the dev, eval and full splits.

The split is by thread, keyed on Thread.root_tweet_id, so every tweet of a
thread lands in the same split. The seed and fractions come from the owner's
data/split.json, which this module only reads.

Assignment is a pure function of (seed, root id):
    u = first 8 bytes of sha256("{seed}:{root_id}") as a big-endian fraction of 2**64
and u is compared against the cumulative fractions in SPLIT_NAMES order.
Changing this recipe or the order of SPLIT_NAMES moves threads between splits
and invalidates every downstream eval, so treat it as frozen.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Tuple, Union

from triage.threads import Thread

SPLIT_NAMES: Tuple[str, ...] = ("dev", "eval", "full")
DEFAULT_SPLIT_PATH = Path(__file__).resolve().parents[2] / "data" / "split.json"
_SUM_TOLERANCE = 1e-9


class SplitConfigError(ValueError):
    """data/split.json (or a config dict) is missing or malformed."""


@dataclass(frozen=True)
class SplitConfig:
    seed: int
    fractions: Tuple[Tuple[str, float], ...]


@dataclass(frozen=True)
class SplitReport:
    seed: int
    total: int
    counts: Dict[str, int]
    shares: Dict[str, float]
    targets: Dict[str, float]


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def config_from_dict(data) -> SplitConfig:
    if not isinstance(data, dict):
        raise SplitConfigError("split config must be a JSON object")
    if "seed" not in data:
        raise SplitConfigError("seed is missing")
    seed = data["seed"]
    if not _is_int(seed):
        raise SplitConfigError("seed must be an int, got %r" % (seed,))
    if data.get("unit") != "thread" or not isinstance(data.get("unit"), str):
        raise SplitConfigError("unit must be 'thread', got %r" % (data.get("unit"),))
    if "fractions" not in data:
        raise SplitConfigError("fractions is missing")
    fr = data["fractions"]
    if not isinstance(fr, dict):
        raise SplitConfigError("fractions must be an object")
    missing = set(SPLIT_NAMES) - set(fr)
    extra = set(fr) - set(SPLIT_NAMES)
    if missing:
        raise SplitConfigError("fractions missing split name(s): %s" % sorted(missing))
    if extra:
        raise SplitConfigError("fractions has unknown split name(s): %s"
                               % sorted(map(str, extra)))
    values = []
    for name in SPLIT_NAMES:
        v = fr[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SplitConfigError("fraction %r must be a number, got %r" % (name, v))
        if not math.isfinite(v):
            raise SplitConfigError("fraction %r must be finite, got %r" % (name, v))
        if v < 0:
            raise SplitConfigError("fraction %r must not be negative, got %r" % (name, v))
        values.append((name, v))
    total = sum(v for _, v in values)
    if abs(total - 1) > _SUM_TOLERANCE:
        raise SplitConfigError("fractions sum to %r, expected 1" % (total,))
    return SplitConfig(seed=seed, fractions=tuple(values))


def load_split_config(path=DEFAULT_SPLIT_PATH) -> SplitConfig:
    """Read a split config file (read-only) and validate it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError) as e:
        raise SplitConfigError("cannot read split config %s: %s" % (path, e)) from e
    except json.JSONDecodeError as e:
        raise SplitConfigError("split config %s is not valid JSON: %s" % (path, e)) from e
    return config_from_dict(data)


def assign_split(key: int, config: SplitConfig) -> str:
    """Return the split name for one thread root id. Pure in (seed, key)."""
    if not _is_int(key):
        raise TypeError("thread key must be an int, got %r" % (key,))
    digest = hashlib.sha256(("%d:%d" % (config.seed, key)).encode("ascii")).digest()
    u = int.from_bytes(digest[:8], "big") / 2 ** 64
    cumulative = 0.0
    for name, frac in config.fractions:
        cumulative += frac
        if u < cumulative:
            return name
    return config.fractions[-1][0]


def split_threads(items: Iterable[Union[Thread, int]],
                  config: SplitConfig) -> Tuple[Dict[int, str], SplitReport]:
    """Assign each unique root id to a split. Iterates `items` exactly once."""
    mapping: Dict[int, str] = {}
    for item in items:
        if isinstance(item, Thread):
            key = item.root_tweet_id
        elif _is_int(item):
            key = item
        else:
            raise TypeError("expected a Thread or an int root id, got %r" % (item,))
        if key not in mapping:
            mapping[key] = assign_split(key, config)

    total = len(mapping)
    counts = {name: 0 for name in SPLIT_NAMES}
    for name in mapping.values():
        counts[name] += 1
    shares = {name: (counts[name] / total if total else 0.0) for name in SPLIT_NAMES}
    report = SplitReport(seed=config.seed, total=total, counts=counts, shares=shares,
                         targets=dict(config.fractions))
    return mapping, report
