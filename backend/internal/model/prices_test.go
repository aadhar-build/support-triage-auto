package model

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

func fixturePrices(t *testing.T) *Prices {
	t.Helper()
	p, err := LoadPrices(filepath.Join(parity.RepoRoot(t), "tests/fixtures/model_prices.json"))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func i64(n int64) *int64     { return &n }
func f64(f float64) *float64 { return &f }

func TestFixtureBands(t *testing.T) {
	p := fixturePrices(t)
	if p.Version != "test-fixture" {
		t.Errorf("version %q", p.Version)
	}
	bands := p.Models["fixture-model"]
	if len(bands) != 2 {
		t.Fatalf("bands: %+v", bands)
	}
	b0, b1 := bands[0], bands[1]
	if b0.MaxInputTokens == nil || *b0.MaxInputTokens != 100000 || b0.Input != 1.0 || b0.Output != 5.0 ||
		b0.CacheWrite != nil || b0.CacheRead != nil {
		t.Errorf("band 0: %+v", b0)
	}
	if b1.MaxInputTokens != nil || b1.Input != 2.0 || b1.Output != 10.0 ||
		b1.CacheWrite == nil || *b1.CacheWrite != 2.5 || b1.CacheRead == nil || *b1.CacheRead != 0.2 {
		t.Errorf("band 1: %+v", b1)
	}
}

func TestCost(t *testing.T) {
	p := fixturePrices(t)
	if got, err := p.Cost("fixture-model", TokenUsage{1000, 50, 0, 0}); err != nil || got != 0.00125 {
		t.Errorf("cost 1000/50: %v, %v", got, err)
	}
	// Cache tokens count toward the band; band 2 has cache prices.
	if got, err := p.Cost("fixture-model", TokenUsage{100000, 0, 0, 1}); err != nil || got != 0.20000020000000002 {
		t.Errorf("cost band 2: %v, %v", got, err)
	}
	var ub *UnknownBillError
	_, err := p.Cost("other-model", TokenUsage{1, 1, 0, 0})
	if !errors.As(err, &ub) || err.Error() != "model 'other-model' has no price" {
		t.Errorf("unknown model: %v", err)
	}
	_, err = p.Cost("fixture-model", TokenUsage{10, 1, 5, 0})
	if !errors.As(err, &ub) || err.Error() != "model 'fixture-model' has no cache_write price in its band" {
		t.Errorf("no cache price: %v", err)
	}
}

func TestBandNoneFits(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	writeFile(t, path, `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": 10, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`)
	p, err := LoadPrices(path)
	if err != nil {
		t.Fatal(err)
	}
	if b, err := p.Band("m", 10); err != nil || *b.MaxInputTokens != 10 || b.Input != 1.0 {
		t.Errorf("band at limit: %+v, %v", b, err)
	}
	var ub *UnknownBillError
	if _, err := p.Band("m", 11); !errors.As(err, &ub) || err.Error() != "model 'm' has no band for 11 input tokens" {
		t.Errorf("no band: %v", err)
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

const band1 = `{"max_input_tokens": null, "input_usd_per_mtok": 2.0, "output_usd_per_mtok": 10.0}`

func TestMalformedPrices(t *testing.T) {
	cases := []struct{ name, content, problem string }{
		{"missing_file", "", "not found"},
		{"bad_json", "{not json", "not valid JSON"},
		{"bom", "\xef\xbb\xbf{}", "not valid JSON"},
		{"list", "[]", "top level is not an object"},
		{"missing_version", `{"models": {}}`, "version is missing or empty"},
		{"empty_version", `{"version": "", "models": {}}`, "version is missing or empty"},
		{"empty_models", `{"version": "v", "models": {}}`, "models is missing or empty"},
		{"models_list", `{"version": "v", "models": [1]}`, "models is missing or empty"},
		{"entry_not_object", `{"version": "v", "models": {"m": 1}}`, "model 'm' has no bands"},
		{"empty_bands", `{"version": "v", "models": {"m": {"bands": []}}}`, "model 'm' has no bands"},
		{"band_not_object", `{"version": "v", "models": {"m": {"bands": [3]}}}`, "model 'm' band 0 is not an object"},
		{"out_of_order", `{"version": "v", "models": {"m": {"bands": [` + band1 + `, {"max_input_tokens": 5, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: null max_input_tokens is only allowed last"},
		{"decreasing", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": 5, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}, {"max_input_tokens": 5, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 1: bands are out of order"},
		{"missing_limit", `{"version": "v", "models": {"m": {"bands": [{"input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: max_input_tokens must be a positive int or null"},
		{"float_limit", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": 100000.0, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: max_input_tokens must be a positive int or null"},
		{"exp_limit", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": 1e5, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: max_input_tokens must be a positive int or null"},
		{"bool_limit", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": true, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: max_input_tokens must be a positive int or null"},
		{"zero_limit", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": 0, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: max_input_tokens must be a positive int or null"},
		{"missing_input", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: input_usd_per_mtok is missing"},
		{"zero_price", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, "input_usd_per_mtok": 0, "output_usd_per_mtok": 1}]}}}`,
			"model 'm' band 0: input_usd_per_mtok must be a finite number > 0"},
		{"null_cache", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1, "cache_read_usd_per_mtok": null}]}}}`,
			"model 'm' band 0: cache_read_usd_per_mtok must be a finite number > 0"},
		{"string_price", `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, "input_usd_per_mtok": 1, "output_usd_per_mtok": "1"}]}}}`,
			"model 'm' band 0: output_usd_per_mtok must be a finite number > 0"},
		{"quote_name", `{"version": "v", "models": {"it's": {"bands": []}}}`, `model "it's" has no bands`},
	}
	dir := t.TempDir()
	for _, c := range cases {
		path := filepath.Join(dir, c.name+".json")
		if c.name != "missing_file" {
			writeFile(t, path, c.content)
		}
		_, err := LoadPrices(path)
		var pe *PricesError
		want := "price file " + path + ": " + c.problem
		if !errors.As(err, &pe) || err.Error() != want {
			t.Errorf("%s: got %v; want %s", c.name, err, want)
		}
	}
}

// TestRefusesNonStandardPrices: price-file parts of criterion 12.
func TestRefusesNonStandardPrices(t *testing.T) {
	limit := func(v string) string {
		return `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": ` + v + `, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}, ` + band1 + `]}}}`
	}
	input := func(v string) string {
		return `{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, "input_usd_per_mtok": ` + v + `, "output_usd_per_mtok": 1}]}}}`
	}
	const badLimit = "model 'm' band 0: max_input_tokens must be a positive int or null"
	const badInput = "model 'm' band 0: input_usd_per_mtok must be a finite number > 0"
	cases := []struct{ name, content, problem string }{
		{"nan", input("NaN"), "not valid JSON"},
		{"inf", input("Infinity"), "not valid JSON"},
		{"neginf", input("-Infinity"), "not valid JSON"},
		{"surrogate", `{"version": "\ud800", "models": {}}`, "not valid JSON"},
		{"limit_1e30", limit("1000000000000000000000000000000"), badLimit},
		{"limit_2_63", limit("9223372036854775808"), badLimit},
		{"input_1e30", input("1000000000000000000000000000000"), badInput},
		{"input_2_63", input("9223372036854775808"), badInput},
	}
	dir := t.TempDir()
	for _, c := range cases {
		path := filepath.Join(dir, c.name+".json")
		writeFile(t, path, c.content)
		_, err := LoadPrices(path)
		want := "price file " + path + ": " + c.problem
		if err == nil || err.Error() != want {
			t.Errorf("%s: got %v; want %s", c.name, err, want)
		}
	}
	for name, content := range map[string]string{"limit_max": limit("9223372036854775807"), "input_max": input("9223372036854775807")} {
		path := filepath.Join(dir, name+".json")
		writeFile(t, path, content)
		if _, err := LoadPrices(path); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}
