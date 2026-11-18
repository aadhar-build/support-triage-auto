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

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from triage.split import DEFAULT_SPLIT_PATH, load_split_config, split_threads
from triage.threads import Thread, build_threads

DEFAULT_DATA = "data/raw/twcs.csv"
REPO_ROOT = Path(__file__).resolve().parents[2]

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


class _Fail(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.pick_hard_cases",
                                description=__doc__.splitlines()[0])
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--split-config", default=str(DEFAULT_SPLIT_PATH))
    p.add_argument("--exclude-ids", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--summary-out")
    return p


def _load_exclude_ids(path) -> Set[int]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() if h else h for h in reader.fieldnames]
        return {int(row["thread_id"].strip()) for row in reader}


def _turn_record(turn) -> dict:
    return {"tweet_id": turn.tweet_id, "inbound": turn.inbound,
            "created_at": turn.created_at.isoformat(), "text": turn.text}


def _check_out_path(out) -> None:
    resolved = Path(out).resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise _Fail("--out %s is inside the repository %s; write the labelling file outside it"
                    % (resolved, REPO_ROOT))


def _run(args) -> str:
    _check_out_path(args.out)
    exclude = _load_exclude_ids(args.exclude_ids)
    config = load_split_config(args.split_config)
    threads, _ = build_threads(args.data)

    mapping, _ = split_threads(threads, config)
    eval_threads = [t for t in threads if mapping[t.root_tweet_id] == "eval"]
    eval_ids = {t.root_tweet_id for t in eval_threads}
    kept = [t for t in eval_threads if t.root_tweet_id not in exclude]

    candidates = []
    for t in sorted(kept, key=lambda t: t.root_tweet_id):
        signals = escalation_signals(t)
        if signals:
            candidates.append((t, signals))
    random.Random(args.seed).shuffle(candidates)
    candidates.sort(key=lambda c: len(c[1]), reverse=True)
    picked = candidates[:args.n]

    lines = [json.dumps({"rank": rank, "thread_id": t.root_tweet_id, "signals": list(signals),
                         "turns": [_turn_record(turn) for turn in t.turns]},
                        ensure_ascii=False) + "\n"
             for rank, (t, signals) in enumerate(picked, start=1)]
    with open(args.out, "w", encoding="utf-8") as f:
        f.writelines(lines)

    summary = {
        "threads_considered": len(eval_threads),
        "threads_excluded_random_sample": len(eval_threads) - len(kept),
        "exclude_ids_unmatched": len(exclude - eval_ids),
        "threads_qualifying": len(candidates),
        "threads_picked": len(picked),
        "picked_per_signal": {name: sum(1 for _, s in picked if name in s)
                              for name in SIGNAL_NAMES},
        "n": args.n,
        "seed": args.seed,
    }
    return json.dumps(summary, sort_keys=True, indent=2) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = _run(args)
    except _Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as f:
            f.write(summary)
    else:
        sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
