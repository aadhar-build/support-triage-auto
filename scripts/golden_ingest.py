#!/usr/bin/env python3
"""Reference dumper for the ingest parity goldens (job 066). Reference tooling only.

Usage: golden_ingest.py --data CSV --split-config JSON --out-prefix PATH

Writes PATH.threads.jsonl (one canonical line per thread, in emission order) and
PATH.stats.json (load, thread and split stats) from the unchanged Python modules
triage.threads and triage.split. Any input that resolves under <repo>/data/raw is
refused with exit 2 before anything is opened or written. Deleted together with
loader.py, threads.py and split.py.
"""

import argparse
import hashlib
import json
import os
import sys

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def refuse_raw(flag, path):
    raw_lit = os.path.join(REPO, "data", "raw")
    raw_real = os.path.realpath(raw_lit)
    p = os.path.realpath(path)
    for raw in (raw_lit, raw_real):
        if p == raw or p.startswith(raw + os.sep):
            sys.stderr.write("golden_ingest.py: %s %s resolves under data/raw; refused\n" % (flag, path))
            sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--split-config", required=True)
    ap.add_argument("--out-prefix", required=True)
    args = ap.parse_args()
    refuse_raw("--data", args.data)
    refuse_raw("--split-config", args.split_config)

    sys.path.insert(0, os.path.join(REPO, "src"))
    from triage.split import load_split_config, split_threads
    from triage.threads import build_threads

    threads, stats = build_threads(args.data)
    cfg = load_split_config(args.split_config)
    mapping, report = split_threads(threads, cfg)

    lines = []
    for th in threads:
        obj = {
            "brand_ids": list(th.brand_ids),
            "brand_replied": th.brand_replied,
            "root_tweet_id": str(th.root_tweet_id),
            "split": mapping[th.root_tweet_id],
            "turns": [{
                "author_id": t.author_id,
                "created_at": t.created_at.isoformat(),
                "inbound": t.inbound,
                "text_sha256": hashlib.sha256(t.text.encode("utf-8")).hexdigest(),
                "tweet_id": str(t.tweet_id),
            } for t in th.turns],
        }
        lines.append(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")

    load = stats.load
    out = {
        "load": {
            "rows_read": load.rows_read,
            "rows_parsed": load.rows_parsed,
            "rows_skipped": load.rows_skipped,
            "skip_reasons": dict(load.skip_reasons),
        },
        "threads": {name: getattr(stats, name) for name in (
            "tweets_seen", "duplicate_ids", "threads_built", "tweets_placed", "tweets_not_placed",
            "broken_links", "cycles", "cycle_tweets", "brand_started",
            "parent_link_disagreements", "response_id_fallbacks")},
        "split": {
            "seed": report.seed,
            "total": report.total,
            "counts": report.counts,
            "shares": report.shares,
            "targets": report.targets,
        },
    }
    with open(args.out_prefix + ".threads.jsonl", "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    with open(args.out_prefix + ".stats.json", "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(out, sort_keys=True, indent=2, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
