//go:build !linux

package ingest

import "os"

func sweepTemp(string, sweepConfig)             {}
func acquireLease(string, sweepConfig) *os.File { return nil }
