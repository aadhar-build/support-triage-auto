# Factory autonomy shadow check

This command evaluates one proposed build pickup or no-action decision against a supplied snapshot. It never invokes a model, runs a job, reserves capacity, or writes factory state. The caller supplies both the snapshot and the model's proposal on standard input. The snapshot is only as trustworthy as its source; an accepted shadow result is **not** authorization to dispatch a job.

Run from `backend/` with Go 1.26 or newer:

```sh
go run ./cmd/factory-autonomy-shadow < request.json
```

A no-action example:

```json
{
  "snapshot": {"Revision": "snapshot-1"},
  "proposal": {"Action": "no-action", "StateRevision": "snapshot-1"}
}
```

A pickup proposal needs the snapshot's current base, explicit capacity and active-build count, and the selected job's current revision, owner authorization, round, and independently verified specification evidence. The proposal must include `Action: "pickup-build"`, `StateRevision`, `JobID`, and `JobRevision`. `Jobs` and every job's `Round` must be present. The result is one JSON object with `mode: "supplied-snapshot-shadow"`, `dispatch_authorized: false`, `accepted`, `action`, and a fixed refusal `reason` when applicable. Unknown fields, ambiguous duplicate keys, overlarge inputs, and incomplete pickup inputs are rejected.

Production adoption requires a trusted snapshot source and a real compare-and-swap operation that atomically fences the job and capacity; those integrations are not implemented here.
