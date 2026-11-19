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
