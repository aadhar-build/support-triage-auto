package eval

import (
	"errors"
	"regexp"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// runAtRE is Python's _RUN_AT_RE restricted to ASCII digits (owner decision Q1).
var runAtRE = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$`)

// CheckRunAt is Python's metrics.check_run_at. The first 19 bytes must be a
// real date and time; year 0, which time.Parse accepts, is refused as
// strptime refuses it.
func CheckRunAt(s string) error {
	if !runAtRE.MatchString(s) {
		return errors.New("run_at must be an ISO 8601 UTC time ending in Z, got " + route.PyRepr(s))
	}
	tm, err := time.Parse("2006-01-02T15:04:05", s[:19])
	if err != nil || tm.Year() < 1 {
		return errors.New("run_at is not a valid time: " + route.PyRepr(s))
	}
	return nil
}
