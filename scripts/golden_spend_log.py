#!/usr/bin/env python3
"""Dump the Python reference behaviour of triage.model_prices and triage.spend_log
as goldens for the Go port (backend/internal/model, job 070).

Run by scripts/make_golden.sh from the repo root with PYTHONPATH=src. Standard
library only; imports triage.spend_log and triage.model_prices read-only. Takes
explicit --inputs and --out paths and refuses any that resolves under
<repo>/data/raw (exit 2, one stderr line). Needs Python 3.12 or later (exit 2).

uuid.uuid4 is patched with a counter ("%032x" % n, n = 1, 2, ...) and
spend_log._now with the step clock. Logs are written only in a tempfile dir
outside the repository; every message has the log path replaced by "<log>" and
then the repository root by "<repo>". Floats are repr(float(x)). Every line is
json.dumps(obj, sort_keys=True).

Files:
  prices.jsonl          {"file", "prices"} or {"file", "error", "message"} per price
                        file, then {"file", "model", "usage", "band", "cost"} per
                        usage for each loaded file (band/cost: value or
                        {"error", "message"})
  caps.jsonl            {"file", "value"} or {"file", "error", "message"}
  scenario_log.jsonl    the scripted sequence's final log bytes
  scenario_steps.jsonl  {"step", "op", "result" | "error"+"message", "mtd"}
  read.jsonl            {"log", "clock", "mtd" | "error"+"message"} per clock, then
                        {"log", "reserve": id | "error"+"message", "changed"}
"""

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 12):
    sys.stderr.write("golden_spend_log.py: needs Python 3.12 or later\n")
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"


def refuse_raw(flag, value):
    lit = Path(os.path.abspath(value))
    real = Path(os.path.realpath(value))
    for p in (lit, real):
        for raw in (RAW, Path(os.path.realpath(RAW))):
            if p == raw or raw in p.parents:
                sys.stderr.write("golden_spend_log.py: %s %s resolves under data/raw; "
                                 "refused\n" % (flag, value))
                sys.exit(2)


from triage import model_prices, spend_log  # noqa: E402


class _Hex:
    def __init__(self, h):
        self.hex = h


def _counter():
    n = [0]

    def uuid4():
        n[0] += 1
        return _Hex("%032x" % n[0])
    return uuid4


def dumps(obj):
    return json.dumps(obj, sort_keys=True) + "\n"


def r(x):
    return repr(float(x))


def err(e, log=None):
    msg = str(e)
    if log:
        msg = msg.replace(log, "<log>")
    msg = msg.replace(str(ROOT), "<repo>")
    return {"error": type(e).__name__, "message": msg}


def render_band(b):
    return {"max_input_tokens": b.max_input_tokens, "input": r(b.input), "output": r(b.output),
            "cache_write": None if b.cache_write is None else r(b.cache_write),
            "cache_read": None if b.cache_read is None else r(b.cache_read)}


class Usage:
    def __init__(self, a, b, c, d):
        self.input_tokens, self.output_tokens = a, b
        self.cache_write_tokens, self.cache_read_tokens = c, d


def prices(inputs):
    files = ["%s/prices/%s" % (inputs, n) for n in sorted(os.listdir(os.path.join(inputs, "prices")))]
    files += ["tests/fixtures/model_prices.json", "policies/model_prices.json"]
    with open(os.path.join(inputs, "usages.json"), encoding="utf-8") as f:
        usages = json.load(f)["usages"]
    out = []
    for path in files:
        try:
            p = model_prices.load_prices(path)
        except (model_prices.PricesError, model_prices.UnknownBillError) as e:
            out.append(dumps(dict(err(e), file=path)))
            continue
        out.append(dumps({"file": path, "prices": {
            "version": p.version,
            "models": [[m, [render_band(b) for b in p.models[m]]] for m in sorted(p.models)]}}))
        for u in usages:
            usage = Usage(*u["usage"])
            rec = {"file": path, "model": u["model"], "usage": u["usage"]}
            total = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
            try:
                rec["band"] = render_band(p.band(u["model"], total))
            except model_prices.UnknownBillError as e:
                rec["band"] = err(e)
            try:
                rec["cost"] = r(p.cost(u["model"], usage))
            except model_prices.UnknownBillError as e:
                rec["cost"] = err(e)
            out.append(dumps(rec))
    return "".join(out)


