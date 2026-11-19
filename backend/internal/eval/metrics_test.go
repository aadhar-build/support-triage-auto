package eval

import (
	"encoding/json"
	"maps"
	"os"
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

// Expected values are Python 3.12 comparison_key outputs.
func TestComparisonKey(t *testing.T) {
	rev := "sha256:" + strings.Repeat("a", 64)
	cohort := func() map[string]any {
		return map[string]any{"name": "gold_random", "split": "dev", "gold_revision": rev, "brand_population": "all"}
	}
	m := func(c any) map[string]any { return map[string]any{"schema_version": 1, "kind": "gold", "cohort": c} }
	got, err := ComparisonKey(m(cohort()))
	if err != nil || got != [6]any{1, "gold", "gold_random", "dev", rev, "all"} {
		t.Fatalf("ComparisonKey = %v, %v", got, err)
	}
	other := cohort()
	other["split"] = "test"
	k2, _ := ComparisonKey(m(other))
	if k2 == got {
		t.Error("different splits gave equal keys")
	}
	k3, _ := ComparisonKey(m(cohort()))
	if k3 != got {
		t.Error("equal metrics gave different keys")
	}
	const p = "metrics lacks a comparison field: "
	noCohort := m(nil)
	delete(noCohort, "cohort")
	noKind := m(cohort())
	delete(noKind, "kind")
	noSplit := cohort()
	delete(noSplit, "split")
	cases := []struct {
		m    map[string]any
		want string
	}{
		{noCohort, p + "'cohort'"},
		{noKind, p + "'kind'"},
		{m(noSplit), p + "'split'"},
		{map[string]any{"cohort": nil}, p + "'schema_version'"},
		{nil, p + "'cohort'"},
		{m(nil), p + "'NoneType' object is not subscriptable"},
		{m([]any{1}), p + "list indices must be integers or slices, not str"},
		{m("x"), p + "string indices must be integers, not 'str'"},
		{m(true), p + "'bool' object is not subscriptable"},
		{m(3), p + "'int' object is not subscriptable"},
		{m(int64(3)), p + "'int' object is not subscriptable"},
		{m(uint64(3)), p + "'int' object is not subscriptable"},
		{m(json.Number("3")), p + "'int' object is not subscriptable"},
		{m(json.Number("123456789012345678901234567890")), p + "'int' object is not subscriptable"},
		{m(2.5), p + "'float' object is not subscriptable"},
		{m(json.Number("2.5")), p + "'float' object is not subscriptable"},
		{m(json.Number("1e3")), p + "'float' object is not subscriptable"},
	}
	for i, c := range cases {
		_, err := ComparisonKey(c.m)
		if got := errText(err); got != c.want {
			t.Errorf("case %d: got %q, want %q", i, got, c.want)
		}
	}
}

// testdata/proportion.json is Python 3.12 to_json of proportion(7,9),
// (0,0), (29,29) and (3,30).
func TestProportionToJSON(t *testing.T) {
	want, err := os.ReadFile("testdata/proportion.json")
	if err != nil {
		t.Fatal(err)
	}
	got, err := ToJSON(map[string]any{"p": Proportion(7, 9), "z": Proportion(0, 0), "q": Proportion(29, 29), "r": Proportion(3, 30)})
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("got\n%s\nwant\n%s", got, want)
	}
}
