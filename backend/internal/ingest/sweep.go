package ingest

import "time"

const scratchPrefix = "support-triage-ingest-"
const leaseMarker = "support-triage-ingest lease v1\n"

type sweepConfig struct {
	now              func() time.Time
	legacy           bool
	beforeMove       func()
	afterLeaseCreate func()
	afterLock        func()
}
