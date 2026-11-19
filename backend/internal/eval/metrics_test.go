package eval

import (
	"encoding/json"
	"maps"
	"os"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
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

// Fixtures are copied from tests/test_metrics.py. The expected metrics files
// in testdata/metrics_*.json are Python's to_json(run(...)) on the same cases.
var (
	testRev       = "sha256:" + strings.Repeat("a", 64)
	testRunAt     = "2026-11-18T10:00:00Z"
	testGoldCohrt = func() map[string]any {
		return map[string]any{"name": "gold_random", "split": "dev", "gold_revision": testRev, "brand_population": "all"}
	}
	testSynCohort = func() map[string]any {
		return map[string]any{"name": "synthetic", "split": nil, "gold_revision": testRev, "brand_population": "all"}
	}
)

func testSample() map[string]any { return map[string]any{"size": 200, "seed": 0} }
func testConfig() map[string]any { return map[string]any{"threshold": 0.5, "prices": nil} }

func runCompute(t *testing.T, cases []ScoredCase, kind string, opts ComputeOptions) map[string]any {
	t.Helper()
	if opts.RunAt == "" {
		opts.RunAt = testRunAt
	}
	if opts.Cohort == nil {
		opts.Cohort = testGoldCohrt()
		if kind == "synthetic" {
			opts.Cohort = testSynCohort()
		}
	}
	if opts.Sample == nil {
		opts.Sample = testSample()
	}
	if opts.Config == nil {
		opts.Config = testConfig()
	}
	m, err := Compute(cases, kind, opts)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

// sc is test_metrics.sc: gold_escalate defaults to gold_action == "escalate"
// and both intents to "other".
func sc(t *testing.T, gold, pred string) ScoredCase {
	t.Helper()
	return scFull(t, nil, "other", gold, gold == "escalate", "other", pred)
}

func scFull(t *testing.T, id *string, gi, ga string, ge bool, pi, pa string) ScoredCase {
	t.Helper()
	c, err := NewScoredCase(id, gi, ga, ge, pi, pa)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func rep(n int, c ScoredCase) []ScoredCase {
	out := make([]ScoredCase, n)
	for i := range out {
		out[i] = c
	}
	return out
}

func escalationFixture(t *testing.T) []ScoredCase {
	// TP=8, FP=2, TN=85, FN=5
	return concat(rep(8, sc(t, "escalate", "escalate")), rep(2, sc(t, "answer", "escalate")),
		rep(85, sc(t, "answer", "answer")), rep(5, sc(t, "escalate", "answer")))
}

func actionFixture(t *testing.T) []ScoredCase {
	return concat(rep(3, sc(t, "answer", "answer")), rep(1, sc(t, "answer", "ask")),
		rep(2, sc(t, "ask", "ask")), rep(1, sc(t, "ask", "route")), rep(2, sc(t, "route", "route")),
		rep(1, sc(t, "escalate", "escalate")), rep(1, sc(t, "escalate", "answer")))
}

func concat(parts ...[]ScoredCase) []ScoredCase {
	var out []ScoredCase
	for _, p := range parts {
		out = append(out, p...)
	}
	return out
}

func assertMetricsFile(t *testing.T, name string, m map[string]any) {
	t.Helper()
	got, err := ToJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile("testdata/metrics_" + name + ".json")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("metrics_%s.json differs from Python:\n got: %s\nwant: %s", name, got, want)
	}
}

func value(t *testing.T, m map[string]any, path ...string) any {
	t.Helper()
	var cur any = m
	for _, p := range path {
		cur = cur.(map[string]any)[p]
	}
	return cur.(map[string]any)["value"]
}

// The escalation literals are Python 3.12 outputs pinned in the spec.
func TestComputeEscalationLiterals(t *testing.T) {
	m := runCompute(t, escalationFixture(t), "gold", ComputeOptions{})
	want := map[string]float64{"mcc": 0.6640828206858921, "balanced_accuracy": 0.7961980548187445,
		"f0_5": 0.7547169811320755, "f2": 0.6451612903225806, "f1": 0.6956521739130435}
	for k, w := range want {
		if got := value(t, m, "escalation", k); got != w {
			t.Errorf("escalation.%s = %v, want %v", k, got, w)
		}
	}
	assertMetricsFile(t, "escalation", m)
}

func TestComputeFixturesMatchPython(t *testing.T) {
	never := concat(rep(2, sc(t, "answer", "answer")), rep(2, sc(t, "ask", "answer")))
	unsupported := []ScoredCase{sc(t, "answer", "answer"), sc(t, "answer", "route")}
	support2 := concat(rep(2, sc(t, "ask", "ask")), rep(98, sc(t, "answer", "answer")))
	mixed := []ScoredCase{scFull(t, nil, "billing", "answer", false, "delivery", "ask"),
		scFull(t, nil, "praise", "route", false, "praise", "route")}
	cases := map[string][]ScoredCase{
		"never_predicted": never, "unsupported": unsupported, "all_one_class": rep(7, sc(t, "ask", "ask")),
		"empty": nil, "support2": support2, "mixed_intent": mixed,
		// 30 positives, 5 negatives: balanced_accuracy and mcc are thin, f-scores are not.
		"thin_negatives": concat(rep(30, sc(t, "escalate", "escalate")), rep(5, sc(t, "answer", "answer"))),
	}
	for name, cs := range cases {
		t.Run(name, func(t *testing.T) { assertMetricsFile(t, name, runCompute(t, cs, "gold", ComputeOptions{})) })
	}
	// test_empty_input: included_classes is [] (not null) when empty.
	empty := runCompute(t, nil, "gold", ComputeOptions{})
	for _, b := range []string{"action", "intent"} {
		for _, avg := range []string{"macro", "weighted"} {
			if ic, ok := empty[b].(map[string]any)[avg].(map[string]any)["included_classes"].([]string); !ok || ic == nil || len(ic) != 0 {
				t.Errorf("%s.%s.included_classes = %#v, want []string{}", b, avg, ic)
			}
		}
	}
	// The action fixture with test_run_metadata's config, coverage and operations.
	opts := ComputeOptions{
		Config: map[string]any{"threshold": 0.7, "prices": map[string]any{"cheap": 0.0, "larger": 0.1}},
		Coverage: map[string]any{"threads_routed": 5, "threads_scored": 4, "threads_without_gold": 1,
			"gold_rows_unmatched": 1},
		Operations: map[string]any{"tier_shares": map[string]any{"cheap": map[string]any{"value": 1.0}},
			"cost_per_1000_usd": 2.0, "prices_are_placeholders": true},
	}
	m := runCompute(t, actionFixture(t), "gold", opts)
	assertMetricsFile(t, "action", m)
	// Inputs are deep-copied: changing them afterwards leaves the output alone.
	opts.Config.(map[string]any)["prices"].(map[string]any)["cheap"] = 9.0
	opts.Coverage.(map[string]any)["threads_routed"] = 99
	assertMetricsFile(t, "action", m)
}

func TestComputeSyntheticIDs(t *testing.T) {
	results := []SyntheticResult{
		{ID: "c1", Expected: SyntheticExpected{Intent: "delivery", Action: "ask"}, Actual: SyntheticActual{Intent: "delivery", Action: "ask"}},
		{ID: "c2", Expected: SyntheticExpected{Intent: "other", Action: "escalate", Escalate: true}, Actual: SyntheticActual{Intent: "delivery", Action: "ask"}},
		{ID: "c3", Expected: SyntheticExpected{Intent: "delivery", Action: "ask"}, Actual: SyntheticActual{Intent: "delivery", Action: "ask"}},
	}
	cases, err := CasesFromSynthetic(results)
	if err != nil {
		t.Fatal(err)
	}
	if c := cases[1]; c.CaseID == nil || *c.CaseID != "c2" || c.GoldIntent != "other" || c.GoldAction != "escalate" ||
		!c.GoldEscalate || c.PredIntent != "delivery" || c.PredAction != "ask" {
		t.Errorf("cases[1] = %+v", c)
	}
	m := runCompute(t, cases, "synthetic", ComputeOptions{Sample: map[string]any{"size": nil, "seed": nil}})
	ids := m["action"].(map[string]any)["ids"].([]any)
	if got := ids[1].([]any)[1]; len(got.([]any)) != 2 {
		t.Errorf("ids[ask][ask] = %v", got)
	}
	// Full output, with Python's sample {size: 200, seed: 0}.
	assertMetricsFile(t, "synthetic", runCompute(t, cases, "synthetic", ComputeOptions{}))
	// A nil case id is a JSON null in its cell.
	nilID := []ScoredCase{scFull(t, nil, "other", "answer", false, "other", "answer")}
	assertMetricsFile(t, "synthetic_nil_id", runCompute(t, nilID, "synthetic", ComputeOptions{}))
}

// Messages are Python 3.12 outputs of the same calls.
func TestComputeRefusals(t *testing.T) {
	without := testGoldCohrt()
	delete(without, "split")
	extra := testGoldCohrt()
	extra["extra"] = 1
	with := func(k string, v any) map[string]any { m := testGoldCohrt(); m[k] = v; return m }
	const cohortKeys = "cohort must be an object with exactly the keys name, split, gold_revision, brand_population"
	const sampleKeys = "sample must be an object with exactly the keys size, seed"
	cases := []struct {
		kind string
		opts ComputeOptions
		want string
	}{
		{"gold", ComputeOptions{RunAt: "2026-11-18T10:00:00"}, "run_at must be an ISO 8601 UTC time ending in Z, got '2026-11-18T10:00:00'"},
		{"gold", ComputeOptions{RunAt: "2026-11-18T10:00:00+00:00"}, "run_at must be an ISO 8601 UTC time ending in Z, got '2026-11-18T10:00:00+00:00'"},
		{"gold", ComputeOptions{RunAt: "not a date Z"}, "run_at must be an ISO 8601 UTC time ending in Z, got 'not a date Z'"},
		{"gold", ComputeOptions{Cohort: without}, cohortKeys},
		{"gold", ComputeOptions{Cohort: extra}, cohortKeys},
		{"gold", ComputeOptions{Cohort: with("name", "synthetic")}, "cohort name 'synthetic' does not fit kind 'gold'"},
		{"gold", ComputeOptions{Cohort: with("gold_revision", "sha256:xyz")}, "cohort gold_revision must be 'sha256:' + 64 hex digits"},
		{"gold", ComputeOptions{Cohort: with("brand_population", "SomeBrand")}, "cohort brand_population must be 'all'"},
		{"gold", ComputeOptions{Sample: map[string]any{"size": 1}}, sampleKeys},
		{"gold", ComputeOptions{Sample: map[string]any{"size": 1, "seed": 2, "extra": 3}}, sampleKeys},
		{"weekly", ComputeOptions{}, "kind must be 'gold' or 'synthetic', got 'weekly'"},
		{"weekly", ComputeOptions{RunAt: "x"}, "kind must be 'gold' or 'synthetic', got 'weekly'"},
		{"gold", ComputeOptions{RunAt: "x", Cohort: map[string]any{}}, "run_at must be an ISO 8601 UTC time ending in Z, got 'x'"},
		{"gold", ComputeOptions{Cohort: map[string]any{}, Sample: map[string]any{}}, cohortKeys},
		{"synthetic", ComputeOptions{Cohort: with("name", "hard_case")}, "cohort name 'hard_case' does not fit kind 'synthetic'"},
		{"gold", ComputeOptions{Cohort: with("name", "hard_case")}, ""},
	}
	for _, c := range cases {
		opts := c.opts
		if opts.RunAt == "" {
			opts.RunAt = testRunAt
		}
		if opts.Cohort == nil {
			opts.Cohort = testGoldCohrt()
		}
		if opts.Sample == nil {
			opts.Sample = testSample()
		}
		_, err := Compute(nil, c.kind, opts)
		if got := errText(err); got != c.want {
			t.Errorf("Compute(%s, %+v) = %q, want %q", c.kind, c.opts, got, c.want)
		}
	}
}

// Python's ScoredCase.__post_init__ messages. gold_escalate is a Go bool, so
// "gold_escalate must be a bool" can't happen.
func TestScoredCaseRefusals(t *testing.T) {
	cases := []struct {
		gi, ga, pi, pa, want string
	}{
		{"other", "answer", "other", "shout", "unknown pred_action 'shout'"},
		{"other", "dance", "other", "answer", "unknown gold_action 'dance'"},
		{"weather", "answer", "other", "answer", "unknown gold_intent 'weather'"},
		{"other", "answer", "weather", "answer", "unknown pred_intent 'weather'"},
		{"weather", "answer", "other", "x", "unknown gold_intent 'weather'"},
	}
	for _, c := range cases {
		_, err := NewScoredCase(nil, c.gi, c.ga, false, c.pi, c.pa)
		if got := errText(err); got != c.want {
			t.Errorf("NewScoredCase(%v) = %q, want %q", c, got, c.want)
		}
	}
	// SyntheticResult is a struct, so Python's KeyError on a malformed result
	// becomes a decode error in the caller; a bad label is refused here.
	_, err := CasesFromSynthetic([]SyntheticResult{{ID: "x", Expected: SyntheticExpected{Intent: "other", Action: "nope"},
		Actual: SyntheticActual{Intent: "other", Action: "answer"}}})
	if got := errText(err); got != "unknown gold_action 'nope'" {
		t.Errorf("CasesFromSynthetic = %q", got)
	}
}

// test_gold_report_has_no_ids: one case per routed thread with a gold row, no
// case id, and no thread id anywhere in the output.
func TestCasesFromRoutedHasNoIDs(t *testing.T) {
	routed := []route.RoutedThread{
		rt(987654321, "billing", "ask", "cheap"),
		rt(987654322, "other", "escalate", "cheap"),
		rt(555, "other", "answer", "cheap"),
	}
	g, err := LoadGold(writeGold(t, goldHeader+"987654321,billing,ask,false\n987654322,other,escalate,true\n"))
	if err != nil {
		t.Fatal(err)
	}
	cases, err := CasesFromRouted(routed, g)
	if err != nil {
		t.Fatal(err)
	}
	if len(cases) != 2 || cases[0].CaseID != nil || cases[1].CaseID != nil {
		t.Fatalf("cases = %+v", cases)
	}
	if want := (ScoredCase{nil, "other", "escalate", true, "other", "escalate"}); cases[1] != want {
		t.Errorf("cases[1] = %+v, want %+v", cases[1], want)
	}
	text, err := ToJSON(runCompute(t, cases, "gold", ComputeOptions{}))
	if err != nil {
		t.Fatal(err)
	}
	for _, needle := range []string{"987654321", "987654322"} {
		if strings.Contains(string(text), needle) {
			t.Errorf("output holds %s", needle)
		}
	}
	id := "SECRETCASE_ALPHA"
	text, _ = ToJSON(runCompute(t, []ScoredCase{scFull(t, &id, "other", "answer", false, "other", "answer")}, "gold", ComputeOptions{}))
	if strings.Contains(string(text), "SECRETCASE") || strings.Contains(string(text), `"ids"`) {
		t.Errorf("gold output holds ids:\n%s", text)
	}
}
