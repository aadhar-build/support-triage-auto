# Support triage PRD (repo copy)

The master copy is the owner's Notion page. This file is the copy agents read: when a spec needs a metric definition, a target or a guardrail, it comes from here. Owner decisions change it through a PR, never silently. Last synced 18 Nov 2026 (PRD v1.0, approved by Aadhar on 18 Nov, plus the decisions of the same day).

## What we're building

A triage layer for public customer support on X. It reads every incoming conversation and decides whether to **answer**, **ask** for details, **route** to a team or **escalate** to a person. It drafts a reply when one is safe. A cheap tier handles the bulk, and a larger model only sees what the cheap tier is unsure about. Anything that commits the brand goes to a person in a review queue. Every decision is measured against labels the owner keeps, with the cost of each tier on the same page.

Data: Customer Support on Twitter (Kaggle). 2.81M tweets, 787,346 conversations, 108 brands, 2008 to 2017, split 10/5/85 (dev/eval/full) by conversation.

## Users

- **Primary:** a front-line agent on a brand's social-care team, who works the review queue.
- **Secondary:** the team lead (admin), who sets policy, thresholds and budget, and reads the eval report.
- **Portfolio:** a hiring manager, who should see the trade-offs and the evals in under 3 minutes.

## Non-goals

- No live integration with X or a helpdesk; it runs offline on the dataset.
- No reply is ever posted. Drafts only, approved by a person.
- No refunds, compensation or account changes by the system, ever.
- Not multilingual in v1. Non-English conversations are detected and routed, not answered.
- No fine-tuning or model training.
- DMs are out of scope for now (#31 is parked).

## North star

**Correctly handled without a person:** the share of scored eval conversations where the system's action equals the gold action and the gold action is not `escalate`. A wrong "answer" doesn't count.

- Target for the 6 Dec demo: **60% or more**.
- Baseline: the keyword cheap tier (pending the owner's gold review).

## Supporting metrics

| Metric | Definition | Target |
|---|---|---|
| Action accuracy | System action = gold action, overall and per action | 80% or more overall; no action under 60% |
| Intent accuracy | System intent = gold intent | 75% or more |
| Share needing a person | Conversations the system escalates | Report it; expect 10% to 15% (gold pre-labels: 37 of 300) |
| Tier shares | Share reaching the larger model, and the agent | Larger model 40% or less |
| Cost per 1,000 conversations | Real prices, all tiers | $5 or less |
| p95 latency per conversation | Wall time, all tiers | 5 s or less |
| Judge agreement | LLM judge vs the owner's labels | 85% or more, with disagreements listed |
| Reply quality | Judge preference for the draft vs the brand's real reply | Tie or win in 50% or more |

Reports also give, for escalation (positive = should escalate): TP, FP, TN, FN, TPR (recall), TNR (specificity), precision, NPV, F1, **F2 (the headline, because a miss costs more than a false alarm)**, F0.5, MCC and balanced accuracy. For action and intent, they give confusion matrices and per-class TPR, TNR, precision, F1, F2 and F0.5, plus macro and weighted averages and Cohen's kappa (#25).

## Guardrails (a breach blocks the demo and the full run)

| Guardrail | Limit | How measured |
|---|---|---|
| Missed escalations | 0 on the hard-case set, **and** the upper bound of the 95% interval under 5% on all gold escalations | Gold should_escalate = true, system did not escalate |
| False escalations | 20% or less of non-escalation conversations | Gold should_escalate = false, system escalated |
| Personal data repeated in a public draft | 0 | Policy check, plus a judge on drafts |
| Out-of-policy promises (refunds, compensation) | 0 | Policy check, plus a judge on drafts |
| Eval leakage | 0 eval conversations in prompts, few-shot examples or threshold tuning | Code review, and the factory's eval reviewer |
| API spend | $200 a month (API-equivalent dollars while on the subscription) | Monthly spend log checked before every call |

## How every number is reported

- Every rate is shown with its count (for example, 12 of 37) and a 95% Wilson interval, including shares (#14).
- A rate with n under 30 is marked directional.
- Runs are compared week on week, so runs over different weeks or samples can be compared, each shown with its range. Truly unlike runs (a different run kind, gold version, split or brand population) are refused. When ranges are disjoint, the report says "ranges don't overlap", never "significant".
- Gold-run reports are aggregate only: no conversation text or ids. Drill-down to single conversations works on the made-up (synthetic) set only.

## Eval sets

- **Gold random set:** 300 random eval conversations, reviewed by the owner.
- **Hard-case set:** about 100 eval conversations, about 70 picked by escalation signal and about 30 random with no signal, reported separately (#18).
- **Synthetic set:** `evals/gold/synthetic.json`, 63 made-up conversations, 22 of them must-pass. Baseline on 18 Nov: 24 of 63 pass; 20 of 22 must-pass fail.

## Release criteria for the demo

1. The owner's gold review is done, and the hard-case set is labelled.
2. Every guardrail holds on the eval split.
3. The north star and its interval are reported against the keyword baseline, whatever they show.
4. Cost per 1,000 conversations uses real prices.
5. The prototype shows one conversation end to end: tiers, policy check, review queue, trace.

## Phases

**Phase 1: triage (now).** Larger-model tier (#10), drift (#20), incident surges (#21), repeat contact (#22), injection guard (#23), audit trail (#24), metrics and drill-down reports (#25, #26), admin console and simulated views (#27, #28), policy editor (#29), always-escalate topics (#37), per-brand rules (#38).

**Phase 2: replies.** Reply rules from per-brand guidelines, starting with AmazonHelp (the largest brand by threads, 81,347, which moves only 0.6% of replies to DM). Objective: **increase containment without increasing repeat contacts or decreasing CSAT.**
- North star: containment rate. Handled without a person, correct, and no repeat contact within 7 days (72 hours reported alongside). Moving the customer to DM does not count.
- Guardrails: the repeat-contact rate must not rise. The CSAT proxy must not fall: the data has no CSAT, so the proxy is the customer's next message after the reply, validated at 80% or more agreement on 100 owner-labelled messages, and shown as unvalidated until then. False containment must be 0 on must-pass cases.
- Reply scorecard: #30.

**Later phase.** Many brands as a multi-tenant system.

## Production scenarios (P0, phase 1)

| Scenario | What we build | How it's measured |
|---|---|---|
| Drift over time | Weekly drift monitor on a time-ordered replay (intent mix, confidence, escalation share, share of "other"); a breach raises a call-out with a labelling sample | Planted shifts detected; false alarms per month on stable periods; weeks from shift to call-out |
| Incident surges | Spike detection per brand and topic; one incident, one status reply for a person to approve | Incidents found; drafts avoided; time from first message to incident |
| Context across turns and contacts | Follow-up and repeat-contact signals that raise urgency or escalate under an owner rule (suggested: 3 or more contacts in 7 days) | Missed escalations on repeat-contact cases |
| Manipulation and prompt injection | Customer text always passed as quoted data; injection detector flags for a person; policy checks run last | 100% of made-up injection cases flagged or blocked; 0 policy violations in drafts |
| Audit trail and versions | Every decision records code, policy, threshold, tier and model versions plus a reason code | Every decision has a complete record; reports split by version |

## Owner-owned files

Labels, thresholds, policy rules, taxonomy and guidelines belong to the owner. The factory never edits them:
- `policies/rules.json` (moving to `brands/<brand>/guidelines.md`, #38)
- `evals/gold/`
- `docs/labelling-guide.md`
- thresholds, drift and injection files as they are added

## Surfaces for the demo

- **Admin / team lead only** (owner decision, 18 Nov): a real local console, run on demand and not always on (#27, #29), with the drill-down reports (#26). Selecting a conversation replays it turn by turn with its traces and the triage state at each turn (#28). The simulated customer and agent views are dropped.

## Stack and migration (owner decision, 19 Nov; DRAFT pending Aadhar's approval)

- **Backend in Go** (language choice pending Aadhar's confirmation; Rust only for a measured hot path). **Frontend in TypeScript.** Python is avoided for new work.
- **Nothing working is thrown away first.** Today's Python code (about 8,500 lines, 32 modules, 727 tests) becomes the reference implementation. Each Go port must match its Python module's output exactly on the synthetic set and generated data (golden files) before the Python module is deleted.
- **One backend, one API.** A Go service and CLIs (`backend/`) expose JSON over a local HTTP API. A TypeScript admin console (`web/`) is the only UI: reports, drift, incidents, policies, and conversation-and-state replay with traces. The current HTML generators retire once the console has parity.
- **Port order** (each step is one or more factory issues):
  1. Ingestion, threads and splits, with streaming and a disk-backed thread index. This also fixes Astra's full-data memory finding.
  2. Rules, policy checks, injection detection and quoting, then the router with its tiers.
  3. Metrics, `run_synthetic` and `run_eval`, and the audit trace.
  4. Spend log, price file and the paid tier (headless Claude Code on the subscription).
  5. Drift, incidents and repeat contacts (with per-turn re-triage, #46).
  6. The Go HTTP API and the TypeScript admin console, at parity with the replay, report, admin and review pages.
  7. The policy proposer (#29), the hard-case picker and review tools. Then Python is removed.
- **Rules that change:** "standard library only" becomes "Go standard library first. Any third-party Go module or npm package needs Aadhar's approval and a pinned version." Tests stay offline, and use fixtures only.
- **Demo on 6 Dec:** run on the current Python backend. Steps 1 and 6 (ingestion, plus the TypeScript console reading today's run files) are the first targets, so the demo shows the new console. The full port continues after the demo.
- **The factory repo** (software-factory-mvp) keeps its Python orchestration scripts for now. They are tooling, not product code.

## Decision timing (owner decision, 18 Nov)

- No look-ahead. Every decision, alert and count uses only what was known at that moment: no later turns and no later brand replies. After-the-fact replays are not allowed. Where zero look-ahead can't be reached, live monitoring becomes more urgent.
- Triage is re-run after every new turn, and each decision is kept as a step in the thread's history (#46).

## Testing rules (owner decision, 18 Nov)

- Unit tests never read the live owner files. They use made-up fixtures with the same shape. An optional conformance check, run only when switched on, validates the live files.
- Scale limits on the VPS (first guesses, revised after the first measured run): 200k generated rows within 60 seconds and 1 GB of memory, and the full dataset within 20 minutes and 6 GB.
