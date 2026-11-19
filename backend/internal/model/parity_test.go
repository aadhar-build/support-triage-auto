package model

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

const inputs = "testdata/spend_log"

// Golden rendering mirrors scripts/golden_spend_log.py: every line is
// json.dumps(obj, sort_keys=True) (route.PyJSON) and floats are repr strings.

func line(t *testing.T, rec map[string]any) string {
	t.Helper()
	s, err := route.PyJSON(rec)
	if err != nil {
		t.Fatal(err)
	}
	return s + "\n"
}

func repr(f float64) string { return route.PyRepr(f) }

func errClass(err error) string {
	var ce *MonthlyCapError
	var se *SpendLogError
	var pe *PricesError
	var ub *UnknownBillError
	switch {
	case errors.As(err, &ce):
		return "MonthlyCapError"
	case errors.As(err, &se):
		return "SpendLogError"
	case errors.As(err, &pe):
		return "PricesError"
	case errors.As(err, &ub):
		return "UnknownBillError"
	}
	return fmt.Sprintf("%T", err)
}

// errRec renders an error with the log path, then the repo root, substituted.
func errRec(err error, log, repo string) map[string]any {
	msg := err.Error()
	if log != "" {
		msg = strings.ReplaceAll(msg, log, "<log>")
	}
	msg = strings.ReplaceAll(msg, repo, "<repo>")
	return map[string]any{"error": errClass(err), "message": msg}
}

func with(m map[string]any, k string, v any) map[string]any { m[k] = v; return m }

