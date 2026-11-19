#!/usr/bin/env python3
"""Dump the Python reference behaviour of triage.context_signals as goldens for
the Go port (backend/internal/policy, job 087).

Run by scripts/make_golden.sh from the repo root, so every loader source in the
output is repo-relative. Standard library only. Takes explicit input paths and
refuses any that resolves under <repo>/data/raw (exit 2, one stderr line)
before opening anything. Never prints the --twcs path.

Customer ids never leave this script: every customer is written as a per-run
ordinal "c0", "c1", ... in order of first appearance in thread build order.
Times are ISO 8601 UTC with "Z".

Every JSONL line is json.dumps(obj, ensure_ascii=True, sort_keys=True,
separators=(",", ":")). Files and line schemas:
  records.jsonl      {"root", "record": null | {"customer", "start",
                      "brands_at_start": [sorted], "brand_replies": [[brand, time]]}}
                     one per thread, in build order
  counts.jsonl       {"window_seconds", "root", "count"} for windows 0, 72h and
                     7 days, each window's lines sorted by root
  attribution.jsonl  {"root", "attribution": {brand: label}} per thread, for
                     every known brand (all threads' brand_ids) plus NoSuchBrand
  loaders.jsonl      {"source", "value": {rule fields}} or {"source", "error"},
                     with the read detail replaced by "(<detail>)"
  lower.txt          "<cp hex> <lowered cp hex>..." for every scalar value whose
                     str.lower() is not itself
  lower_cases.jsonl  {"in", "lower"} for made-up contextual strings
"""

import argparse
import json
import os
import re
import sys
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

WINDOWS = (timedelta(0), timedelta(hours=72), timedelta(days=7))
LOWER_CASES = ["İBM", "ΑΣ", "Σ", "ΑΣΑ", "ΑΣ.", "Α.Σ", "ΑΣ́", "ΆΣ", "ΑΣ Β",
               "ΣΑ", "Α'Σ", "ΑΣ'Α", "ShopCo", "SHOPCO", "ǅ", "ẞ", "K"]


def refuse_raw(flag, value):
    lit = Path(os.path.abspath(value))
    real = Path(os.path.realpath(value))
    for p in (lit, real):
        for raw in (RAW, Path(os.path.realpath(RAW))):
            if p == raw or raw in p.parents:
                sys.stderr.write("golden_context_signals.py: %s %s resolves under data/raw; "
                                 "refused\n" % (flag, value))
                sys.exit(2)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_lines(path, objs):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for obj in objs:
            f.write(dumps(obj) + "\n")


DETAIL = re.compile(r"(: cannot read file )\(.*\Z", re.S)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def lower_table():
    lines = []
    for c in range(0x110000):
        if 0xD800 <= c <= 0xDFFF:
            continue
        low = chr(c).lower()
        if low != chr(c):
            lines.append("%04x %s" % (c, " ".join("%04x" % ord(x) for x in low)))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--twcs", required=True)
    ap.add_argument("--rules", action="append", default=[], help="rules file source, in order")
    args = ap.parse_args(argv)
    refuse_raw("--out", args.out)
    refuse_raw("--twcs", args.twcs)
    for value in args.rules:
        refuse_raw("--rules", value)

    sys.path.insert(0, str(ROOT / "src"))
    from triage import context_signals as cs
    from triage.threads import build_threads

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    loaders = []
    for src in args.rules:
        try:
            r = cs.load_repeat_contact_rule(Path(src))
            loaders.append({"source": src, "value": {
                "window_days": r.window_days, "min_conversations": r.min_conversations,
                "effect": r.effect, "report_window_hours": r.report_window_hours}})
        except cs.RepeatContactError as exc:
            loaders.append({"source": src, "error": DETAIL.sub(r"\1(<detail>)", str(exc))})
    write_lines(out / "loaders.jsonl", loaders)

    threads, _ = build_threads(args.twcs)
    known = set()
    for th in threads:
        known.update(th.brand_ids)
    ordinals = {}
    records, record_lines, attribution_lines = [], [], []
    for th in threads:
        r = cs.contact_record(th, known)
        if r is None:
            record_lines.append({"root": th.root_tweet_id, "record": None})
        else:
            records.append(r)
            name = ordinals.setdefault(r.customer, "c%d" % len(ordinals))
            record_lines.append({"root": th.root_tweet_id, "record": {
                "customer": name, "start": iso(r.start),
                "brands_at_start": sorted(r.brands_at_start),
                "brand_replies": [[b, iso(at)] for b, at in r.brand_replies]}})
        attribution_lines.append({"root": th.root_tweet_id, "attribution": {
            b: cs.brand_attribution(th, b, known) for b in sorted(known) + ["NoSuchBrand"]}})
    write_lines(out / "records.jsonl", record_lines)
    write_lines(out / "attribution.jsonl", attribution_lines)

    count_lines = []
    for w in WINDOWS:
        counts = cs.contact_counts(records, w)
        for root in sorted(counts):
            count_lines.append({"window_seconds": int(w.total_seconds()), "root": root,
                                "count": counts[root]})
    write_lines(out / "counts.jsonl", count_lines)

    with open(out / "lower.txt", "w", encoding="utf-8", newline="\n") as f:
        f.write(lower_table())
    write_lines(out / "lower_cases.jsonl", [{"in": s, "lower": s.lower()} for s in LOWER_CASES])
    return 0


if __name__ == "__main__":
    sys.exit(main())
