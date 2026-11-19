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
