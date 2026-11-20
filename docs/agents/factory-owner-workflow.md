# Factory owner workflow

Run these commands from the main checkout that holds the job. Use `factory/bin/job list` to get its ID and `factory/bin/job card <job-id>` to read the current candidate, evidence, open questions and next action. Ask the owner for only the decision or fact missing from the evidence.

When a new issue response omits the repository ID, job creation makes one read-only GitHub repository lookup. If that lookup fails, admission continues and the job has no tracker link. Cards and weekly scorecards run offline by default. Add `--verify-github-links` to either command to check a new job's captured repository and issue IDs on GitHub. The card also checks the original pull request ID, repository IDs and exact current head SHA before linking the candidate, including after later pushes to the same pull request. Missing IDs, unavailable GitHub responses, a different origin or an unpushed candidate leave the corresponding link unavailable. These checks confirm the current GitHub targets. The original job association comes from local controller records under `.git/factory-link-receipts/`, which anyone with controller write access can alter.

## Capture owner effort and decisions

Record one `owner_effort` observation for each distinct span of active work the owner reports. Enter the UTC time the activity occurred, even when recording it later. Mark `rework: true` when the owner was revisiting work or a decision because the prior result needed correction.

| Activity | What to record |
| --- | --- |
| `preparation` | Defining scope, preparing the issue or agreeing acceptance criteria |
| `clarification` | Answering a question or making a product decision |
| `supervision` | Actively checking progress or coordinating the run |
| `review` | Evaluating a submitted change, evidence or demo |
| `recovery` | Diagnosing an interruption or choosing how to proceed |
| `correction` | Personally correcting an outcome or preparing the follow-up fix |

Record only active minutes. Waiting for builds, agent runs or reviews is elapsed time. Use `minutes: 0` for an explicitly reported zero. An unreported duration stays unknown. For a partial log, the sum is a known subtotal. Add an `owner_effort_coverage` event with `scope: "job"` and `complete: true` once the owner confirms the full job log. Use UTC `window_start` and `window_end` to attest coverage for a bounded interval.

When the owner makes a disposition, record `owner_decision` with `disposition` set to `accepted`, `rejected` or `abandoned`, and `material_follow_up` when known. A merge or technical review alone leaves owner acceptance unknown. Record an escalation judgment only when the owner supplies it, using an `escalation` event with a stable `request_id` and its reported `rating` of `necessary`, `unnecessary` or `missed`.

Record when the owner made the decision and identify the exact candidate SHA and round for code. For a direct non-code task without a candidate, add a `task_context` observation with `workflow_mode: "direct"` and a non-code `task_type` such as `planning` or `research`. Reports leave the outcome unknown when they cannot establish the applicable decision or its order.

For costs, record `usage_cost` with the source billing item or UTC source window. Use `subscription_usage` for metered usage and name the unit, `subscription_charge` or `incremental_charge` for actual billed amounts, and `api_equivalent_estimate` for estimated API cost. For money, send a decimal string `amount` and a three-letter `currency`. Access through a subscription establishes neither a bill amount nor a zero incremental charge. Legacy fields from `job record` remain readable. Use the typed observations for new measurements.

### Submit and correct an observation

This Bash example submits owner-confirmed review minutes. Set `JOB_ID` to the existing job ID, `OCCURRED_AT` to the activity's ISO 8601 UTC timestamp ending in `Z`, `MINUTES` to the reported number, and `SOURCE_RECORD_ID` to a stable, sanitized identifier for the owner's report. Generate `EVENT_ID` once with `python3 -c 'import uuid; print(uuid.uuid4())'` and save it with your input before submitting. Leave `SUPERSEDES_EVENT_ID` unset for the first observation.

```bash
observation_json=$(
  python3 - "$JOB_ID" "$EVENT_ID" "$OCCURRED_AT" "$SOURCE_RECORD_ID" "$MINUTES" "${SUPERSEDES_EVENT_ID:-}" <<'PY'
import json
import sys

job_id, event_id, occurred_at, source_id, minutes, supersedes = sys.argv[1:]
event = {
    "schema_version": 1,
    "event_id": event_id,
    "job_id": job_id,
    "kind": "owner_effort",
    "origin": "real",
    "occurred_at": occurred_at,
    "source": {"kind": "owner", "record_id": source_id},
    "data": {"activity": "review", "minutes": float(minutes), "rework": False},
}
if supersedes:
    event["supersedes_event_id"] = supersedes
print(json.dumps(event, allow_nan=False))
PY
) && printf '%s\n' "$observation_json" | factory/bin/job observe "$JOB_ID" -
```

