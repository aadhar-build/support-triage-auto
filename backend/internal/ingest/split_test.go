package ingest

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

const stdCfg = `{"seed": 20261115, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`

func mustCfg(t *testing.T, s string) SplitConfig {
	t.Helper()
	cfg, err := ConfigFromJSON([]byte(s))
	if err != nil {
		t.Fatalf("config %s: %v", s, err)
	}
	return cfg
}

func TestAssignKnown(t *testing.T) {
	cfg := mustCfg(t, stdCfg)
	prefixes := map[uint64]string{1: "0102e5c497406f18", 2: "1c0fa2466aae53da"}
	for k, want := range prefixes {
		if got := fmt.Sprintf("%016x", hashPrefix(cfg.Seed, k)); got != want {
			t.Errorf("key %d prefix %s, want %s", k, got, want)
		}
	}
	want := map[uint64]string{1: "dev", 2: "eval", 3: "full", 42: "full", 1000000000000000: "full"}
	for k, w := range want {
		if got := AssignSplit(k, cfg); got != w {
			t.Errorf("AssignSplit(%d) = %s, want %s", k, got, w)
		}
	}
}

func TestSplitBoundaries(t *testing.T) {
	cfg := mustCfg(t, stdCfg)
	cases := []struct {
		u    float64
		want string
	}{
		{0.09999999999999999, "dev"},
		{0.1, "eval"},
		{0.15, "eval"},
		{0.15000000000000002, "full"},
	}
	for _, c := range cases {
		if got := splitForU(c.u, cfg); got != c.want {
			t.Errorf("u=%v: %s, want %s", c.u, got, c.want)
		}
	}
	if u := uFromN(2767011611056432640); u != 0.15 {
		t.Errorf("u(2767011611056432640) = %v, want 0.15", u)
	}
	if got := splitForU(uFromN(2767011611056432640), cfg); got != "eval" {
		t.Errorf("n=2767011611056432640: %s, want eval", got)
	}
	max := ^uint64(0)
	if u := uFromN(max); u != 1.0 {
		t.Errorf("u(2^64-1) = %v, want 1.0", u)
	}
	if got := splitForU(uFromN(max), cfg); got != "full" {
		t.Errorf("n=2^64-1: %s, want full", got)
	}
	full := mustCfg(t, `{"seed": 1, "unit": "thread", "fractions": {"dev": 0, "eval": 0, "full": 1}}`)
	dev := mustCfg(t, `{"seed": 1, "unit": "thread", "fractions": {"dev": 1, "eval": 0, "full": 0}}`)
	for k := uint64(0); k < 200; k++ {
		if got := AssignSplit(k, full); got != "full" {
			t.Fatalf("0/0/1 key %d: %s", k, got)
		}
		if got := AssignSplit(k, dev); got != "dev" {
			t.Fatalf("1/0/0 key %d: %s", k, got)
		}
	}
	if got := splitForU(1.0, dev); got != "full" {
		t.Errorf("1/0/0 at u=1.0: %s, want fall-through full", got)
	}
}

