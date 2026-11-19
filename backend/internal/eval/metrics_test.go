package eval

import (
	"maps"
	"strings"
	"testing"
)

// Expected messages are Python 3.12 triage.metrics.check_run_at outputs, except
// the two non-ASCII-digit cases, which Go refuses by owner decision Q1.
func TestCheckRunAt(t *testing.T) {
	const bad = "run_at is not a valid time: "
	const format = "run_at must be an ISO 8601 UTC time ending in Z, got "
	cases := []struct{ in, want string }{
		{"2000-01-01T00:00:00Z", ""},
		{"0001-01-01T00:00:00Z", ""},
		{"2024-02-29T00:00:00Z", ""},
		{"2000-01-01T00:00:00.123456789Z", ""},
		{"0000-01-01T00:00:00Z", bad + "'0000-01-01T00:00:00Z'"},
		{"2023-02-29T00:00:00Z", bad + "'2023-02-29T00:00:00Z'"},
		{"2000-01-01T00:00:60Z", bad + "'2000-01-01T00:00:60Z'"},
		{"2000-01-01T00:00:61Z", bad + "'2000-01-01T00:00:61Z'"},
		{"2000-01-01T24:00:00Z", bad + "'2000-01-01T24:00:00Z'"},
		{"2000-13-01T00:00:00Z", bad + "'2000-13-01T00:00:00Z'"},
		{"2000-01-01T00:00:00", format + "'2000-01-01T00:00:00'"},
		{"2000-01-01 00:00:00Z", format + "'2000-01-01 00:00:00Z'"},
		{"2000-01-01T00:00:00.Z", format + "'2000-01-01T00:00:00.Z'"},
		{"2000-01-01T00:00:00Z\n", format + `'2000-01-01T00:00:00Z\n'`},
		// Owner decision Q1: non-ASCII digits are refused (Python accepts them).
		{"２０００-01-01T00:00:00Z", format + "'２０００-01-01T00:00:00Z'"},
		{"2000-01-01T00:00:00.５Z", format + "'2000-01-01T00:00:00.５Z'"},
	}
	for _, c := range cases {
		err := CheckRunAt(c.in)
		got := ""
		if err != nil {
			got = err.Error()
		}
		if got != c.want {
			t.Errorf("CheckRunAt(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Messages are Python 3.12 _check_cohort / _check_sample outputs.
func TestCheckCohortAndSample(t *testing.T) {
	rev := "sha256:" + strings.Repeat("a", 64)
	base := map[string]any{"name": "gold_random", "split": "dev", "gold_revision": rev, "brand_population": "all"}
	with := func(k string, v any) map[string]any {
		m := maps.Clone(base)
		m[k] = v
		return m
	}
	without := maps.Clone(base)
	delete(without, "split")
	const keys = "cohort must be an object with exactly the keys name, split, gold_revision, brand_population"
	const revMsg = "cohort gold_revision must be 'sha256:' + 64 hex digits"
	cohorts := []struct {
		kind string
		c    map[string]any
		want string
	}{
		{"gold", base, ""},
		{"gold", with("name", "synthetic"), "cohort name 'synthetic' does not fit kind 'gold'"},
		{"synthetic", map[string]any{"name": "synthetic", "split": nil, "gold_revision": rev, "brand_population": "all"}, ""},
		{"gold", with("name", 3), "cohort name 3 does not fit kind 'gold'"},
		{"gold", with("split", 3), "cohort split must be a string or null"},
		{"gold", with("gold_revision", "sha256:"+strings.Repeat("A", 64)), revMsg},
		{"gold", with("gold_revision", rev+"\n"), revMsg},
		{"gold", with("brand_population", "some"), "cohort brand_population must be 'all'"},
		{"gold", with("brand_population", []any{"all"}), "cohort brand_population must be 'all'"},
		{"gold", without, keys},
		{"gold", with("extra", 1), keys},
		{"gold", nil, keys},
	}
	for i, c := range cohorts {
		if got := errText(checkCohort(c.kind, c.c)); got != c.want {
			t.Errorf("cohort case %d: got %q, want %q", i, got, c.want)
		}
	}
	samples := []struct {
		s    map[string]any
		want string
	}{
		{map[string]any{"size": 200, "seed": int64(0)}, ""},
		{map[string]any{"size": nil, "seed": nil}, ""},
		{map[string]any{"size": uint64(1), "seed": 1}, ""},
		{map[string]any{"size": true, "seed": 0}, "sample size must be an int or null"},
		{map[string]any{"size": 1, "seed": 1.0}, "sample seed must be an int or null"},
		{map[string]any{"size": 1}, "sample must be an object with exactly the keys size, seed"},
		{nil, "sample must be an object with exactly the keys size, seed"},
	}
	for i, c := range samples {
		if got := errText(checkSample(c.s)); got != c.want {
			t.Errorf("sample case %d: got %q, want %q", i, got, c.want)
		}
	}
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