For a retry, send the same payload with the same `EVENT_ID`. The response preserves the first recording time. If the owner corrects the value, use a new `EVENT_ID`, set `SUPERSEDES_EVENT_ID` to the UUID of the current observation and `SOURCE_RECORD_ID` to the source of the correction, and run the same command with the corrected values. Keep the same job and kind and supply the complete replacement data. For a minutes-only correction, keep the original activity time. The command preserves history and assigns the new recording time. Read the card again to verify the current observation.

The ledger is local to `factory/jobs/<job-id>/observations/`, alongside `job.json`. Preserve that directory during controller updates and rollbacks. Point `evidence` to an approved stable HTTPS record or a sanitized job-relative artifact using `path` and `sha256`. A local reference resolves under `factory/jobs/<job-id>/`, so a recipient needs access to that checkout and artifact. Store only vetted identifiers and sanitized references. Keep raw transcripts, customer data, secrets, credentials and signed URLs out of the ledger and any shared report.

## Review recovery and linked corrections

When a job pauses after failed checks or review rounds, read `factory/bin/job recovery <job-id>` and `factory/bin/job card <job-id>`. Use the failed rounds, candidate, current evidence and open question to ask for one owner decision. When the evidence supports it, discuss a scope split, a permitted alternative or deferring failed scope as follow-up work. Keep an unresolved job paused within the existing stage and gate rules. The recovery and card commands read evidence and leave job state alone.

Record the reported choice and outcome as `recovery_decision` with a stable `failure_id`, relevant round and candidate where known, and owner provenance for the actual owner choice. For a post-merge defect, use a `defect` observation with a stable `defect_id`, discovery time and impact. Keep the cause unknown until there is supporting evidence. Link the observed fix through `correction_event_id`, a separate observation with an evidence reference. Link an exact-candidate `check_evidence` event through `regression_check_event_id`, or supply a grounded `no_check_reason` when no suitable check exists. Use `owner_effort_event_ids` and `interruption_event_ids` for related observations. Read recovery again to confirm the links resolve and distinguish reported outcomes from verified evidence. Changes to an earlier observation use `supersedes_event_id`.

## Review a week on demand

Ask for the start date, IANA time zone and escaped-defect follow-up window in days. Use the supplied settings for the seven local calendar days beginning at that date.

```bash
factory/bin/scorecard --weekly "$WEEK_START" --timezone "$REPORT_TIMEZONE" --defect-window-days "$DEFECT_WINDOW_DAYS"
```

Append `--json` for structured output. Use `--as-of` with the same UTC cutoff to reproduce the observation view. If the zone or follow-up window is pending, the CLI can omit the corresponding flag and report the affected measure as unknown. Defined empty weeks can have zero deliveries and unavailable rates. Review delivered work, owner effort and rework, guardrail evidence gaps and recurring blockers with their coverage and local evidence references. Jobs with only an issue or pull request number appear as local references. A weekly report can link a pull request when the optional GitHub merge check verifies the job’s recorded delivery and local candidate. The weekly command runs on demand. Scheduling and publishing require a separate owner request.

## Open weekly evidence offline

Use the same week, time zone, UTC cutoff and escaped-defect window for the private scorecard and the sanitized bundle. Set `DEFECT_WINDOW_DAYS=7` for seven days of follow-up after each recorded delivery. The bundle requires an explicit cutoff. A missing time zone leaves the cohort unknown. A missing defect window leaves the escaped-defect rate unknown.

```bash
factory/bin/evidence-bundle --weekly "$WEEK_START" --timezone "$REPORT_TIMEZONE" \
  --as-of "$OBSERVATION_CUTOFF" --defect-window-days "$DEFECT_WINDOW_DAYS" \
  --output "$NEW_DIRECTORY"
```

Choose an unused physical path under an existing owner-controlled parent. Open `index.html` in Chrome after the command succeeds. The dashboard leads with recorded deliveries, owner acceptance, known owner time and escaped defects, followed by submission outcome and evidence coverage charts. An unavailable rate can still have recorded counts. Deliveries still within the follow-up window are shown separately from those that have reached its end. Expand **Report details** sections for UTC boundaries, rework, cost categories, evidence gaps and the historical baseline.

