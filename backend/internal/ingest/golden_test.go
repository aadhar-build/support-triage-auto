package ingest

import (
	"fmt"
	"time"
)

// isoformat is Python datetime.isoformat() for a second-precision aware time.
func isoformat(t time.Time) string {
	_, off := t.Zone()
	sign := '+'
	if off < 0 {
		sign, off = '-', -off
	}
	return fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d%c%02d:%02d", t.Year(), int(t.Month()), t.Day(),
		t.Hour(), t.Minute(), t.Second(), sign, off/3600, off%3600/60)
}