func listDir(t *testing.T, dir string) []string {
	t.Helper()
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range ents {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names
}

func renderBand(b Band) map[string]any {
	opt := func(p *float64) any {
		if p == nil {
			return nil
		}
		return repr(*p)
	}
	var limit any
	if b.MaxInputTokens != nil {
		limit = *b.MaxInputTokens
	}
	return map[string]any{"max_input_tokens": limit, "input": repr(b.Input), "output": repr(b.Output),
		"cache_write": opt(b.CacheWrite), "cache_read": opt(b.CacheRead)}
}

func clockAt(t *testing.T, s string) time.Time {
	t.Helper()
	c, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func parseUSD(t *testing.T, s string) float64 {
	t.Helper()
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func readJSON(t *testing.T, path string, v any) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, v); err != nil {
		t.Fatal(err)
	}
}

func TestPricesGolden(t *testing.T) {
	repo := parity.RepoRoot(t)
	t.Chdir(repo)
	var files []string
	for _, n := range listDir(t, inputs+"/prices") {
		files = append(files, inputs+"/prices/"+n)
	}
	files = append(files, "tests/fixtures/model_prices.json", "policies/model_prices.json")
	var usages struct {
		Usages []struct {
			Model string   `json:"model"`
			Usage [4]int64 `json:"usage"`
		} `json:"usages"`
	}
	readJSON(t, inputs+"/usages.json", &usages)
	var out strings.Builder
	for _, path := range files {
		p, err := LoadPrices(path)
		if err != nil {
			out.WriteString(line(t, with(errRec(err, "", repo), "file", path)))
			continue
		}
		names := make([]string, 0, len(p.Models))
		for m := range p.Models {
			names = append(names, m)
		}
		sort.Strings(names)
		var models []any
		for _, m := range names {
			var bands []any
			for _, b := range p.Models[m] {
				bands = append(bands, renderBand(b))
			}
			models = append(models, []any{m, bands})
		}
		out.WriteString(line(t, map[string]any{"file": path, "prices": map[string]any{"version": p.Version, "models": models}}))
		for _, u := range usages.Usages {
			usage := TokenUsage{u.Usage[0], u.Usage[1], u.Usage[2], u.Usage[3]}
			rec := map[string]any{"file": path, "model": u.Model,
				"usage": []any{u.Usage[0], u.Usage[1], u.Usage[2], u.Usage[3]}}
			if b, err := p.Band(u.Model, usage.InputTokens+usage.CacheWriteTokens+usage.CacheReadTokens); err != nil {
				rec["band"] = errRec(err, "", repo)
			} else {
				rec["band"] = renderBand(b)
			}
			if c, err := p.Cost(u.Model, usage); err != nil {
				rec["cost"] = errRec(err, "", repo)
			} else {
				rec["cost"] = repr(c)
			}
			out.WriteString(line(t, rec))
		}
	}
	parity.AssertGolden(t, "spend_log/prices.jsonl", []byte(out.String()))
}

func TestMonthlyCapGolden(t *testing.T) {
	repo := parity.RepoRoot(t)
	t.Chdir(repo)
	var files []string
	for _, n := range listDir(t, inputs+"/thresholds") {
		files = append(files, inputs+"/thresholds/"+n)
	}
	files = append(files, "evals/thresholds.json", "tests/fixtures/thresholds_spend.json")
	var out strings.Builder
	for _, path := range files {
		if v, err := LoadMonthlyCap(path); err != nil {
			out.WriteString(line(t, with(errRec(err, "", repo), "file", path)))
		} else {
			out.WriteString(line(t, map[string]any{"file": path, "value": repr(v)}))
		}
	}
	parity.AssertGolden(t, "spend_log/caps.jsonl", []byte(out.String()))
}

type scenarioStep struct {
	Op     string           `json:"op"`
	Clock  string           `json:"clock"`
	RunID  string           `json:"run_id"`
	Model  string           `json:"model"`
	Split  string           `json:"split"`
	USD    string           `json:"usd"`
	Ref    *int             `json:"ref"`
	ID     string           `json:"id"`
	Tokens map[string]int64 `json:"tokens"`
}

func TestScenarioGolden(t *testing.T) {
	repo := parity.RepoRoot(t)
	t.Chdir(repo)
	var spec struct {
		Cap   string         `json:"cap"`
		Steps []scenarioStep `json:"steps"`
	}
	readJSON(t, inputs+"/scenario.json", &spec)
	log := filepath.Join(realTemp(t), "scenario.jsonl")
	capUSD := parseUSD(t, spec.Cap)
	ids := counterIDs()
	results := make([]string, len(spec.Steps))
	var out strings.Builder
	for i, st := range spec.Steps {
		clock := clockAt(t, st.Clock)
		opts := SpendLogOptions{Clock: func() time.Time { return clock }, NewID: ids}
		l, err := OpenSpendLog(repo, log, capUSD, opts)
		if err != nil {
			t.Fatal(err)
		}
		rid := st.ID
		if st.Ref != nil {
			rid = results[*st.Ref]
		}
		rec := map[string]any{"step": int64(i), "op": st.Op}
		var res any
		switch st.Op {
		case "create":
			err = CreateSpendLog(repo, log, opts)
		case "reserve":
			var id string
			id, err = l.Reserve(st.RunID, st.Model, st.Split, parseUSD(t, st.USD))
			res = id
			results[i] = id
		case "settle":
			tk := st.Tokens
			err = l.Settle(rid, parseUSD(t, st.USD), TokenUsage{tk["input_tokens"], tk["output_tokens"], tk["cache_write_tokens"], tk["cache_read_tokens"]})
		case "mark_unknown":
			err = l.MarkUnknown(rid)
		default:
			var m float64
			m, err = l.MonthToDate()
			res = repr(m)
		}
		if err != nil {
			for k, v := range errRec(err, log, repo) {
				rec[k] = v
			}
		} else {
			rec["result"] = res
		}
		if m, err := l.MonthToDate(); err != nil {
			rec["mtd"] = errRec(err, log, repo)
		} else {
			rec["mtd"] = repr(m)
		}
		out.WriteString(line(t, rec))
	}
	got, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	parity.AssertGolden(t, "spend_log/scenario_log.jsonl", got)
	parity.AssertGolden(t, "spend_log/scenario_steps.jsonl", []byte(out.String()))
}

func TestReadGolden(t *testing.T) {
	repo := parity.RepoRoot(t)
	t.Chdir(repo)
	var spec struct {
		Clocks  []string `json:"clocks"`
		Cap     string   `json:"cap"`
		Reserve struct {
			RunID string `json:"run_id"`
			Model string `json:"model"`
			Split string `json:"split"`
			USD   string `json:"usd"`
		} `json:"reserve"`
	}
	readJSON(t, inputs+"/read_clocks.json", &spec)
	capUSD := parseUSD(t, spec.Cap)
	dir := realTemp(t)
	var out strings.Builder
	for _, name := range listDir(t, inputs+"/logs") {
		src, err := os.ReadFile(inputs + "/logs/" + name)
		if err != nil {
			t.Fatal(err)
		}
		log := filepath.Join(dir, "read.jsonl")
		if err := os.WriteFile(log, src, 0o600); err != nil {
			t.Fatal(err)
		}
		ids := counterIDs()
		open := func(c string) *SpendLog {
			clock := clockAt(t, c)
			l, err := OpenSpendLog(repo, log, capUSD, SpendLogOptions{Clock: func() time.Time { return clock }, NewID: ids})
			if err != nil {
				t.Fatal(err)
			}
			return l
		}
		for _, c := range spec.Clocks {
			rec := map[string]any{"log": name, "clock": c}
			if m, err := open(c).MonthToDate(); err != nil {
				for k, v := range errRec(err, log, repo) {
					rec[k] = v
				}
			} else {
				rec["mtd"] = repr(m)
			}
			out.WriteString(line(t, rec))
			if after, _ := os.ReadFile(log); !bytes.Equal(after, src) {
				t.Errorf("%s: MonthToDate changed the file", name)
			}
		}
		rv := spec.Reserve
		rec := map[string]any{"log": name}
		id, err := open(spec.Clocks[0]).Reserve(rv.RunID, rv.Model, rv.Split, parseUSD(t, rv.USD))
		after, _ := os.ReadFile(log)
		if err != nil {
			rec["reserve"] = errRec(err, log, repo)
			if !bytes.Equal(after, src) {
				t.Errorf("%s: refused Reserve changed the file", name)
			}
		} else {
			rec["reserve"] = id
		}
		rec["changed"] = !bytes.Equal(after, src)
		out.WriteString(line(t, rec))
		if err := os.Remove(log); err != nil {
			t.Fatal(err)
		}
	}
	parity.AssertGolden(t, "spend_log/read.jsonl", []byte(out.String()))
}
