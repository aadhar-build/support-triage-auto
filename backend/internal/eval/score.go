package eval

import (
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// TierNames are the built-in tiers, in report order.
var TierNames = []string{"cheap", "larger", "agent"}

// LargerFallbackReasons are Python's LARGER_FALLBACK_REASONS, in order.
var LargerFallbackReasons = []string{"spend_cap", "monthly_cap", "spend_log", "unknown_bill",
	"reservation_exceeded", "api_error", "too_long", "parse_error", "invalid_labels"}

// TierPrice is one tier's per-call price. Prices are ordered, as the
// Markdown price rows follow Python's dict insertion order.
type TierPrice struct {
	Tier  string
	Price float64
}

// FallbackCount is one larger-tier fallback reason and its count.
type FallbackCount struct {
	Reason string
	Count  int
}

// LargerUsage holds the report's larger-tier spend fields.
type LargerUsage struct {
	LargerMode                                                                           string
	LargerModel, LargerPromptVersion                                                     *string
	LargerPostMergeEval                                                                  bool
	LargerInputTokens, LargerOutputTokens, LargerCacheWriteTokens, LargerCacheReadTokens int
	LargerConfirmedUSD, LargerUnconfirmedUSD                                             float64
	MaxSpendUSD, MonthlyCapUSD, MonthToDateUSD                                           *float64
	LargerStoppedBy                                                                      *string
	LargerAnswered, LargerReservationExceeded                                            int
	LargerFallbacks                                                                      []FallbackCount
}

func zeroFallbacks() []FallbackCount {
	fb := make([]FallbackCount, len(LargerFallbackReasons))
	for i, r := range LargerFallbackReasons {
		fb[i] = FallbackCount{r, 0}
	}
	return fb
}

// DefaultLargerUsage is Python's Report dataclass defaults for these fields.
func DefaultLargerUsage() LargerUsage {
	return LargerUsage{LargerMode: "fake", LargerFallbacks: zeroFallbacks()}
}

// TierShare is one tier's share of routed threads and its interval.
type TierShare struct {
	Tier  string
	Share *float64
	CI    []float64 // nil when there is no interval
}

// Report is Python's evaluate.Report. A nil []float64 interval is null.
type Report struct {
	ThreadsRouted, ThreadsScored, ThreadsWithoutGold, GoldRowsUnmatched int
	ActionAccuracy, IntentAccuracy                                      *float64
	MissedEscalations                                                   int
	MissedEscalationRate                                                *float64
	FalseEscalations                                                    int
	FalseEscalationRate, NeedsPersonShare                               *float64
	TierShares                                                          []TierShare
	CostPer1000Threads                                                  *float64
	Prices                                                              []TierPrice
	PricesArePlaceholders                                               bool
	SampleSize, Seed                                                    *int64
	Split                                                               *string
	Threshold                                                           *float64
	ActionAccuracyCI, IntentAccuracyCI, MissedEscalationRateCI          []float64
	FalseEscalationRateCI, NeedsPersonShareCI                           []float64
	LargerUsage
}

// ScoreOptions are score's keyword arguments.
type ScoreOptions struct {
	SampleSize, Seed *int64
	Split            *string
	Threshold        *float64
	Usage            *LargerUsage
}

func rate(num, den int) *float64 {
	if den == 0 {
		return nil
	}
	v := float64(num) / float64(den)
	return &v
}

func wilson(s, n int) []float64 {
	low, high, ok := WilsonInterval(s, n)
	if !ok {
		return nil
	}
	return []float64{low, high}
}

// placeholderOrder is the order of route.PlaceholderPrices's tiers.
func placeholderPrices() []TierPrice {
	out := make([]TierPrice, len(TierNames))
	for i, t := range TierNames {
		out[i] = TierPrice{t, route.PlaceholderPrices[t]}
	}
	return out
}

// Score scores routed threads against gold, as Python's evaluate.score.
func Score(routed []route.RoutedThread, gold Gold, prices []TierPrice, opts ScoreOptions) (Report, error) {
	if prices == nil {
		prices = placeholderPrices()
	}
	priceOf := map[string]float64{}
	for _, p := range prices {
		if _, dup := priceOf[p.Tier]; dup {
			return Report{}, fmt.Errorf("price for tier %s given twice", route.PyRepr(p.Tier))
		}
		priceOf[p.Tier] = p.Price
	}
	tierNames := slices.Clone(TierNames)
	for _, r := range routed {
		for _, t := range r.Tiers {
			if !slices.Contains(tierNames, t) {
				tierNames = append(tierNames, t)
			}
		}
	}
	for _, t := range tierNames {
		if _, ok := priceOf[t]; !ok {
			return Report{}, fmt.Errorf("no price given for tier %s", route.PyRepr(t))
		}
	}
	for _, p := range prices {
		if err := route.CheckPrice(p.Tier, p.Price); err != nil {
			return Report{}, err
		}
	}

	n := len(routed)
	routedIDs := map[string]bool{}
	scored, actionOK, intentOK, escN, noEscN, missed, falseEsc, escalated := 0, 0, 0, 0, 0, 0, 0, 0
	for _, r := range routed {
		routedIDs[strconv.FormatUint(r.ThreadID, 10)] = true
		if r.Labels.Action == "escalate" {
			escalated++
		}
		g, ok := gold.Lookup(r.ThreadID)
		if !ok {
			continue
		}
		scored++
		if r.Labels.Action == g.Action {
			actionOK++
		}
		if r.Labels.Intent == g.Intent {
			intentOK++
		}
		if g.ShouldEscalate {
			escN++
			if r.Labels.Action != "escalate" {
				missed++
			}
		} else {
			noEscN++
			if r.Labels.Action == "escalate" {
				falseEsc++
			}
		}
	}
	unmatched := 0
	for _, id := range gold.ids {
		if !routedIDs[id] {
			unmatched++
		}
	}
	shares := make([]TierShare, len(tierNames))
	for i, t := range tierNames {
		c := 0
		for _, r := range routed {
			if slices.Contains(r.Tiers, t) {
				c++
			}
		}
		shares[i] = TierShare{t, rate(c, n), wilson(c, n)}
	}
	// Summed one price at a time, in routed then tier order, never count * price.
	var prs []float64
	for _, r := range routed {
		for _, t := range r.Tiers {
			prs = append(prs, priceOf[t])
		}
	}
	total := pySum(prs)
	var cost *float64
	if n != 0 {
		v := 1000 * total / float64(n)
		cost = &v
	}
	placeholders := len(prices) == len(route.PlaceholderPrices)
	for _, p := range prices {
		v, ok := route.PlaceholderPrices[p.Tier]
		placeholders = placeholders && ok && v == p.Price
	}
	rep := Report{
		ThreadsRouted: n, ThreadsScored: scored, ThreadsWithoutGold: n - scored,
		GoldRowsUnmatched: unmatched,
		ActionAccuracy:    rate(actionOK, scored), IntentAccuracy: rate(intentOK, scored),
		MissedEscalations: missed, MissedEscalationRate: rate(missed, escN),
		FalseEscalations: falseEsc, FalseEscalationRate: rate(falseEsc, noEscN),
		NeedsPersonShare: rate(escalated, n), TierShares: shares, CostPer1000Threads: cost,
		Prices: slices.Clone(prices), PricesArePlaceholders: placeholders,
		SampleSize: opts.SampleSize, Seed: opts.Seed, Split: opts.Split, Threshold: opts.Threshold,
		ActionAccuracyCI: wilson(actionOK, scored), IntentAccuracyCI: wilson(intentOK, scored),
		MissedEscalationRateCI: wilson(missed, escN), FalseEscalationRateCI: wilson(falseEsc, noEscN),
		NeedsPersonShareCI: wilson(escalated, n),
		LargerUsage:        DefaultLargerUsage(),
	}
	if opts.Usage != nil {
		u := *opts.Usage
		// Python updates the zero defaults in place and appends unknown reasons.
		fb := zeroFallbacks()
		for _, f := range u.LargerFallbacks {
			i := slices.IndexFunc(fb, func(x FallbackCount) bool { return x.Reason == f.Reason })
			if i >= 0 {
				fb[i].Count = f.Count
			} else {
				fb = append(fb, f)
			}
		}
		u.LargerFallbacks = fb
		rep.LargerUsage = u
	}
	return rep, nil
}

func floatOrNil(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}

func strOrNil(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

func int64OrNil(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

func ciOrNil(ci []float64) any {
	if ci == nil {
		return nil
	}
	return []any{ci[0], ci[1]}
}

// ReportToJSON is json.dumps(asdict(report), sort_keys=True, indent=2) + "\n".
func ReportToJSON(r Report) ([]byte, error) {
	shares, sharesCI, prices, fb := map[string]any{}, map[string]any{}, map[string]any{}, map[string]any{}
	for _, s := range r.TierShares {
		shares[s.Tier] = floatOrNil(s.Share)
		sharesCI[s.Tier] = ciOrNil(s.CI)
	}
	for _, p := range r.Prices {
		prices[p.Tier] = p.Price
	}
	for _, f := range r.LargerFallbacks {
		fb[f.Reason] = f.Count
	}
	m := map[string]any{
		"threads_routed": r.ThreadsRouted, "threads_scored": r.ThreadsScored,
		"threads_without_gold": r.ThreadsWithoutGold, "gold_rows_unmatched": r.GoldRowsUnmatched,
		"action_accuracy": floatOrNil(r.ActionAccuracy), "intent_accuracy": floatOrNil(r.IntentAccuracy),
		"missed_escalations": r.MissedEscalations, "missed_escalation_rate": floatOrNil(r.MissedEscalationRate),
		"false_escalations": r.FalseEscalations, "false_escalation_rate": floatOrNil(r.FalseEscalationRate),
		"needs_person_share": floatOrNil(r.NeedsPersonShare), "tier_shares": shares,
		"cost_per_1000_threads": floatOrNil(r.CostPer1000Threads), "prices": prices,
		"prices_are_placeholders": r.PricesArePlaceholders,
		"sample_size":             int64OrNil(r.SampleSize), "seed": int64OrNil(r.Seed),
		"split": strOrNil(r.Split), "threshold": floatOrNil(r.Threshold),
		"action_accuracy_ci": ciOrNil(r.ActionAccuracyCI), "intent_accuracy_ci": ciOrNil(r.IntentAccuracyCI),
		"missed_escalation_rate_ci": ciOrNil(r.MissedEscalationRateCI),
		"false_escalation_rate_ci":  ciOrNil(r.FalseEscalationRateCI),
		"needs_person_share_ci":     ciOrNil(r.NeedsPersonShareCI), "tier_shares_ci": sharesCI,
		"larger_mode": r.LargerMode, "larger_model": strOrNil(r.LargerModel),
		"larger_prompt_version":  strOrNil(r.LargerPromptVersion),
		"larger_post_merge_eval": r.LargerPostMergeEval,
		"larger_input_tokens":    r.LargerInputTokens, "larger_output_tokens": r.LargerOutputTokens,
		"larger_cache_write_tokens": r.LargerCacheWriteTokens, "larger_cache_read_tokens": r.LargerCacheReadTokens,
		"larger_confirmed_usd": r.LargerConfirmedUSD, "larger_unconfirmed_usd": r.LargerUnconfirmedUSD,
		"max_spend_usd": floatOrNil(r.MaxSpendUSD), "monthly_cap_usd": floatOrNil(r.MonthlyCapUSD),
		"month_to_date_usd": floatOrNil(r.MonthToDateUSD), "larger_stopped_by": strOrNil(r.LargerStoppedBy),
		"larger_answered": r.LargerAnswered, "larger_reservation_exceeded": r.LargerReservationExceeded,
		"larger_fallbacks": fb,
	}
	b, err := route.PyJSONIndent(m)
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

// pyFmt is Python's _fmt.
func pyFmt(v any) string {
	switch x := v.(type) {
	case nil:
		return "n/a"
	case bool:
		if x {
			return "yes"
		}
		return "no"
	case float64:
		return fmt.Sprintf("%.4f", x)
	case string:
		return x
	}
	return route.PyRepr(v)
}

func fmtRate(r *float64, ci []float64) string {
	if r == nil || ci == nil {
		return "n/a"
	}
	return fmt.Sprintf("%.4f (%.4f to %.4f)", *r, ci[0], ci[1])
}

type mdRow struct {
	k string
	v any
}

// ReportToMarkdown is Python's report_to_markdown.
func ReportToMarkdown(r Report) string {
	lines := []string{"# Triage eval report", ""}
	if r.PricesArePlaceholders {
		lines = append(lines, "> Prices are placeholders, not real prices. Do not quote the cost figure as a real cost.", "")
	}
	lines = append(lines, "| metric | value |", "| --- | --- |")
	rows := []mdRow{
		{"split", strOrNil(r.Split)}, {"sample_size", int64OrNil(r.SampleSize)}, {"seed", int64OrNil(r.Seed)},
		{"threshold", floatOrNil(r.Threshold)},
		{"threads_routed", r.ThreadsRouted}, {"threads_scored", r.ThreadsScored},
		{"threads_without_gold", r.ThreadsWithoutGold}, {"gold_rows_unmatched", r.GoldRowsUnmatched},
		{"action_accuracy", fmtRate(r.ActionAccuracy, r.ActionAccuracyCI)},
		{"intent_accuracy", fmtRate(r.IntentAccuracy, r.IntentAccuracyCI)},
		{"missed_escalations", r.MissedEscalations},
		{"missed_escalation_rate", fmtRate(r.MissedEscalationRate, r.MissedEscalationRateCI)},
		{"false_escalations", r.FalseEscalations},
		{"false_escalation_rate", fmtRate(r.FalseEscalationRate, r.FalseEscalationRateCI)},
		{"needs_person_share", fmtRate(r.NeedsPersonShare, r.NeedsPersonShareCI)},
	}
	for _, s := range r.TierShares {
		rows = append(rows, mdRow{"tier_share." + s.Tier, fmtRate(s.Share, s.CI)})
	}
	rows = append(rows, mdRow{"cost_per_1000_threads", floatOrNil(r.CostPer1000Threads)})
	for _, p := range r.Prices {
		rows = append(rows, mdRow{"price." + p.Tier, p.Price})
	}
	rows = append(rows, mdRow{"prices_are_placeholders", r.PricesArePlaceholders})
	for _, row := range rows {
		lines = append(lines, "| "+row.k+" | "+pyFmt(row.v)+" |")
	}
	if r.LargerMode == "real" {
		lines = append(lines, "", "## Model spend (API-equivalent USD)", "",
			"Tokens each call reported, times the owner's list prices. Not a bill.", "",
			"| item | value |", "| --- | --- |")
		stopped := "none"
		if r.LargerStoppedBy != nil && *r.LargerStoppedBy != "" {
			stopped = *r.LargerStoppedBy
		}
		spend := []mdRow{
			{"larger_model", strOrNil(r.LargerModel)}, {"larger_prompt_version", strOrNil(r.LargerPromptVersion)},
			{"larger_post_merge_eval", r.LargerPostMergeEval},
			{"confirmed_usd", r.LargerConfirmedUSD}, {"unconfirmed_usd", r.LargerUnconfirmedUSD},
			{"max_spend_usd (per run cap)", floatOrNil(r.MaxSpendUSD)},
			{"monthly_cap_usd", floatOrNil(r.MonthlyCapUSD)}, {"month_to_date_usd", floatOrNil(r.MonthToDateUSD)},
			{"stopped_by", stopped}, {"answered", r.LargerAnswered},
			{"reservation_exceeded", r.LargerReservationExceeded},
			{"input_tokens", r.LargerInputTokens}, {"output_tokens", r.LargerOutputTokens},
			{"cache_write_tokens", r.LargerCacheWriteTokens}, {"cache_read_tokens", r.LargerCacheReadTokens},
		}
		for _, f := range r.LargerFallbacks {
			spend = append(spend, mdRow{"fallback." + f.Reason, f.Count})
		}
		for _, row := range spend {
			lines = append(lines, "| "+row.k+" | "+pyFmt(row.v)+" |")
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

// pySum is CPython 3.12's builtin sum() over floats starting from int 0: a
// Neumaier compensated sum, with the compensation added once at the end.
func pySum(xs []float64) float64 {
	f, c := 0.0, 0.0
	for _, x := range xs {
		t := f + x
		if math.Abs(f) >= math.Abs(x) {
			c += (f - t) + x
		} else {
			c += (x - t) + f
		}
		f = t
	}
	if c != 0 && !math.IsInf(c, 0) && !math.IsNaN(c) {
		f += c
	}
	return f
}
