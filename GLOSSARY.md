# Glossary

## Support triage

- **Thread**: one customer conversation, rebuilt from tweets by reply links. Splits, labels and scores are all per thread, never per tweet.
- **Inbound turn**: a tweet from the customer. Brand turns are replies from the company.
- **Intent**: what the customer wants: delivery, billing, account_access, outage_technical, complaint, praise or other.
- **Urgency**: low, normal or high.
- **Action**: what support should do next: answer, ask (for more detail), route (to a specialist team) or escalate (to a person now).
- **Escalation**: action `escalate`. A **missed escalation** is a thread the gold label says to escalate that the system didn't; a **false escalation** is the reverse.
- **Tier**: one labeller in the cascade: `cheap` (rules and keywords), `larger` (a model), `agent` (drafts the reply). A thread moves up when the tier below is not confident enough.
- **Threshold**: the confidence below which the cheap tier hands a thread up. The owner sets it.
- **Gold label**: the owner-confirmed intent, action and should-escalate for one eval thread, in `evals/gold/`. Protected.
- **Split**: dev, eval or full, by thread, from `data/split.json` (seed and fractions owner-decided, protected).
- **Placeholder price**: a made-up per-call cost used until real prices are set. Reports must say so.

## Factory

- **Job**: one issue's trip through the factory, in `factory/jobs/<id>/`. Its `job.json` is written only by `factory/bin/job`.
- **Stage**: intake, spec, build, gate, review, approve, merging, recovery, needs-info, needs-input, needs-human, merged, rejected.
- **Clarify phase**: intake and spec only. It ends when the spec has no open product question (`job clarify`); open questions go on the issue with the `needs-input` label.
- **Build phase**: build, gate, review, approve and PR. It never starts while a product question is open.
- **Lane**: the clarify and build phases run as two independent lanes, side by side, so specs and owner questions move while other issues build.
- **Round**: one build plus its gate and reviews.
- **Gate**: the scripted check after each build: protected paths, spec scope, `factory/checks.sh`.
- **Seam**: the public boundary a test runs against. The spec names the seams; the builder tests only there.
- **Blast radius**: what a change can break. `job merge` merges unseen only what is on the auto-merge allowlist.
- **Owner**: Aadhar. Adds the `factory` label, merges what the factory hands over, and makes the product calls.
