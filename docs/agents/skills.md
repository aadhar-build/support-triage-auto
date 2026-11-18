# Skills: how the factory uses them

The factory's own rules (`AGENTS.md`, `.claude/skills/factory/SKILL.md`, the agent files) always win over a vendored skill. Where a skill says something different, the override below applies.

## Where each skill sits

| Stage | Skill | Who runs it | How the factory uses it |
| --- | --- | --- | --- |
| Plan | `grill-me` / `grilling` | the owner, by asking for it | Stress-test a plan in rounds before any issue exists. |
| Plan | `to-spec` | the owner | Turn a settled plan into a GitHub issue (problem, user stories, decisions, testing, out of scope). |
| Plan | `to-tickets` | the owner | Split a big issue into vertical-slice issues with "Blocked by" edges. Stacked jobs follow those edges. |
| Intake | `triage` | `intake` agent | Redundancy check, `.out-of-scope/` check, needs-info notes template. |
| Spec | `to-spec`, `codebase-design` | `spec-writer` agent | Problem statement, short user stories, seams under test, out of scope. |
| Build | `tdd` | `builder` agent | One test, then the code for it, at the spec's seams; no tautological or implementation-coupled tests. |
| Review | Pocock's two-axis review | `reviewer-code` agent | Standards and Spec reported separately; code smells are notes, never blockers on their own. |
| PR | `file-pr` | `job pr` | PR body leads with the problem, then the fix, then how it was proved. |
| PR | `leaving-pr-comments` | `job` (every comment it posts) | Comments open with a 🤖 note saying who is speaking for the owner. |
| Ship | `babysit-pr` | the orchestrator | After the PR opens: read check-runs through REST; a real CI failure becomes the next build round. |
| Ship | `full-stop` | the factory's default | Stop with the PR ready for the owner to merge. |
| Ship | `full-send` | the owner only, by saying "full send" | Off for factory jobs (see below). |
| Fix up | `takeover`, `modernize` | the owner, by asking | Finish or rebase an old PR, keeping its credit. |

## Overrides

1. **The `factory` label stays the owner's.** Where `to-spec`, `to-tickets` or `triage` say to apply `ready-for-agent`, apply the `ready-for-agent` label only. The owner adds `factory` to start a job.
2. **No seam sign-off mid-run.** `tdd` and `to-spec` say to confirm seams with the user. In a factory run, the spec's `## Seams` section is that agreement, and the builder never waits for a person.
3. **File paths in specs.** `to-spec` says not to name files. The factory spec must list them under `## Files`, because the gate enforces scope with that list.
4. **Merging.** Factory jobs merge only through `job merge`. `full-send` is never used on a factory job; on owner-led work it needs the owner to say "full send". While `automerge_allowlist` in `factory/config.json` is empty, `job merge` hands every PR to the owner. It is empty in support-triage-auto until the design-review findings 1 to 4 are fixed (PRD v1.1); the old list is kept under `automerge_allowlist_paused`.
5. **No `sol-loop` or `council`.** Where `full-send` says to run `sol-loop`, run the factory reviewers (or `/code-review`) instead.
6. **REST only.** `babysit-pr`'s `pr-wait` and `gh pr` use GraphQL, which this environment blocks. Use `gh api repos/<owner>/<repo>/commits/<sha>/check-runs` and `.../pulls/<n>` instead, and the session's PR-activity subscription to wait. Never write a polling loop.
7. **Public posting.** The skills never post outside this repo. Drafts only (`AGENTS.md` rule 10).
