// Package admin is the Go port of triage.admin: the read-only local console's
// run listing, overview data and request guard.
package admin

import (
	"cmp"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/eval"
	"github.com/aadhar-build/support-triage-auto/backend/internal/report"
)

// Run statuses, as in admin.py.
const (
	StatusOK    = "ok"
	NoManifest  = "no manifest"
	BadManifest = "unreadable manifest"
	NoMetrics   = "no usable metrics file"
)

const manifestSchema = "run-manifest/1"

var (
	manifestKeys = []string{"schema_version", "kind", "created_at", "cost"}
	costKeys     = []string{"estimated_usd", "cost_per_1000_usd", "prices_are_placeholders"}
)

// Run is one direct subfolder of the runs folder. Kind, CreatedAt and Cost
// are set only when Status is "ok". Dir is never sent to a client.
type Run struct {
	Name, Status    string
	Kind, CreatedAt string
	Cost            map[string]any
	Dir             string
}

// kinds is admin.py's KINDS: the union of the cohort names.
func isKind(s string) bool {
	for _, names := range eval.CohortNames {
		if slices.Contains(names, s) {
			return true
		}
	}
	return false
}

func exactKeys(m map[string]any, keys []string) bool {
	if len(m) != len(keys) {
		return false
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return false
		}
	}
	return true
}

func numberOrNull(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case int64:
		return x >= 0
	case float64:
		return x >= 0
	}
	return false
}

// checkManifest is _check_manifest: ok is false for any shape problem.
func checkManifest(v any) (kind, createdAt string, cost map[string]any, ok bool) {
	m, isMap := v.(map[string]any)
	if !isMap || !exactKeys(m, manifestKeys) {
		return "", "", nil, false
	}
	if s, _ := m["schema_version"].(string); s != manifestSchema {
		return "", "", nil, false
	}
	kind, _ = m["kind"].(string)
	if !isKind(kind) {
		return "", "", nil, false
	}
	createdAt, isStr := m["created_at"].(string)
	if !isStr || eval.CheckRunAt(createdAt) != nil {
		return "", "", nil, false
	}
	c, isMap := m["cost"].(map[string]any)
	if !isMap || !exactKeys(c, costKeys) {
		return "", "", nil, false
	}
	if _, isBool := c["prices_are_placeholders"].(bool); !isBool ||
		!numberOrNull(c["estimated_usd"]) || !numberOrNull(c["cost_per_1000_usd"]) {
		return "", "", nil, false
	}
	return kind, createdAt, c, true
}

// timeKey is _time_key: seconds, then the fraction as a float.
func timeKey(createdAt string) (time.Time, float64) {
	base, _ := time.Parse("2006-01-02T15:04:05", createdAt[:19])
	frac := createdAt[19 : len(createdAt)-1]
	if frac == "" {
		return base, 0
	}
	f, _ := strconv.ParseFloat("0"+frac, 64)
	return base, f
}

// ListRuns is list_runs: one Run per direct subfolder, ok runs newest first
// (ties by name), then unreadable manifests, then folders with no manifest,
// each by name. Only manifest.json is read.
func ListRuns(runsDir string) ([]Run, error) {
	entries, err := os.ReadDir(runsDir)
	if err != nil {
		return nil, err
	}
	var good, other []Run
	for _, e := range entries {
		if !e.Type().IsDir() || !utf8.ValidString(e.Name()) {
			continue
		}
		r := Run{Name: e.Name(), Dir: filepath.Join(runsDir, e.Name())}
		v, err := report.ReadJSON(filepath.Join(r.Dir, "manifest.json"))
		switch {
		case errors.Is(err, fs.ErrNotExist):
			r.Status = NoManifest
		case err != nil:
			r.Status = BadManifest
		default:
			var ok bool
			if r.Kind, r.CreatedAt, r.Cost, ok = checkManifest(v); ok {
				r.Status = StatusOK
				good = append(good, r)
				continue
			}
			r.Status = BadManifest
		}
		other = append(other, r)
	}
	slices.SortStableFunc(good, func(a, b Run) int {
		at, af := timeKey(a.CreatedAt)
		bt, bf := timeKey(b.CreatedAt)
		if c := bt.Compare(at); c != 0 {
			return c
		}
		if c := cmp.Compare(bf, af); c != 0 {
			return c
		}
		return cmp.Compare(a.Name, b.Name)
	})
	slices.SortStableFunc(other, func(a, b Run) int {
		if a.Status != b.Status {
			if a.Status == BadManifest {
				return -1
			}
			return 1
		}
		return cmp.Compare(a.Name, b.Name)
	})
	return append(good, other...), nil
}
