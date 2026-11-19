# support-triage-auto

A support-triage system at volume: for each customer-support conversation on X it decides to answer, ask, route or escalate to a person, and proves with evals how often it gets that right and at what cost.

**Status (18 Nov 2026):** phase 1, triage. The data loader, thread rebuild, split, keyword cheap tier, eval report with 95% ranges, policy checks, made-up regression suite, offline review page and hard-case picker are merged. The made-up suite has 63 cases; today's rule-based router passes 24 of them, and the must-pass cases it fails are the bar for the next issues. The product requirements are in [docs/prd.md](docs/prd.md).

## Design in brief

- Rebuild tweets into conversation threads.
- Cost-aware cascade: a cheap tier labels every thread; a larger model only for low-confidence or risky threads; a tool-using agent only for actionable ones.
- Deterministic policy checks outside the model, and a human review queue.
- Evals: a hand-labelled gold set, a calibrated LLM judge, routing accuracy, missed escalations, cost per 1,000 tweets, p95 latency, τ²-bench pass^k, and a failure taxonomy.

## How work gets built

Issues are built by the coding-agent factory in `.claude/skills/factory/` (from [software-factory-mvp](https://github.com/aadhar-build/software-factory-mvp)), in two lanes that run side by side:

1. **Clarify:** an issue labelled `factory` gets a spec, never code. If the spec raises a product question (a metric definition, a threshold, a policy, a reply rule), it goes on the issue with a recommended answer and the `needs-input` label. The owner answers there and removes the label.
2. **Build:** only an issue with no open product question is built, tests first, then checked, reviewed and opened as a PR for the owner.

Labels, thresholds, policy rules and the gold sets are owner files (`policies/`, `evals/`), and the factory never edits them.

## Running the baseline eval

The first triage slice is a rule and keyword cheap tier, a cascade router with fake larger-model and agent tiers, and an aggregate-only eval report. It makes no network or paid model calls.

```
PYTHONPATH=src python3 -m triage.run_eval --gold path/to/gold.csv --out report.json
```

The gold CSV has the columns `thread_id,intent,action,should_escalate`. By default it samples 200 threads from the `dev` split (`--sample-size`, `--seed`, `--split`, `--format md`). The default per-tier prices are **placeholders, not real prices**, and the report says so; pass `--price-cheap`, `--price-larger` and `--price-agent` to change them.

Add `--trace PATH` to also write an audit trace: one JSON line per routed thread with the git commit (and a `code_dirty` flag), the threshold, split and seed, each tier's model id and labels, the final labels and the reason codes (`cheap_confident`, `low_confidence_to_larger`, `high_risk_to_larger`). It holds conversation ids only, never tweet text, author ids or brand ids. The path must be outside the repository; the run exits 2 and writes nothing if any record is incomplete.

### Full metrics (`metrics.json`)

Add `--metrics-out PATH --cohort {gold_random,hard_case}` to also write the full metrics as one JSON file (schema `metrics/1`, from `triage.metrics`): escalation counts, rates and F1/F2/F0.5, MCC and balanced accuracy, plus confusion matrices, per-class metrics, macro and weighted averages, accuracy and kappa for action and intent. Every rate carries its n, a Wilson 95% interval and a `directional` mark below n = 30. `--cohort` is required with `--metrics-out`, so one file holds one gold set; the hard-case and random sets are never pooled. `--run-at` (ISO 8601 UTC ending in `Z`, default now) stamps the file. Gold-kind metrics hold no conversation, author or brand ids. `run_synthetic` always writes `DIR/metrics.json` (synthetic kind, with the case ids per confusion cell) and takes the same `--run-at`. `triage.metrics.comparison_key` says whether two files may be compared.

### Real larger tier (`--larger real`)

By default the larger tier is a fake stand-in (`--larger fake`), and nothing below is read or written. `--larger real` labels the threads the router sends to the larger tier with a real model through headless Claude Code on the subscription login (`claude -p`, `--claude-bin`), with no API key: API-key variables are stripped from its environment. The default model is `claude-haiku-5-5` (`--larger-model`). The instructions are the versioned prompt `src/triage/prompts/larger_tier.md` (rewrite it only on dev or synthetic data, one PR per version); the conversation goes on stdin, built only by `quoting.quote_conversation`.

Spend is in API-equivalent USD: the tokens each call reports times the list prices in the owner's price file (`--prices`, default `policies/model_prices.json`; the code holds no prices). Two hard caps apply. `--max-spend-usd` caps one run (default and maximum 2.50). The monthly cap (`guardrails.api_spend_usd_per_month.max` in `--thresholds`, default `evals/thresholds.json`) is enforced by a durable spend log outside the repository (`--spend-log`, default `~/.local/state/support-triage/spend-log.jsonl`). Start one with `--create-spend-log`; a missing log is never treated as an empty month. Before each call the worst-case cost is reserved under a lock, and a failed call or an unknown bill keeps the reservation charged and stops paid calls for the run. Bad output, failed calls and calls a cap would block fall back to the cheap tier's labels, counted by reason.

A real run uses the dev split. `--split eval` needs `--post-merge-eval` (one scoring run after a prompt merge, recorded in the report and the spend log), and `--split full` is always refused. Any refusal exits 2 before data is read. The report adds a "Model spend (API-equivalent USD)" section; the metrics file keeps its shape.

## Reviewing a synthetic run

After `python3 -m triage.run_synthetic --cases CASES.json --out DIR`, a person can review what the cascade decided for each made-up case:

```
PYTHONPATH=src python3 -m triage.review_page --cases CASES.json --run DIR --out review.html
PYTHONPATH=src python3 -m triage.apply_review --run DIR --decisions decisions.json --cases CASES.json [--rules RULES.json]
```

`review_page` writes one offline HTML file (no server, no network requests). Flagged cases (a policy violation or action `escalate`) come first. A case with a policy violation can only be edited or rejected, never approved. A case flagged only by `escalate` can be approved with a reason. The reviewer approves, edits, rejects with a reason or changes the action, then downloads `decisions.json`. `apply_review` checks it against the run and its cases file, runs the policy check (`--rules`, default `policies/rules.json`) on every edited draft and refuses any that still break a rule, merges it by case id into `DIR/review_applied.json` (re-applying is safe; a later file resumes the review) and prints the approved, edited, rejected and undecided counts and any changed actions. Both commands accept only a synthetic run, never real dataset threads.

## Per-brand rules

Each brand can keep its rules in `brands/<brand>/guidelines.md`: free Markdown prose plus exactly one fenced block opened by a line that is exactly ```` ```rules ````, holding a JSON object with the same keys as `policies/rules.json` (`public_reply_rules`, `escalate_always`, optional `repeat_contact`, `version`, `status`). Only that block is enforced. A missing, duplicated, empty or invalid block is refused (exit 2). `brands/amazonhelp/guidelines.md` starts with exactly today's `policies/rules.json`.

```
PYTHONPATH=src python3 -m triage.run_synthetic --cases CASES.json --out DIR --brand amazonhelp
PYTHONPATH=src python3 -m triage.run_eval --gold GOLD.csv --out REPORT --brand amazonhelp
PYTHONPATH=src python3 -m triage.brand_rules --conformance [--brand amazonhelp] [--rules policies/rules.json]
```

`--brand` and `--rules` can't be combined; without `--brand` the runners use `--rules` as before. Brand names must match `[a-z0-9][a-z0-9_-]{0,63}` and resolve inside `brands/` (`--brands-dir` is a test seam). Every run prints `brand=<name|none>`, the rules and topics loaded and `repeat_contact=on|off`. For `run_synthetic` and `apply_review`, `--brand` only picks the rules: every case still runs. For `run_eval`, `--brand` also picks the traffic: only threads attributed to the brand (a first-tweet @mention, or a brand reply made by the last inbound turn) are sampled, and stdout and the report give the included and excluded counts. `run_eval --brand` can't write `--metrics-out`. The conformance command exits 0 when the brand file's rules block equals the JSON rules file, 1 naming the differing top-level keys, 2 on a load error.

## Replaying a synthetic run

To see why the cascade handled each made-up conversation the way it did, run the synthetic cases with a trace (the path must be outside the repository), then build the replay page:

```
PYTHONPATH=src python3 -m triage.run_synthetic --cases CASES.json --out DIR --trace /tmp/synthetic-trace.jsonl
PYTHONPATH=src python3 -m triage.replay --cases CASES.json --run DIR --trace /tmp/synthetic-trace.jsonl --out replay.html
```

`run_synthetic --trace` writes one audit record per case (split `synthetic`, `thread_id` = case position) before any report, and adds ` trace_records=<n>` to its summary line; without `--trace` nothing changes. `replay` writes one offline HTML page (no server, no network requests): pick a thread, step through its turns with Next and Back, and see the triage state (intent, urgency, action, confidence, reason codes, deciding tier and model id, policy result, drafted or held reply) next to the thread's trace. Until per-turn re-triage (#46) lands, the one recorded decision is shown only at the last turn, labelled as decided once on the full thread. It refuses (exit 2, one line, no file) anything but a synthetic trace and run, and a cases file, run report or trace that don't belong together; edit the cases file in any way and you must re-run `run_synthetic` first.

## Performance report

To read one eval run's performance as a page, point the report at a `metrics/1` file (`run_eval --metrics-out` or `run_synthetic`'s `DIR/metrics.json`):

```
PYTHONPATH=src python3 -m triage.report DIR/metrics.json -o report.html
PYTHONPATH=src python3 -m triage.report this_week.json --compare last_week.json -o report.html
```

It writes one offline HTML page (no server, no network requests) and prints `page=<path>`. The page shows each headline rate with its value, "num of n", 95% range and a "directional" badge when the evidence is thin; the escalation detail; confusion matrices and sortable per-class tables for action and intent; and, with `--compare`, both runs side by side with the delta and a literal "ranges don't overlap" mark. Green and red come only from the targets in `evals/thresholds.json` (read, never written; `--thresholds PATH` points elsewhere); a missing key shows "target not set". Compare refuses only truly unlike runs (a different kind, cohort, split, gold revision or brand population); a different `run_at`, sample or config is fine. A gold-run page carries aggregates only; on a synthetic run, clicking a confusion cell lists its case ids. Any refusal exits 2 with one `error:` line and writes no file.

## Admin console (local, read-only)

A read-only web console over a folder of past runs. Start it by hand; it runs in the foreground only (never a service), and Ctrl-C stops it and exits 0:

```
PYTHONPATH=src python3 -m triage.admin --runs ~/triage-runs/
```

It binds only to `127.0.0.1` (default port 8765, `--port N` to change it, `--port 0` for a free one) and answers only requests whose `Host` is `127.0.0.1` or `localhost`. From your own machine, reach it over an SSH tunnel, then open `localhost:9000` in a browser:

```
ssh -L 9000:127.0.0.1:8765 <server>
```

Keep `--runs` outside git. Each run is one subfolder with a hand-written `manifest.json` (`{"schema_version": "run-manifest/1", "kind": "synthetic" | "gold_random" | "hard_case", "created_at": "...Z", "cost": {"estimated_usd": ..., "cost_per_1000_usd": ..., "prices_are_placeholders": ...}}`) and optionally a `metrics.json`; nothing writes manifests yet. Pages: Overview (latest run and estimated spend against the caps), Eval reports (run list and each run's drill-down report), Drift and incidents (names the commands only) and Policies and thresholds (rules and thresholds on disk next to each run's effective config). `--rules` and `--thresholds` point at other files; both are only read.

## Go console (`triage serve`)

The Go port of the admin console. It is read-only, GET-only and binds only to
`127.0.0.1`, in the foreground. The default port is 8770 (8765 is taken on the
VPS); `--port` overrides it. The same server serves the built web console.

```sh
cd web && npm ci && npm run build
cd ../backend && go run ./cmd/triage serve --runs ~/triage-runs/ --web ../web/dist
```

Default paths (`policies/rules.json`, `evals/thresholds.json`, `web/dist`) are
relative to the working directory, so run from the repo root or pass `--rules`,
`--thresholds` and `--web` explicitly. Ctrl-C stops it.

## Data and attribution

This project uses **Customer Support on Twitter** by thoughtvector, published on Kaggle:
https://www.kaggle.com/datasets/thoughtvector/customer-support-on-twitter

- The dataset is licensed **CC BY-NC-SA 4.0** (attribution, non-commercial, share-alike).
- The raw data is **not** in this repository. A download script fetches it locally into `data/raw/`, which is gitignored.
- Anything published here that is derived from the data (aggregates, quoted examples, gold labels, the taxonomy) is shared under **CC BY-NC-SA 4.0**, and is for non-commercial use only.
- Agent benchmarks use τ²-bench by Sierra Research (MIT).

## Licence

Code: MIT. See [LICENSE](LICENSE). Data-derived artefacts: CC BY-NC-SA 4.0, as above.