def caps(inputs):
    files = ["%s/thresholds/%s" % (inputs, n) for n in sorted(os.listdir(os.path.join(inputs, "thresholds")))]
    files += ["evals/thresholds.json", "tests/fixtures/thresholds_spend.json"]
    out = []
    for path in files:
        try:
            out.append(dumps({"file": path, "value": r(spend_log.load_monthly_cap(path))}))
        except spend_log.SpendLogError as e:
            out.append(dumps(dict(err(e), file=path)))
    return "".join(out)


def scenario(inputs, tmp):
    with open(os.path.join(inputs, "scenario.json"), encoding="utf-8") as f:
        spec = json.load(f)
    log = os.path.join(tmp, "scenario.jsonl")
    cap = float(spec["cap"])
    uuid.uuid4 = _counter()
    results, out = [], []
    for i, st in enumerate(spec["steps"]):
        clock = datetime.fromisoformat(st["clock"])
        spend_log._now = lambda c=clock: c
        sl = spend_log.SpendLog(log, cap, clock=lambda c=clock: c)
        rid = results[st["ref"]] if "ref" in st else st.get("id")
        rec = {"step": i, "op": st["op"]}
        try:
            if st["op"] == "create":
                spend_log.SpendLog.create(log)
                res = None
            elif st["op"] == "reserve":
                res = sl.reserve(st["run_id"], st["model"], st["split"], float(st["usd"]))
            elif st["op"] == "settle":
                res = sl.settle(rid, float(st["usd"]), st["tokens"])
            elif st["op"] == "mark_unknown":
                res = sl.mark_unknown(rid)
            else:
                res = r(sl.month_to_date())
            rec["result"] = res
        except spend_log.SpendLogError as e:
            res = None
            rec.update(err(e, log))
        results.append(res)
        try:
            rec["mtd"] = r(sl.month_to_date())
        except spend_log.SpendLogError as e:
            rec["mtd"] = err(e, log)
        out.append(dumps(rec))
    with open(log, "rb") as f:
        return f.read(), "".join(out)


def read(inputs, tmp):
    with open(os.path.join(inputs, "read_clocks.json"), encoding="utf-8") as f:
        spec = json.load(f)
    rv = spec["reserve"]
    out = []
    logs_dir = os.path.join(inputs, "logs")
    for name in sorted(os.listdir(logs_dir)):
        log = os.path.join(tmp, "read.jsonl")
        shutil.copyfile(os.path.join(logs_dir, name), log)
        uuid.uuid4 = _counter()
        for c in spec["clocks"]:
            clock = datetime.fromisoformat(c)
            sl = spend_log.SpendLog(log, float(spec["cap"]), clock=lambda c=clock: c)
            rec = {"log": name, "clock": c}
            try:
                rec["mtd"] = r(sl.month_to_date())
            except spend_log.SpendLogError as e:
                rec.update(err(e, log))
            out.append(dumps(rec))
        clock = datetime.fromisoformat(spec["clocks"][0])
        sl = spend_log.SpendLog(log, float(spec["cap"]), clock=lambda c=clock: c)
        with open(log, "rb") as f:
            before = f.read()
        rec = {"log": name}
        try:
            rec["reserve"] = sl.reserve(rv["run_id"], rv["model"], rv["split"], float(rv["usd"]))
        except spend_log.SpendLogError as e:
            rec["reserve"] = err(e, log)
        with open(log, "rb") as f:
            rec["changed"] = f.read() != before
        out.append(dumps(rec))
        os.remove(log)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    refuse_raw("--inputs", a.inputs)
    refuse_raw("--out", a.out)
    os.makedirs(a.out, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="golden_spend_log.") as tmp:
        tmp = os.path.realpath(tmp)
        if tmp == str(ROOT) or tmp.startswith(str(ROOT) + "/"):
            sys.stderr.write("golden_spend_log.py: temp dir is inside the repository\n")
            sys.exit(2)
        files = {"prices.jsonl": prices(a.inputs), "caps.jsonl": caps(a.inputs)}
        log_bytes, steps = scenario(a.inputs, tmp)
        files["scenario_steps.jsonl"] = steps
        files["read.jsonl"] = read(a.inputs, tmp)
    for name, text in files.items():
        with open(os.path.join(a.out, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    with open(os.path.join(a.out, "scenario_log.jsonl"), "wb") as f:
        f.write(log_bytes)


if __name__ == "__main__":
    main()