func TestConfigRejects(t *testing.T) {
	fr := func(f string) string { return `{"seed": 20261115, "unit": "thread", "fractions": {` + f + `}}` }
	bad := map[string]struct{ js, prefix string }{
		"not a dict":                 {`[["seed", 1]]`, "split config must be a JSON object"},
		"missing seed":               {`{"unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed is missing"},
		"string seed":                {`{"seed": "20261115", "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"float seed":                 {`{"seed": 1.0, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"bool seed":                  {`{"seed": true, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"seed 20261115.0":            {`{"seed": 20261115.0, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"seed 2.0261115e7":           {`{"seed": 2.0261115e7, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"seed outside int64":         {`{"seed": 99999999999999999999, "unit": "thread", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "seed must be an int"},
		"missing unit":               {`{"seed": 1, "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "unit must be 'thread'"},
		"wrong unit":                 {`{"seed": 1, "unit": "tweet", "fractions": {"dev": 0.1, "eval": 0.05, "full": 0.85}}`, "unit must be 'thread'"},
		"missing fractions":          {`{"seed": 1, "unit": "thread"}`, "fractions is missing"},
		"list fractions":             {`{"seed": 1, "unit": "thread", "fractions": [0.1, 0.05, 0.85]}`, "fractions must be an object"},
		"missing split name":         {fr(`"dev": 0.15, "full": 0.85`), "fractions missing split name(s)"},
		"extra split name":           {fr(`"dev": 0.1, "eval": 0.05, "full": 0.8, "test": 0.05`), "fractions has unknown split name(s)"},
		"string fraction":            {fr(`"dev": "0.1", "eval": 0.05, "full": 0.85`), "fraction 'dev' must be a number"},
		"bool fraction":              {fr(`"dev": false, "eval": true, "full": 0.0`), "fraction 'dev' must be a number"},
		"none fraction":              {fr(`"dev": null, "eval": 0.05, "full": 0.95`), "fraction 'dev' must be a number"},
		"negative fraction":          {fr(`"dev": -0.1, "eval": 0.25, "full": 0.85`), "fraction 'dev' must not be negative"},
		"NaN fraction":               {fr(`"dev": NaN, "eval": 0.05, "full": 0.85`), "split config is not valid JSON"},
		"infinite fraction":          {fr(`"dev": 1e400, "eval": 0.05, "full": 0.85`), "fraction 'dev' must be finite"},
		"sum below 1":                {fr(`"dev": 0.1, "eval": 0.05, "full": 0.8`), "fractions sum to"},
		"sum above 1":                {fr(`"dev": 0.1, "eval": 0.05, "full": 0.9`), "fractions sum to"},
		"sum just outside tolerance": {fr(`"dev": 0.1, "eval": 0.05, "full": 0.850001`), "fractions sum to"},
		"non-JSON":                   {`seed=1`, "split config is not valid JSON"},
	}
	for name, c := range bad {
		t.Run(name, func(t *testing.T) {
			_, err := ConfigFromJSON([]byte(c.js))
			var se *SplitConfigError
			if !errors.As(err, &se) {
				t.Fatalf("err = %v, want *SplitConfigError", err)
			}
			if !strings.HasPrefix(se.Error(), c.prefix) {
				t.Errorf("message %q, want prefix %q", se.Error(), c.prefix)
			}
		})
	}
}

func TestConfigMissingFile(t *testing.T) {
	_, err := LoadSplitConfig(filepath.Join(t.TempDir(), "nope.json"))
	var se *SplitConfigError
	if !errors.As(err, &se) || !strings.HasPrefix(se.Error(), "cannot read split config") {
		t.Fatalf("err = %v", err)
	}
}

func TestConfigAccepts(t *testing.T) {
	cfg := mustCfg(t, `{"seed": 7, "unit": "thread", "fractions": {"dev": 0.7, "eval": 0.2, "full": 0.1}}`)
	if cfg.Seed != 7 {
		t.Errorf("seed %d", cfg.Seed)
	}
	cfg = mustCfg(t, `{"seed": 7, "unit": "thread", "decided": "someone", "fractions": {"dev": 0, "eval": 0, "full": 1}}`)
	if !cfg.Fractions[2].IsInt || cfg.Fractions[2].Literal != "1" || cfg.Fractions[0].Name != "dev" {
		t.Errorf("fractions %+v", cfg.Fractions)
	}
}

func TestReportDedupAndEmpty(t *testing.T) {
	cfg := mustCfg(t, stdCfg)
	r := NewSplitter(cfg).Report()
	if r.Total != 0 {
		t.Errorf("total %d", r.Total)
	}
	for _, n := range SplitNames {
		if r.Shares[n] != 0.0 || r.Counts[n] != 0 {
			t.Errorf("%s: share %v count %d", n, r.Shares[n], r.Counts[n])
		}
	}
	m, rep := SplitThreads([]uint64{1, 2, 3, 1, 2, 1}, cfg)
	if rep.Total != 3 || len(m) != 3 {
		t.Fatalf("total %d, mapping %d, want 3", rep.Total, len(m))
	}
	if rep.Counts["dev"] != 1 || rep.Counts["eval"] != 1 || rep.Counts["full"] != 1 {
		t.Errorf("counts %v", rep.Counts)
	}
	if rep.Shares["dev"] != 1.0/3 || rep.Seed != 20261115 || rep.Targets[1].Value != 0.05 {
		t.Errorf("report %+v", rep)
	}
}

func TestReportOrderIndependent(t *testing.T) {
	cfg := mustCfg(t, stdCfg)
	var a, b []uint64
	for i := uint64(0); i < 500; i++ {
		a = append(a, i)
		b = append(b, 499-i)
	}
	ma, ra := SplitThreads(a, cfg)
	mb, rb := SplitThreads(b, cfg)
	for k, v := range ma {
		if mb[k] != v {
			t.Fatalf("root %d: %s vs %s", k, v, mb[k])
		}
	}
	if fmt.Sprint(ra.Counts) != fmt.Sprint(rb.Counts) {
		t.Errorf("counts differ: %v vs %v", ra.Counts, rb.Counts)
	}
}

// TestConfigSumMatchesPython312 pins the tolerance check to Python 3.12's
// compensated float sum(), which differs from naive (a+b)+c by one ulp at the
// 1e-9 boundary. Accept/reject verdicts were taken from python3 3.12.3.
func TestConfigSumMatchesPython312(t *testing.T) {
	fr := func(a, b, c string) string {
		return `{"seed": 1, "unit": "thread", "fractions": {"dev": ` + a + `, "eval": ` + b + `, "full": ` + c + `}}`
	}
	accept := [][3]string{{"0.1", "0.05", "0.850000001"}, {"0.4", "0.1", "0.499999999"}, {"1", "0.0", "0"}}
	for _, c := range accept {
		if _, err := ConfigFromJSON([]byte(fr(c[0], c[1], c[2]))); err != nil {
			t.Errorf("%v: %v, want accepted", c, err)
		}
	}
	// Ints are added without compensation, so the int mix sums differently
	// from three compensated floats.
	reject := map[[3]string]string{
		{"0.25", "0.3", "0.449999999"}:       "fractions sum to 0.9999999989999999,",
		{"1", "3.2847436310745673e-10", "1"}: "fractions sum to 2.000000000328474,",
	}
	for c, prefix := range reject {
		_, err := ConfigFromJSON([]byte(fr(c[0], c[1], c[2])))
		var ce *SplitConfigError
		if !errors.As(err, &ce) || !strings.HasPrefix(ce.Msg, prefix) {
			t.Errorf("%v: %v, want prefix %q", c, err, prefix)
		}
	}
}
