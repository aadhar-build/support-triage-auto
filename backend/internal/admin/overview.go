package admin

import (
	"path/filepath"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/report"
)

// headlines is admin.py's HEADLINES: block, key and label, in page order.
var headlines = [][3]string{
	{"escalation", "f2", "Escalation F2"}, {"escalation", "tpr", "Escalation TPR"},
	{"escalation", "tnr", "Escalation TNR"}, {"escalation", "precision", "Escalation precision"},
	{"escalation", "f1", "Escalation F1"}, {"escalation", "fnr", "Missed escalation rate"},
	{"escalation", "fpr", "False escalation rate"}, {"action", "accuracy", "Action accuracy"},
	{"intent", "accuracy", "Intent accuracy"},
}

// runMetrics is _metrics: the checked metrics of an ok run, or nil.
func runMetrics(r Run) map[string]any {
	if r.Status != StatusOK {
		return nil
	}
	m, err := report.LoadMetrics(filepath.Join(r.Dir, "metrics.json"))
	if err != nil {
		return nil
	}
	return m
}

// headlineRows returns the nine headline rows of checked metrics.
func headlineRows(m map[string]any) []any {
	rows := make([]any, len(headlines))
	for i, h := range headlines {
		p := m[h[0]].(map[string]any)[h[1]].(map[string]any)
		rows[i] = map[string]any{"label": h[2], "value": p["value"], "n": p["n"], "ci": p["ci"], "directional": p["directional"]}
	}
	return rows
}

// monthlyCap is _monthly_cap over the strictly read thresholds file.
func monthlyCap(path string) any {
	v, err := report.ReadJSON(path)
	if err != nil {
		return nil
	}
	for _, k := range []string{"guardrails", "api_spend_usd_per_month", "max"} {
		m, ok := v.(map[string]any)
		if !ok {
			return nil
		}
		if v, ok = m[k]; !ok {
			return nil
		}
	}
	switch v.(type) {
	case int64, float64:
		return v
	}
	return nil
}

// Overview is the data behind page_overview, with the clock injected.
func Overview(runsDir, thresholdsPath string, now time.Time) (map[string]any, error) {
	runs, err := ListRuns(runsDir)
	if err != nil {
		return nil, err
	}
	var ok []Run
	for _, r := range runs {
		if r.Status == StatusOK {
			ok = append(ok, r)
		}
	}
	out := map[string]any{"run_count": len(runs), "ok_count": len(ok), "latest": nil, "spend": nil}
	if len(ok) == 0 {
		return out, nil
	}
	latest := ok[0]
	l := map[string]any{"name": latest.Name, "kind": latest.Kind, "created_at": latest.CreatedAt, "headlines": nil}
	if m := runMetrics(latest); m != nil {
		l["headlines"] = headlineRows(m)
	}
	out["latest"] = l

	month := now.UTC().Format("2006-01")
	total, runsInMonth, noCost := 0.0, 0, 0
	placeholders := latest.Cost["prices_are_placeholders"] == true
	for _, r := range ok {
		if r.CreatedAt[:7] != month {
			continue
		}
		runsInMonth++
		switch x := r.Cost["estimated_usd"].(type) {
		case nil:
			noCost++
		case int64:
			total += float64(x)
		case float64:
			total += x
		}
		if r.Cost["prices_are_placeholders"] == true {
			placeholders = true
		}
	}
	out["spend"] = map[string]any{
		"this_run_usd": latest.Cost["estimated_usd"], "month": month,
		"month_total_usd": total, "month_runs": runsInMonth, "month_no_cost": noCost,
		"monthly_cap_usd": monthlyCap(thresholdsPath), "placeholder_prices": placeholders,
	}
	return out, nil
}
