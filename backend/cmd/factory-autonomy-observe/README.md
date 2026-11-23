# Factory scheduling observation

Run from `backend/` with Go 1.26.10 and select the trusted main factory checkout.

```sh
go run ./cmd/factory-autonomy-observe -checkout /absolute/path/to/factory-checkout
```

The command reads the configured build capacity, stage inventory, and local ready-job evidence. It refuses interrupted admissions, inventory changes during the read, and a checkout whose `HEAD` differs from its local `origin` tracking ref. It reads the refs already on the machine.

Add `-allow-model` to let the scheduling adapter use native CLI calls. This consumes provider quota. Choose explicit paths to trusted binaries outside the main checkout and all job checkouts. The adapter can use the other provider when a binary is absent. Use separate private homes that contain only the scheduling accounts' supported credentials and native state. The native CLIs can update state in those homes. Use a Claude subscription login, a Codex ChatGPT login, and explicit model names.

```sh
go run ./cmd/factory-autonomy-observe \
  -checkout /absolute/path/to/factory-checkout \
  -allow-model \
  -claude-bin /absolute/path/to/claude -claude-model "$CLAUDE_MODEL" \
  -claude-home /absolute/path/to/private-claude-home \
  -codex-bin /absolute/path/to/codex -codex-model "$CODEX_MODEL" \
  -codex-home /absolute/path/to/private-codex-home
```

The adapter tries Claude first. Claude gets one reconsideration after an invalid decision. The adapter can then try Codex. The report gives each provider attempt a phase and, when available, a predefined failure code. The command validates the resulting action independently against the observed state. A successful proposal describes what the model chose at that point in time. The command never dispatches work or updates a factory job. The report omits job IDs, issue and spec text, revision values, model prompts, and raw provider output.

The local inventory supplies stage counts and a local base. Verification of remote freshness and capacity leases requires authoritative sources. Native factory records lack authenticated owner approval and evidence that binds the exact spec to the ready job revision. These gaps make pickup ineligible even when a local spec is present and the stage count is below configured capacity. A model can still select `no-action`. The report leaves the cost estimate `unknown` and reports provider failure as `no-model-decision`.

The built command returns 0 for `observed` or `accepted`, 1 for an observation or decision failure, and 2 for invalid options, invalid model configuration, or an output failure.
