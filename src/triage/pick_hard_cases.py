"""Pick escalation-likely eval threads as candidates for a hard-case gold set.

Usage: python3 -m triage.pick_hard_cases --exclude-ids RANDOM300.csv --out LABELLING.jsonl
       [--data data/raw/twcs.csv] [--split-config data/split.json] [--n 100] [--seed 0]
       [--summary-out SUMMARY.json]

Only eval-split threads are considered; dev and full threads, and threads whose id is in
--exclude-ids (a CSV with a thread_id column), are never picked. Threads are ranked by
how many distinct escalation signals their inbound turns hit, ties broken by a seeded
shuffle. Thread ids and text go only to --out, which must resolve outside this repository.
The summary holds aggregate counts only.

The signal word lists are defined here and are independent of the cheap tier's keywords.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from triage.threads import Thread

SIGNALS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("legal", ("lawyer", "lawyers", "attorney", "solicitor", "sue", "suing", "lawsuit",
               "legal action", "court", "regulator", "ombudsman", "trading standards", "bbb",
               "ftc", "fcc", "press", "journalist", "media")),
    ("fraud_or_hacked", ("fraud", "fraudulent", "scam", "scammed", "hacked", "hacker", "stolen",
                         "unauthorised", "unauthorized", "unrecognised", "unrecognized",
                         "identity theft", "didn't authorise", "didn't authorize")),
    ("safety", ("injury", "injured", "hurt", "danger", "dangerous", "unsafe", "fire",
                "caught fire", "burned", "burnt", "smoke", "exploded", "explode", "self harm",
                "suicide", "suicidal")),
    ("repeated_contact", ("again", "third time", "fourth time", "fifth time", "multiple times",
                          "several times", "how many times", "still no reply",
                          "still no response", "still waiting", "no one has", "nobody has",
                          "no reply", "no response")),
    ("strong_negative", ("worst", "terrible", "awful", "horrible", "disgusting", "disgraceful",
                         "appalling", "unacceptable", "ridiculous", "pathetic", "useless",
                         "furious", "outraged", "shocking")),
)
SIGNAL_NAMES: Tuple[str, ...] = tuple(name for name, _ in SIGNALS)

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_PHRASES: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    name: tuple(tuple(_TOKEN_RE.findall(entry)) for entry in entries) for name, entries in SIGNALS
}


def _tokens(thread: Thread) -> List[str]:
    text = "\n".join(t.text for t in thread.turns if t.inbound)
    return _TOKEN_RE.findall(text.lower().replace("’", "'"))


def _has_run(tokens: List[str], phrase: Tuple[str, ...]) -> bool:
    k = len(phrase)
    return any(tuple(tokens[i:i + k]) == phrase for i in range(len(tokens) - k + 1))


def escalation_signals(thread: Thread) -> Tuple[str, ...]:
    """Signals hit by the thread's inbound turns, in SIGNAL_NAMES order."""
    tokens = _tokens(thread)
    token_set = set(tokens)
    hits = []
    for name in SIGNAL_NAMES:
        for phrase in _PHRASES[name]:
            if (phrase[0] in token_set if len(phrase) == 1 else _has_run(tokens, phrase)):
                hits.append(name)
                break
    return tuple(hits)
