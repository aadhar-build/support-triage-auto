# support-triage-auto

A support-triage system at volume: for each customer-support conversation on X it decides to answer, ask, route or escalate to a person, and proves with evals how often it gets that right and at what cost.

**Status:** starting. Week 1 (to 22 Nov 2026): load the data, rebuild threads, discover the taxonomy, label a gold set.

## Design in brief

- Rebuild tweets into conversation threads.
- Cost-aware cascade: a cheap tier labels every thread; a larger model only for low-confidence or risky threads; a tool-using agent only for actionable ones.
- Deterministic policy checks outside the model, and a human review queue.
- Evals: a hand-labelled gold set, a calibrated LLM judge, routing accuracy, missed escalations, cost per 1,000 tweets, p95 latency, τ²-bench pass^k, and a failure taxonomy.

## Running the baseline eval

The first triage slice is a rule and keyword cheap tier, a cascade router with fake larger-model and agent tiers, and an aggregate-only eval report. It makes no network or paid model calls.

```
PYTHONPATH=src python3 -m triage.run_eval --gold path/to/gold.csv --out report.json
```

The gold CSV has the columns `thread_id,intent,action,should_escalate`. By default it samples 200 threads from the `dev` split (`--sample-size`, `--seed`, `--split`, `--format md`). The default per-tier prices are **placeholders, not real prices**, and the report says so; pass `--price-cheap`, `--price-larger` and `--price-agent` to change them.

## Data and attribution

This project uses **Customer Support on Twitter** by thoughtvector, published on Kaggle:
https://www.kaggle.com/datasets/thoughtvector/customer-support-on-twitter

- The dataset is licensed **CC BY-NC-SA 4.0** (attribution, non-commercial, share-alike).
- The raw data is **not** in this repository. A download script fetches it locally into `data/raw/`, which is gitignored.
- Anything published here that is derived from the data (aggregates, quoted examples, gold labels, the taxonomy) is shared under **CC BY-NC-SA 4.0**, and is for non-commercial use only.
- Agent benchmarks use τ²-bench by Sierra Research (MIT).

## Licence

Code: MIT. See [LICENSE](LICENSE). Data-derived artefacts: CC BY-NC-SA 4.0, as above.
