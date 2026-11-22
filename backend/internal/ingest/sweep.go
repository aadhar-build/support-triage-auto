package ingest

import "time"

const scratchPrefix = "support-triage-ingest-"
const leaseMarker = "support-triage-ingest lease v1\n"

type sweepConfig struct {
	now              func() time.Time
	legacy           bool
	beforeMove       func() bool
	afterLeaseCreate func()
	afterLock        func()
}

// leaseHooks is empty in production and lets tests observe lease setup gaps.
var leaseHooks sweepConfig