The dashboard uses local recorded deliveries. GitHub merge verification is unavailable offline. Controlled jobs and controlled observations are excluded from weekly measures. Unreadable and undated jobs remain visible as counts. Job aliases follow the private scorecard JSON's `active_jobs` order starting from 1. A recorded delivery missing from that list receives the next alias and flags incomplete active-job coverage. Task label groups and cost unit groups also use local numbers. Use the private scorecard with the same settings to inspect individual jobs, task labels, cost units, source records and evidence paths.

To follow particular evidence offline, add `--weekly-evidence "$JOB_ID:round-2/checks.md@$EXPECTED_SHA256"` to the command. Repeat for up to 32 supported native artifacts from active jobs in that week. A projected link opens a finite JSON verdict and its exported SHA-256 appears in the manifest. Missing, stale and withheld artifacts get a status. The bundle keeps the job ID, original path, original digest and report body in the source checkout. Selected receipts project the current local artifacts. Their presence during the reporting week or by the cutoff is unverified. A recorded delivery or evidence gap can link to a relevant selected receipt, and selection can cover only part of the week's evidence. Review job and source associations in the private scorecard.

The weekly exporter checks the cohort and selected source state again before it returns success. Use it for the five-minute owner review of a named week and record the observed review outcome separately. A recipient needs an authorized copy of the entire directory. Scheduling, hosting and publishing require a separate owner request.

## Open selected job evidence offline

Run `factory/bin/evidence-bundle` with the job ID in the checkout containing the job. Choose a new directory under an existing local parent that you own and that other users cannot write. Use its physical path, with no symlinked components.

To inspect a current-round gate, use its job-relative path and expected SHA-256. For example, the following command exports a round-two gate after you set the job ID, expected source digest, and destination.

```bash
factory/bin/evidence-bundle "$JOB_ID" --gate "round-2/checks.md@$EXPECTED_SHA256" --output "$NEW_DIRECTORY"
```

Use the digest from the receipt you intend to inspect. You can use `shasum -a 256` on the source file to pin its present bytes for a local inspection. An independently obtained digest provides a separate point of comparison. For a bound gate, the exporter compares the recorded candidate, base and spec digest with the local checkout. A mismatch marks the evidence stale. Unavailable bindings withhold the projection. Legacy gate headers provide historical verdicts. The JSON summary contains the recorded PASS or FAIL, and the manifest records both the source and exported SHA-256. The original Markdown can contain command output and private paths.

Use `--overview` to show the stage, round, local candidate commit, applicable checks and reviews, numbered criteria from the local spec, and demo status. Additional routed reviewers appear under numbered labels. The overview reports when acceptance criteria are unavailable and works without any selected artifact, including on a blocked job.

```bash
factory/bin/evidence-bundle "$JOB_ID" --overview --output "$NEW_DIRECTORY"
```

Add `--artifact PATH@SHA256` for each native report you want to open. Supported paths are `round-N/checks.md`, `round-N/review-{code,security,eval}.md`, `decision.md`, and `final-checks.md`. Round paths must match the current job round. Reviews show PASS or CHANGES. Conflicting verdict declarations withhold the review projection. Technical decisions show APPROVE or ESCALATE. Each link opens a JSON verdict projection, with the original report body withheld.

You can also add `--include evidence/check-1.json` for a path and digest referenced by a current check, criterion or demo observation. You can use this option alone or repeat it with other selected receipts. The eligible paths are `evidence/check-1.json` through `evidence/check-999.json`. This optional format requires a producer to supply the small receipt and observation. For a check reporting a pass, the exact UTF-8 receipt bytes are the following line followed by one newline.

```json
{"kind":"check","schema_version":1,"verdict":"pass"}
```

Valid kinds are `check`, `criterion` and `demo`. Valid verdicts are `pass`, `fail` and `pending`. Keys must use the displayed order and spacing. The values must match the referencing observation. The exporter checks the bindings declared by the observation and its evidence references. It accepts a finite set of complete byte strings. Other receipt contents, URLs and free-form identifiers stay out of the bundle. For the full decision card, use `factory/bin/job card "$JOB_ID"`.

Open `index.html` in Chrome from the selected directory. The index links to the exported local receipts and to `manifest.json`. A row can show a copied receipt, a projected verdict, a missing source, a stale binding or digest, or withheld evidence. Claim verification and owner acceptance remain unverified. A match confirms the selected bytes at export time. A recipient on another device needs an authorized copy of the entire directory, including its JSON files.

