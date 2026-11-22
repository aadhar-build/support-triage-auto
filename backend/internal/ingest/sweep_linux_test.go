//go:build linux

package ingest

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSweepRemovesLeasedOrphan(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "support-triage-ingest-orphan")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }})
	if _, err := os.Lstat(dir); !os.IsNotExist(err) {
		t.Fatalf("orphan remains: %v", err)
	}
}

func TestSweepCutoffAndFresh(t *testing.T) {
	base := t.TempDir()
	now := time.Now()
	for _, tc := range []struct {
		name string
		age  time.Duration
	}{{"cutoff", 24 * time.Hour}, {"fresh", time.Hour}} {
		dir := filepath.Join(base, scratchPrefix+tc.name)
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
			t.Fatal(err)
		}
		stamp := now.Add(-tc.age)
		if err := os.Chtimes(dir, stamp, stamp); err != nil {
			t.Fatal(err)
		}
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }})
	for _, name := range []string{"cutoff", "fresh"} {
		if _, err := os.Stat(filepath.Join(base, scratchPrefix+name)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSweepLegacyAttributed(t *testing.T) {
	// This private base stands for one historically exclusive to the ingester,
	// with no old ingester using or able to start against it.
	base := t.TempDir()
	dir := filepath.Join(base, "ingest-123")
	now := time.Now()
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"run-1", "kept-2"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0600); err != nil {
			t.Fatal(err)
		}
	}
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	cfg := sweepConfig{now: func() time.Time { return now }}
	sweepTemp(base, cfg)
	if _, err := os.Stat(dir); err != nil {
		t.Fatal(err)
	}
	cfg.legacy = true
	sweepTemp(base, cfg)
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("legacy remains: %v", err)
	}
}

func TestSweepConcurrentReplacement(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, scratchPrefix+"candidate")
	original := filepath.Join(base, "original")
	now := time.Now()
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
		t.Fatal(err)
	}
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }, beforeMove: func() {
		if err := os.Rename(dir, original); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "valuable"), []byte("keep"), 0600); err != nil {
			t.Fatal(err)
		}
	}})
	if _, err := os.Stat(filepath.Join(original, ".lease")); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(base)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".support-triage-ingest-sweep-") {
			data, err := os.ReadFile(filepath.Join(base, e.Name(), "victim", "valuable"))
			if err != nil || string(data) != "keep" {
				t.Fatalf("replacement lost: %q %v", data, err)
			}
			found = true
		}
	}
	if !found {
		t.Fatal("replacement not preserved in staging")
	}
}
