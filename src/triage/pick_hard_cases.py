"""Pick eval threads as candidates for a hard-case gold set, in two groups.

Usage: python3 -m triage.pick_hard_cases --exclude-ids RANDOM300.csv --out LABELLING.jsonl
       [--data data/raw/twcs.csv] [--split-config data/split.json]
       [--n-signal 70] [--n-random 30] [--seed 0] [--summary-out SUMMARY.json]

Only eval-split threads are considered; dev and full threads, and threads whose id is in
--exclude-ids (a CSV with a thread_id column), are never picked. The remaining threads
form two groups:
- "signal": up to --n-signal threads whose inbound turns hit an escalation signal, ranked
  by how many distinct signals they hit, ties broken by a seeded shuffle.
- "no_signal_random": up to --n-random threads with no signal hit, drawn at random with
  the same seed, as a control group.
Each picked thread records its group; signal picks come first, then random picks, and
"rank" is the position in the whole file. The same inputs and seed give the same output.
Thread ids and text go only to --out, which must resolve outside this repository. The
summary holds aggregate counts only, including counts per group.

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

from triage.loader import HeaderError
from triage.split import DEFAULT_SPLIT_PATH, SplitConfigError, load_split_config, split_threads
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
GROUP_SIGNAL = "signal"
GROUP_RANDOM = "no_signal_random"

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_ID_RE = re.compile(r"\d+", re.ASCII)
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
    p.add_argument("--n-signal", type=int, default=70)
    p.add_argument("--n-random", type=int, default=30)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--summary-out")
    return p


def _load_exclude_ids(path) -> Set[int]:
    """Read the thread_id column of a CSV. Bad content raises _Fail."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise _Fail("exclusion file %s is empty: expected a thread_id column" % path)
        reader.fieldnames = [h.strip() if h else h for h in reader.fieldnames]
        if "thread_id" not in reader.fieldnames:
            raise _Fail("exclusion file %s has no thread_id column" % path)
        ids: Set[int] = set()
        for row in reader:
            value = row.get("thread_id")
            if value is None:
                raise _Fail("exclusion file %s line %d: row has too few fields"
                            % (path, reader.line_num))
            value = value.strip()
            if not _ID_RE.fullmatch(value):
                raise _Fail("exclusion file %s line %d: thread_id must be a non-negative integer"
                            % (path, reader.line_num))
            ids.add(int(value))
    return ids


def _turn_record(turn) -> dict:
    return {"tweet_id": turn.tweet_id, "inbound": turn.inbound,
            "created_at": turn.created_at.isoformat(), "text": turn.text}


def _check_out_path(out) -> None:
    resolved = Path(out).resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise _Fail("--out %s is inside the repository %s; write the labelling file outside it"
                    % (resolved, REPO_ROOT))


def _write(path, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise _Fail("cannot write %s: %s" % (path, e.strerror or e))


def _run(args) -> str:
    for flag, value in (("--n-signal", args.n_signal), ("--n-random", args.n_random)):
        if value < 0:
            raise _Fail("%s must be at least 0, got %d" % (flag, value))
    if args.n_signal + args.n_random < 1:
        raise _Fail("--n-signal and --n-random are both 0: nothing to pick")
    _check_out_path(args.out)

    try:
        exclude = _load_exclude_ids(args.exclude_ids)
    except FileNotFoundError:
        raise _Fail("exclusion file not found: %s" % args.exclude_ids)
    except OSError as e:
        raise _Fail("cannot read exclusion file %s: %s" % (args.exclude_ids, e.strerror or e))
    except UnicodeDecodeError as e:
        raise _Fail("exclusion file %s is not valid UTF-8 (byte offset %d)"
                    % (args.exclude_ids, e.start))

    try:
        config = load_split_config(args.split_config)
    except SplitConfigError as e:
        raise _Fail("bad split config: %s" % e)

    try:
        threads, _ = build_threads(args.data)
    except FileNotFoundError:
        raise _Fail("data file not found: %s (hint: run scripts/download_data.sh)" % args.data)
    except OSError as e:
        raise _Fail("cannot read data file %s: %s" % (args.data, e.strerror or e))
    except HeaderError as e:
        raise _Fail("bad header in %s: %s" % (args.data, e))
    except UnicodeDecodeError as e:
        raise _Fail("%s is not valid UTF-8 (byte offset %d)" % (args.data, e.start))

    mapping, _ = split_threads(threads, config)
    eval_threads = [t for t in threads if mapping[t.root_tweet_id] == "eval"]
    eval_ids = {t.root_tweet_id for t in eval_threads}
    kept = [t for t in eval_threads if t.root_tweet_id not in exclude]

    candidates = []
    quiet = []
    for t in sorted(kept, key=lambda t: t.root_tweet_id):
        signals = escalation_signals(t)
        if signals:
            candidates.append((t, signals))
        else:
            quiet.append((t, signals))
    rng = random.Random(args.seed)
    rng.shuffle(candidates)
    candidates.sort(key=lambda c: len(c[1]), reverse=True)
    picked_signal = candidates[:args.n_signal]
    picked_random = rng.sample(quiet, min(args.n_random, len(quiet)))
    picked = ([(GROUP_SIGNAL, t, s) for t, s in picked_signal]
              + [(GROUP_RANDOM, t, s) for t, s in picked_random])

    lines = "".join(json.dumps({"rank": rank, "group": group, "thread_id": t.root_tweet_id,
                                "signals": list(signals),
                                "turns": [_turn_record(turn) for turn in t.turns]},
                               ensure_ascii=False) + "\n"
                    for rank, (group, t, signals) in enumerate(picked, start=1))
    _write(args.out, lines)

    summary = {
        "threads_considered": len(eval_threads),
        "threads_excluded_random_sample": len(eval_threads) - len(kept),
        "exclude_ids_unmatched": len(exclude - eval_ids),
        "threads_qualifying": len(candidates),
        "threads_no_signal": len(quiet),
        "threads_picked": len(picked),
        "picked_per_group": {GROUP_SIGNAL: len(picked_signal), GROUP_RANDOM: len(picked_random)},
        "picked_per_signal": {name: sum(1 for _, s in picked_signal if name in s)
                              for name in SIGNAL_NAMES},
        "n_signal": args.n_signal,
        "n_random": args.n_random,
        "seed": args.seed,
    }
    return json.dumps(summary, sort_keys=True, indent=2) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = _run(args)
        if args.summary_out:
            _write(args.summary_out, summary)
    except _Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if not args.summary_out:
        sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