The command checks up to 32 selected artifacts, 256 evidence rows and 64 ledger directory entries. It refuses an incomplete or busy ledger. Copied receipts are capped at 1 KiB and native report inputs at 256 KiB. Run it in an owner-controlled directory while other processes leave the destination alone. The directory is visible while the command writes it. Wait for success before opening it. The command checks the published names and bytes and rechecks the job state and sources for all exported receipts and verdicts. Forced termination or a write failure can leave a partial private directory. Use a fresh destination for a retry. The bundle makes no network calls and starts no service.

### Review a private decision view

Use `--private-owner-view` when the owner needs criterion wording, the local candidate, checks and reviews, demonstration status, limitations, decisions, measured time and available cost records, and one next action on the same page. Choose a private local destination outside Git checkouts and folders that sync to other devices. The exporter checks the factory's Git worktree inventory and the destination's parent directories. This mode excludes controlled observations and requires a normal job.

```bash
factory/bin/evidence-bundle "$JOB_ID" --private-owner-view --output "$PRIVATE_DIRECTORY"
```

Add the same `--gate`, `--artifact` or `--include` selections to make projected evidence clickable next to the relevant requirement. Use `1. [stable-id] Wording` in the local spec to retain a criterion’s ID if you reorder it. A criterion identified only by its written number needs a matching candidate, round and spec digest before a report links to its current wording. Reports with uncertain associations appear under Additional evidence. The private view displays criterion IDs, refuses duplicates and can indicate when a separate native report is available locally.

Source reports show their candidate, round, base and spec bindings. A matching digest identifies the selected bytes. Local receipt validation checks supported factory-host records against the current candidate. Independent verification is unknown because this view uses only factory-host evidence. The separate owner-side GitHub publication observer records GitHub merge metadata, which is outside the inputs to this export.

Owner reports follow their occurrence time. If a current report has no occurrence date, the current owner decision remains unknown. A demonstration receipt records a report about the working software. Use the normal approval workflow to inspect the demonstration itself and record the owner decision.

Open the private `index.html` in Chrome after the export succeeds. The directory and files use owner-only permissions. Criterion text and decision details appear in the private HTML. The manifest still contains the limited row and digest fields. Keep the whole directory local and use the sanitized export when you need outputs without the private labels.

## Retire a merged job checkout

Run the following preview for one job from the controller's main checkout, using the same local account as the factory. Run in the Linux host’s initial PID and user namespaces and its init mount namespace. The command requires a local filesystem, operator-controlled directories and a process mount that exposes every task. Reserve a maintenance window in which the controller owns all writers to this checkout, including editors, test processes and users on shared mounts. The process scans observe activity at the instant of inspection. An independent writer can start afterward. Leave the checkout in place when you cannot ensure exclusive access. Preview briefly acquires factory directory locks and reads the GitHub repository, base ref and pull request.

```bash
factory/bin/retire-worktree "$JOB_ID"
```

An eligible result includes the exact candidate SHA and PR number. The factory checks the assigned branch and both Git registration pointers, hashes tracked file contents, checks for ignored and untracked files, and inspects active processes, operation markers and stacked dependencies. The GitHub merge result must agree with the exact candidate and its integration into the configured base. Retain a checkout if its current verification needs the physical directory.

```bash
factory/bin/retire-worktree "$JOB_ID" --apply
```

Older jobs can lack a historical receipt linking the job to its PR. A squash or rebase can also leave equivalent changes in the merge result while dropping the original commit identity. Preview then returns `owner confirmation required` and a `confirm_pr` value. Read each reason. Check that the displayed PR belongs to this job and that its merged changes match the candidate. To accept the stated uncertainty, pass its exact `PR@SHA` value with `--apply --confirm-pr`. All other checks still apply.

Apply parks the linked checkout under a private random name in `.git/factory-retirements`, inspects it again, then asks Git to remove that single checkout. The job directory, receipts, local branch and remote refs remain available. The controller's journal records the observed merge and evidence bindings. Reporting labels these records as local history. A changed record, candidate or evidence file can invalidate that history.

After an interruption, inspect the job's private journal in `.git/factory-retirements` and the paths it names. Use the following command to reconcile one job.

```bash
factory/bin/retire-worktree "$JOB_ID" --recover
```

Recovery restores an intact parked checkout and records cancellation, or finalizes a removal Git already completed. A partial checkout, new files, changed evidence or ambiguous registration leaves the journal available for inspection. If any process outside the controller is writing to the repository, finish that work before retirement. Each command addresses exactly one job.
