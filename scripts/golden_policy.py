#!/usr/bin/env python3
"""Dump the Python reference behaviour of triage.policy_check and triage.quoting
as policy goldens for the Go port (backend/internal/policy).

Run by scripts/make_golden.sh from the repo root, so every source path in the
output is repo-relative. Standard library only. Takes explicit input paths and
refuses any that resolves under <repo>/data/raw (exit 2, one stderr line)
before opening anything. Never prints the --cases or --twcs path.

Every JSONL line is json.dumps(obj, ensure_ascii=True, sort_keys=True,
separators=(",", ":")). Line schemas:
  loaders.jsonl        {"kind": "file"|"data", "source", "rules", "topics"
                        [, "rules_version" for files]}; each of those is
                        {"value": ...} or {"error": "<RulesError message>"}, with
                        the read/JSON detail replaced by "(<detail>)"
  check_synthetic      {"case", "source", "violations": [[id, start, end, text]], "safe"}
  topics_synthetic     {"case", "source", "topics": [...]}
  check_threads        {"root", "customer_turns", "drafts": [{"draft", "violations", "safe"}]}
  topics_threads       {"root", "customer_turns", "topics"}
  quote_*              {"case"|"root", "turns": [[inbound, text]], "quoted"|"error"}
  check_edge/topics_edge as the synthetic ones, keyed by "case", without "source"
Customer turns are all inbound turns of a case or thread, in order.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"


def refuse_raw(flag, value):
    lit = Path(os.path.abspath(value))
    real = Path(os.path.realpath(value))
    for p in (lit, real):
        for raw in (RAW, Path(os.path.realpath(RAW))):
            if p == raw or raw in p.parents:
                sys.stderr.write("golden_policy.py: %s %s resolves under data/raw; refused\n"
                                 % (flag, value))
                sys.exit(2)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_lines(path, objs):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for obj in objs:
            f.write(dumps(obj) + "\n")


DETAIL = re.compile(r"(: (?:cannot read rules file|not valid JSON) )\(.*\Z", re.S)


def error_text(exc):
    return DETAIL.sub(r"\1(<detail>)", str(exc))


def attempt(fn, *args):
    from triage.policy_check import RulesError
    try:
        return {"value": fn(*args)}, True
    except RulesError as exc:
        return {"error": error_text(exc)}, False


def rule_obj(r):
    return {"id": r.id, "type": r.type, "phrases": list(r.phrases), "detect": list(r.detect)}


def topic_obj(t):
    return {"id": t.id, "phrases": list(t.phrases)}


def violations(draft, turns, rules):
    from triage import policy_check as pc
    found = pc.check(draft, turns, rules)
    return ([[v.rule_id, v.start, v.end, v.text] for v in found],
            pc.is_safe_to_send(draft, turns, rules))


def quote(turns):
    from triage.quoting import quote_conversation
    thread = SimpleNamespace(turns=[SimpleNamespace(inbound=a, text=b) for a, b in turns])
    try:
        return {"quoted": quote_conversation(thread)}
    except ValueError as exc:
        return {"error": str(exc)}


def unicode_table():
    import _sre
    from re._casefix import _EXTRA_CASES
    lines = []
    for name, pred in (("isalnum", str.isalnum), ("isspace", str.isspace)):
        start = None
        for c in range(0x110001):
            ok = c <= 0x10FFFF and pred(chr(c))
            if ok and start is None:
                start = c
            elif not ok and start is not None:
                lines.append("%s %04x %04x" % (name, start, c - 1))
                start = None
    groups = {}
    for c in range(0x110000):
        groups.setdefault(_sre.unicode_tolower(c), []).append(c)
    for p in range(0x110000):
        if not _sre.unicode_iscased(p):
            continue
        lo = _sre.unicode_tolower(p)
        row = set(groups.get(lo, ()))
        for extra in _EXTRA_CASES.get(lo, ()):
            row.update(groups.get(extra, ()))
        pat = re.compile(re.escape(chr(p)), re.IGNORECASE)
        for c in row:
            if not pat.fullmatch(chr(c)):
                raise SystemExit("golden_policy.py: case row %04x: %04x does not match" % (p, c))
        lines.append("case %04x %s" % (p, " ".join("%04x" % c for c in sorted(row))))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--twcs", required=True)
    ap.add_argument("--rules", action="append", default=[], help="rules file source, in order")
    ap.add_argument("--brand", action="append", default=[], help="brand guidelines .md")
    ap.add_argument("--edge-rules", required=True)
    ap.add_argument("--edge-cases", required=True)
    args = ap.parse_args(argv)
    for flag in ("out", "cases", "twcs", "edge_rules", "edge_cases"):
        refuse_raw("--" + flag.replace("_", "-"), getattr(args, flag))
    for flag in ("rules", "brand"):
        for value in getattr(args, flag):
            refuse_raw("--" + flag, value)

    sys.path.insert(0, str(ROOT / "src"))
    from triage import policy_check as pc
    from triage.brand_rules import _brand_object
    from triage.threads import build_threads

    out = Path(args.out)
    (out / "rules_data").mkdir(parents=True, exist_ok=True)

    # Loaders: rules files, then the rules blocks of the brand files.
    loader_lines, rule_sets, topic_sets = [], [], []
    for src in args.rules:
        rules, rok = attempt(pc.load_rules, src)
        topics, tok = attempt(pc.load_escalate_topics, src)
        version, _ = attempt(pc.rules_version, src)
        line = {"kind": "file", "source": src, "rules_version": version}
        source = src
        loader_lines.append((line, rules, rok, topics, tok, source))
    for src in args.brand:
        _, data = _brand_object(src)
        name = Path(src).parent.name if Path(src).name == "guidelines.md" else Path(src).stem
        with open(out / "rules_data" / (name + ".json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=True, indent=2) + "\n")
        rules, rok = attempt(pc.rules_from_data, data, src)
        topics, tok = attempt(pc.topics_from_data, data, src)
        loader_lines.append(({"kind": "data", "source": src}, rules, rok, topics, tok, src))
    rendered = []
    for line, rules, rok, topics, tok, source in loader_lines:
        if rok:
            rule_sets.append((source, rules["value"]))
            rules = {"value": [rule_obj(r) for r in rules["value"]]}
        if tok:
            topic_sets.append((source, topics["value"]))
            topics = {"value": [topic_obj(t) for t in topics["value"]]}
        rendered.append(dict(line, rules=rules, topics=topics))
    write_lines(out / "loaders.jsonl", rendered)

    # Synthetic cases, against every source that loads.
    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    check_lines, topic_lines, quote_lines = [], [], []
    for case in cases:
        inbound = [t["text"] for t in case["turns"] if t["inbound"]]
        if "draft_reply" in case:
            for source, rules in rule_sets:
                found, safe = violations(case["draft_reply"], inbound, rules)
                check_lines.append({"case": case["id"], "source": source,
                                    "violations": found, "safe": safe})
        for source, topics in topic_sets:
            topic_lines.append({"case": case["id"], "source": source,
                                "topics": list(pc.matched_topics(inbound, topics))})
        turns = [[t["inbound"], t["text"]] for t in case["turns"]]
        quote_lines.append(dict(quote(turns), case=case["id"], turns=turns))
    write_lines(out / "check_synthetic.jsonl", check_lines)
    write_lines(out / "topics_synthetic.jsonl", topic_lines)
    write_lines(out / "quote_synthetic.jsonl", quote_lines)

    # Generated twcs threads, under policies/rules.json.
    live_rules = pc.load_rules("policies/rules.json")
    live_topics = pc.load_escalate_topics("policies/rules.json")
    threads, _ = build_threads(args.twcs)
    check_lines, topic_lines, quote_lines = [], [], []
    for th in threads:
        inbound = [t.text for t in th.turns if t.inbound]
        drafts = []
        for t in th.turns:
            if not t.inbound:
                found, safe = violations(t.text, inbound, live_rules)
                drafts.append({"draft": t.text, "violations": found, "safe": safe})
        check_lines.append({"root": th.root_tweet_id, "customer_turns": inbound, "drafts": drafts})
        topic_lines.append({"root": th.root_tweet_id, "customer_turns": inbound,
                            "topics": list(pc.matched_topics(inbound, live_topics))})
        turns = [[t.inbound, t.text] for t in th.turns]
        quote_lines.append(dict(quote(turns), root=th.root_tweet_id, turns=turns))
    write_lines(out / "check_threads.jsonl", check_lines)
    write_lines(out / "topics_threads.jsonl", topic_lines)
    write_lines(out / "quote_threads.jsonl", quote_lines)

    # Hand-made edge cases.
    edge_rules = pc.load_rules(args.edge_rules)
    edge_topics = pc.load_escalate_topics(args.edge_rules)
    with open(args.edge_cases, encoding="utf-8") as f:
        edge = json.load(f)
    lines = []
    for c in edge["check"]:
        found, safe = violations(c["draft"], c["customer_turns"], edge_rules)
        lines.append({"case": c["id"], "violations": found, "safe": safe})
    write_lines(out / "check_edge.jsonl", lines)
    write_lines(out / "topics_edge.jsonl", [
        {"case": c["id"], "topics": list(pc.matched_topics(c["customer_turns"], edge_topics))}
        for c in edge["topics"]])
    lines = []
    for c in edge["quote"]:
        turns = [[t["inbound"], t["text"]] for t in c["turns"]]
        lines.append(dict(quote(turns), case=c["id"], turns=turns))
    write_lines(out / "quote_edge.jsonl", lines)

    with open(out / "unicode.txt", "w", encoding="utf-8", newline="\n") as f:
        f.write(unicode_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
