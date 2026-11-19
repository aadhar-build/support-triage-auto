# Factory workers

The existing factory skill runs each assigned role through `factory/bin/worker`. The command reads the role instructions from `.claude/agents/` and the current job from `factory/jobs/`. It publishes the role's Markdown and records its harness, requested model, effort, CLI version, login mode and candidate SHA in a neighboring JSON file. Stage transitions still go through `job` and `gate`.

## Choose a harness and model

Configure `workers.roles` in `factory/config.json`. This example assigns just the code reviewer to Codex.

```json
{
  "workers": {
    "timeout_s": 1800,
    "roles": {
      "reviewer-code": {
        "harness": "codex",
        "model": "gpt-6.1-terra",
        "effort": "high"
      }
    }
  }
}
```

Supported roles are `intake`, `spec-writer`, `builder`, `reviewer-code`, `reviewer-eval`, `reviewer-security` and `approver`. An unassigned role keeps its existing Claude Code model from `models` or the job's routed reviewer models. The reviewer assignment does not change which reviewers the job requires. Every explicit assignment names a harness and model. Leave out `effort` to use that CLI's default.

Install the native CLI on the worker host and sign in through its own login command. Use `factory/bin/worker --preflight claude` or `factory/bin/worker --preflight codex` to check the local login and CLI version. A Claude worker requires a recognized first-party paid Claude login. A Codex worker requires a reported ChatGPT login. An unknown login, missing CLI, unsupported model, timeout or worker error stops that invocation. The launcher tries only the selected harness. Check the subscription account's own extra-usage settings separately.

## Run and resume

Use `factory/bin/worker <job-id> <role>` from the checkout. Run the corresponding `job` or `gate` step after the worker succeeds. The clarify roles use disposable Git snapshots. Build, review and approval take a snapshot of the branch assigned to the job and check that it has not moved during the run. Reviewer and approver snapshots are read-only. The builder edits only its disposable snapshot. Before updating the local job branch, the launcher applies the protected-path, scope and unresolved-product-question checks to the candidate. The existing gate still runs afterward.

Existing job files are preserved by default. Use `--replace` only after inspecting the existing artifact, such as an old spec after the base changed or build notes from a previous round. If a builder reports a pending publication, resolve any filesystem error and run `factory/bin/worker --recover <job-id>`. Recovery uses the recorded candidate and artifact without another model call. The push command refuses while a builder publication is active or pending. For a lock left by an abruptly terminated process, confirm the process has ended before removing its lock directory.

Worker tools have no configured network access. A builder that needs dependencies for local checks will need them available on the worker host, or should report which checks it could not run. The worker never pushes, opens a PR, merges a PR or changes the job stage.

## Concurrent operation and interrupted work

Run the Python commands and Go worker from the same checkout on a single host. Drain older dispatchers and finish their pending publications before upgrading or rolling back the pair. Older binaries do not participate in job admission. A builder journal produced before spec digests were recorded needs reconciliation with its original binary and original inputs.

A job command reserves that job while it runs. A gate runs checks outside the job mutex and discards its result if the job revision changes. A separate lock on the worktree prevents overlapping round and final checks, including while a surviving checks subprocess finishes after its parent gate exits. Distinct reviewer roles can run concurrently. Consumers check the locks and replacement markers for the roles whose evidence they use before reading artifacts or taking action. An active or interrupted builder publication blocks job changes, including scorecard updates, until its journal clears. Retry a busy command after the active operation finishes.

If a command dies, inspect `.factory-operation.json` in the job directory. It identifies the command, host, PID, token and recorded effects. Verify the process and any child processes have ended. Inspect the job state, evidence, worktree, the remote branch and any relevant PR, comments and labels. Reconcile any completed or uncertain effects before releasing the reservation with `factory/bin/job reconcile-operation <id> <token> --reconciled`. This command checks host, token and PID and only removes that reservation. It does not replay external actions.

A gate interrupted during publication can leave both the job operation reservation and its round receipt marker. Reconcile each surviving record. A surviving `.gate-publication.pending` blocks further jobs and workers. Verify the gate process is gone and inspect the round recorded in the marker. If the job is still in gate, discard the uncommitted round receipt, remove the marker and rerun the gate. If the job transitioned, verify the receipt, candidate, base, spec, verdict, round and job history before removing the marker. Preserve the marker and escalate any ambiguity.

A failed intake, spec-writer, or builder replacement leaves its audit’s `.replacing` marker. The old Markdown can still be inspected. Rerun that role with `--replace` to publish a fresh artifact and remove the marker. If the builder has already recorded a `.pending` journal, use builder recovery instead.

For a builder journal, inspect the recorded round and worktree, clear an abandoned role lock only after verifying its process is gone, then use `factory/bin/worker --recover <id>`. Recovery reuses the recorded bytes and commit. Preserve the journal and inspect any mismatch. Direct edits to Git refs and job files, multiple hosts, and filesystems without interoperable BSD flock semantics fall outside the admission protocol.
