"""Incident grouping: turn a burst of outage threads for one brand into one incident.

Usage: python3 -m triage.incidents --split {dev,eval,full} --report-out PATH
       [--format {json,md}] [--config evals/drift.json] [--data data/raw/twcs.csv]
       [--split-config data/split.json] [--incidents-out PATH]

Threads are replayed in (first-turn time, root_tweet_id) order. A thread's label comes
from its first turn only; its brands come from @mentions in that first turn and from
brand replies, each counted only from the moment it was known. For each brand a
trailing window counts outage threads; a burst that reaches min_burst and beats the
brand's recent baseline opens an incident. Every number comes from the `incidents`
section of the config (load_incident_config); there are no defaults.

The report holds aggregates only. Incident ids, brands and top terms go only to
--incidents-out, which must resolve outside the repository and is refused for the
eval split.

Standard library only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional


class IncidentConfigError(ValueError):
    """The incident config is missing or malformed. The message names the file."""


@dataclass(frozen=True)
class IncidentConfig:
    path: str
    version: Optional[str]
    window_hours: float
    min_burst: int
    baseline_factor: float
    baseline_lookback_hours: float
    label: str


OUTAGE_LABEL = "outage_technical"


def _positive_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def load_incident_config(path) -> IncidentConfig:
    """Read `version` and the top-level `incidents` object of a config shaped like
    evals/drift.json. Any problem raises IncidentConfigError naming the file; there is
    no default and no fallback value."""
    def fail(problem: str):
        return IncidentConfigError("incident config %s: %s" % (path, problem))

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
    section = data.get("incidents")
    if not isinstance(section, dict):
        raise fail("'incidents' is missing or is not an object")

    for key in ("window_hours", "min_burst", "baseline_factor", "baseline_lookback_hours",
                "label"):
        if key not in section:
            raise fail("'incidents.%s' is missing" % key)
    for key in ("window_hours", "baseline_factor", "baseline_lookback_hours"):
        if not _positive_number(section[key]):
            raise fail("'incidents.%s' must be a finite number above 0, got %r"
                       % (key, section[key]))
    min_burst = section["min_burst"]
    if not isinstance(min_burst, int) or isinstance(min_burst, bool) or min_burst < 2:
        raise fail("'incidents.min_burst' must be an int of at least 2, got %r" % (min_burst,))
    if section["baseline_lookback_hours"] < section["window_hours"]:
        raise fail("'incidents.baseline_lookback_hours' %r is shorter than "
                   "'incidents.window_hours' %r"
                   % (section["baseline_lookback_hours"], section["window_hours"]))
    if section["label"] != OUTAGE_LABEL:
        raise fail("'incidents.label' must be %r, got %r" % (OUTAGE_LABEL, section["label"]))
    return IncidentConfig(path=str(path), version=version,
                          window_hours=section["window_hours"], min_burst=min_burst,
                          baseline_factor=section["baseline_factor"],
                          baseline_lookback_hours=section["baseline_lookback_hours"],
                          label=OUTAGE_LABEL)
