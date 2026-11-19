package report

import (
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/eval"
	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// Python's key tuples from triage.report that have no Go counterpart yet.
var (
	TopKeys = []string{"schema_version", "kind", "run_at", "cohort", "sample", "config", "coverage",
		"operations", "n", "directional_below", "averaging", "escalation", "action", "intent"}
	CountKeys      = []string{"tp", "fp", "tn", "fn", "positives", "predicted_positives"}
	EscProportions = []string{"tpr", "tnr", "fpr", "fnr", "precision", "npv", "accuracy", "prevalence"}
	EscScores      = []string{"f1", "f2", "f0_5", "balanced_accuracy", "mcc"}

	blockKeys           = []string{"confusion_matrix", "per_class", "macro", "weighted", "accuracy", "kappa"}
	proportionKeys      = []string{"value", "num", "n", "ci", "directional"}
	scoreKeys           = []string{"value", "n", "ci", "directional"}
	perClassCounts      = []string{"support", "predicted", "tp", "fp", "tn", "fn"}
	perClassProportions = []string{"tpr", "tnr", "precision"}
	perClassScores      = []string{"f1", "f2", "f0_5"}
	configKeys          = []string{"threshold", "prices"}
	coverageKeys        = []string{"threads_routed", "threads_scored", "threads_without_gold", "gold_rows_unmatched"}
	operationsKeys      = []string{"tier_shares", "cost_per_1000_usd", "prices_are_placeholders"}
)

// LoadMetrics is report.load_metrics: read the file strictly, then check it.
// Values shown with %r in a message keep the file's key order.
func LoadMetrics(path string) (map[string]any, error) {
	pv, v, err := readJSON(path)
	if err != nil {
		return nil, err
	}
	return checkMetrics(v, &pv)
}

// CheckMetrics is report.check_metrics on plain Go values. It returns a copy
// rebuilt from checked fields, or Python's ReportError message. Objects in a
// %r message are shown with sorted keys, since a Go map has no order.
func CheckMetrics(v any) (map[string]any, error) {
	return checkMetrics(v, nil)
}

func fail(format string, a ...any) error { return errors.New(fmt.Sprintf(format, a...)) }

// child returns pv[key] when pv is an object holding key, else nil.
func child(pv *policy.Value, key string) *policy.Value {
	if pv == nil {
		return nil
	}
	c, ok := pv.Get(key)
	if !ok {
		return nil
	}
	return &c
}

// repr is Python %r of v. With the parsed value at hand, objects keep their
// file key order; otherwise route.PyRepr (sorted keys) is used.
func repr(v any, pv *policy.Value) string {
	if pv == nil {
		return route.PyRepr(v)
	}
	switch pv.Kind {
	case policy.Object:
		parts := make([]string, len(pv.Keys))
		for i, k := range pv.Keys {
			c := pv.Vals[k]
			cv, _ := convert(c)
			parts[i] = route.PyRepr(k) + ": " + repr(cv, &c)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case policy.Array:
		parts := make([]string, len(pv.Items))
		for i := range pv.Items {
			cv, _ := convert(pv.Items[i])
			parts[i] = repr(cv, &pv.Items[i])
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	return route.PyRepr(v)
}

// pyStr is Python str(): a string bare, anything else as repr.
func pyStr(v any, pv *policy.Value) string {
	if s, ok := v.(string); ok {
		return s
	}
	return repr(v, pv)
}

// keysOf lists m's keys in file order when known, else sorted.
func keysOf(m map[string]any, pv *policy.Value) []string {
	if pv != nil && pv.Kind == policy.Object {
		return pv.Keys
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func sameKeys(m map[string]any, keys []string) bool {
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

func unknownKeys(m map[string]any, allowed []string) []string {
	var out []string
	for k := range m {
		if !slices.Contains(allowed, k) {
			out = append(out, k)
		}
	}
	slices.Sort(out)
	return out
}

func obj(v any, keys []string, where string) (map[string]any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("%s must be an object", where)
	}
	if u := unknownKeys(m, keys); len(u) > 0 {
		return nil, fail("%s has unknown key %s", where, strings.Join(u, ", "))
	}
	var missing []string
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		return nil, fail("%s lacks %s", where, strings.Join(missing, ", "))
	}
	return m, nil
}

func count(v any, where string) (any, error) {
	if n, ok := v.(int64); ok && n >= 0 {
		return n, nil
	}
	return nil, fail("%s must be a non-negative int", where)
}

func intOrNull(v any, where string) (any, error) {
	switch v.(type) {
	case nil, int64:
		return v, nil
	}
	return nil, fail("%s must be an int or null", where)
}

func number(v any, where string, nullable bool) (any, error) {
	switch v.(type) {
	case int64, float64:
		return v, nil
	case nil:
		if nullable {
			return nil, nil
		}
	}
	if nullable {
		return nil, fail("%s must be a number or null", where)
	}
	return nil, fail("%s must be a number", where)
}

func flag(v any, where string) (any, error) {
	if _, ok := v.(bool); ok {
		return v, nil
	}
	return nil, fail("%s must be true or false", where)
}

func text(v any, where string, nullable bool) (any, error) {
	if _, ok := v.(string); ok {
		return v, nil
	}
	if nullable {
		if v == nil {
			return nil, nil
		}
		return nil, fail("%s must be a string or null", where)
	}
	return nil, fail("%s must be a string", where)
}

func ci(v any, where string) (any, error) {
	if v == nil {
		return nil, nil
	}
	l, ok := v.([]any)
	if !ok || len(l) != 2 {
		return nil, fail("%s must be null or [low, high]", where)
	}
	out := make([]any, 2)
	for i, x := range l {
		n, err := number(x, where, false)
		if err != nil {
			return nil, err
		}
		out[i] = n
	}
	return out, nil
}

// fields runs each check in order and stops at the first error.
type field struct {
	key   string
	check func() (any, error)
}

func build(out map[string]any, fs ...field) (map[string]any, error) {
	for _, f := range fs {
		v, err := f.check()
		if err != nil {
			return nil, err
		}
		out[f.key] = v
	}
	return out, nil
}

func proportion(v any, where string) (any, error) {
	p, err := obj(v, proportionKeys, where)
	if err != nil {
		return nil, err
	}
	return build(map[string]any{},
		field{"value", func() (any, error) { return number(p["value"], where+".value", true) }},
		field{"num", func() (any, error) { return count(p["num"], where+".num") }},
		field{"n", func() (any, error) { return count(p["n"], where+".n") }},
		field{"ci", func() (any, error) { return ci(p["ci"], where+".ci") }},
		field{"directional", func() (any, error) { return flag(p["directional"], where+".directional") }},
	)
}

func score(v any, where string, averaged bool) (any, error) {
	keys := scoreKeys
	if averaged {
		keys = append(slices.Clone(scoreKeys), "zero_filled")
	}
	p, err := obj(v, keys, where)
	if err != nil {
		return nil, err
	}
	fs := []field{
		{"value", func() (any, error) { return number(p["value"], where+".value", true) }},
		{"n", func() (any, error) { return count(p["n"], where+".n") }},
		{"ci", func() (any, error) { return ci(p["ci"], where+".ci") }},
		{"directional", func() (any, error) { return flag(p["directional"], where+".directional") }},
	}
	if averaged {
		fs = append(fs, field{"zero_filled", func() (any, error) { return count(p["zero_filled"], where+".zero_filled") }})
	}
	return build(map[string]any{}, fs...)
}

func isGrid(grid any, k int, cellOK func(any) bool) bool {
	rows, ok := grid.([]any)
	if !ok || len(rows) != k {
		return false
	}
	for _, r := range rows {
		cells, ok := r.([]any)
		if !ok || len(cells) != k {
			return false
		}
		for _, c := range cells {
			if !cellOK(c) {
				return false
			}
		}
	}
	return true
}

func hasIDs(v any) bool {
	switch x := v.(type) {
	case map[string]any:
		if _, ok := x["ids"]; ok {
			return true
		}
		for _, c := range x {
			if hasIDs(c) {
				return true
			}
		}
	case []any:
		for _, c := range x {
			if hasIDs(c) {
				return true
			}
		}
	}
	return false
}

func checkBlock(name string, v any, pv *policy.Value, vocab []string, synthetic bool) (any, error) {
	keys := blockKeys
	if synthetic {
		keys = append(slices.Clone(blockKeys), "ids")
	}
	b, err := obj(v, keys, name)
	if err != nil {
		return nil, err
	}
	pcm := child(pv, "confusion_matrix")
	cm, err := obj(b["confusion_matrix"], []string{"labels", "counts"}, name+".confusion_matrix")
	if err != nil {
		return nil, err
	}
	plabels := child(pcm, "labels")
	labels, ok := cm["labels"].([]any)
	if ok {
		seen := map[string]bool{}
		for i, l := range labels {
			seen[pyStr(l, item(plabels, i))] = true
		}
		ok = len(seen) == len(labels)
	}
	if !ok {
		return nil, fail("%s labels must be a list of distinct labels", name)
	}
	names := make([]string, len(labels))
	for i, l := range labels {
		s, isStr := l.(string)
		if !isStr || !slices.Contains(vocab, s) {
			return nil, fail("%s label %s is not a known %s", name, repr(l, item(plabels, i)), name)
		}
		names[i] = s
	}
	k := len(labels)
	if !isGrid(cm["counts"], k, func(c any) bool { n, ok := c.(int64); return ok && n >= 0 }) {
		return nil, fail("%s counts must be a %dx%d list of non-negative ints", name, k, k)
	}
	perClass, ok := b["per_class"].(map[string]any)
	if !ok || !sameKeys(perClass, names) {
		return nil, fail("%s per_class must have exactly the labels as keys", name)
	}
	counts := make([]any, k)
	for i, r := range cm["counts"].([]any) {
		counts[i] = slices.Clone(r.([]any))
	}
	out := map[string]any{
		"confusion_matrix": map[string]any{"labels": slices.Clone(labels), "counts": counts},
	}
	pcOut := map[string]any{}
	for _, label := range names {
		where := name + ".per_class." + label
		e, err := obj(perClass[label], slices.Concat(perClassCounts, perClassProportions, perClassScores), where)
		if err != nil {
			return nil, err
		}
		entry := map[string]any{}
		for _, key := range perClassCounts {
			if entry[key], err = count(e[key], where+"."+key); err != nil {
				return nil, err
			}
		}
		for _, key := range perClassProportions {
			if entry[key], err = proportion(e[key], where+"."+key); err != nil {
				return nil, err
			}
		}
		for _, key := range perClassScores {
			if entry[key], err = score(e[key], where+"."+key, false); err != nil {
				return nil, err
			}
		}
		pcOut[label] = entry
	}
	out["per_class"] = pcOut
	for _, avg := range []string{"macro", "weighted"} {
		where := name + "." + avg
		a, err := obj(b[avg], append(slices.Clone(eval.Averaged), "included_classes"), where)
		if err != nil {
			return nil, err
		}
		included, ok := a["included_classes"].([]any)
		if ok {
			for _, c := range included {
				s, isStr := c.(string)
				if !isStr || !slices.Contains(names, s) {
					ok = false
					break
				}
			}
		}
		if !ok {
			return nil, fail("%s.included_classes must be a list of the block's labels", where)
		}
		rebuilt := map[string]any{}
		for _, key := range eval.Averaged {
			if rebuilt[key], err = score(a[key], where+"."+key, true); err != nil {
				return nil, err
			}
		}
		rebuilt["included_classes"] = slices.Clone(included)
		out[avg] = rebuilt
	}
	if out["accuracy"], err = proportion(b["accuracy"], name+".accuracy"); err != nil {
		return nil, err
	}
	if out["kappa"], err = score(b["kappa"], name+".kappa", false); err != nil {
		return nil, err
	}
	if synthetic {
		strList := func(c any) bool {
			l, ok := c.([]any)
			if !ok {
				return false
			}
			for _, s := range l {
				if _, ok := s.(string); !ok {
					return false
				}
			}
			return true
		}
		if !isGrid(b["ids"], k, strList) {
			return nil, fail("%s.ids must be a %dx%d list of lists of strings", name, k, k)
		}
		ids := make([]any, k)
		for i, r := range b["ids"].([]any) {
			row := make([]any, k)
			for j, c := range r.([]any) {
				row[j] = slices.Clone(c.([]any))
			}
			ids[i] = row
		}
		out["ids"] = ids
	}
	return out, nil
}

func item(pv *policy.Value, i int) *policy.Value {
	if pv == nil || pv.Kind != policy.Array || i >= len(pv.Items) {
		return nil
	}
	return &pv.Items[i]
}

func tierMap(v any, pv *policy.Value, where string, value func(any, string) (any, error)) (any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("%s must be an object", where)
	}
	var unknown []string
	for k := range m {
		if _, ok := route.PlaceholderPrices[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		slices.Sort(unknown)
		return nil, fail("%s has unknown tier %s", where, strings.Join(unknown, ", "))
	}
	out := map[string]any{}
	for _, t := range keysOf(m, pv) {
		x, err := value(m[t], where+"."+t)
		if err != nil {
			return nil, err
		}
		out[t] = x
	}
	return out, nil
}

func checkConfig(v any, pv *policy.Value) (any, error) {
	c, err := obj(v, configKeys, "config")
	if err != nil {
		return nil, err
	}
	return build(map[string]any{},
		field{"threshold", func() (any, error) { return number(c["threshold"], "config.threshold", true) }},
		field{"prices", func() (any, error) {
			if c["prices"] == nil {
				return nil, nil
			}
			return tierMap(c["prices"], child(pv, "prices"), "config.prices", func(x any, w string) (any, error) { return number(x, w, false) })
		}},
	)
}

func checkOperations(v any, pv *policy.Value) (any, error) {
	if v == nil {
		return nil, nil
	}
	o, err := obj(v, operationsKeys, "operations")
	if err != nil {
		return nil, err
	}
	return build(map[string]any{},
		field{"tier_shares", func() (any, error) {
			return tierMap(o["tier_shares"], child(pv, "tier_shares"), "operations.tier_shares", proportion)
		}},
		field{"cost_per_1000_usd", func() (any, error) {
			return number(o["cost_per_1000_usd"], "operations.cost_per_1000_usd", true)
		}},
		field{"prices_are_placeholders", func() (any, error) {
			return flag(o["prices_are_placeholders"], "operations.prices_are_placeholders")
		}},
	)
}

func checkCoverage(v any) (any, error) {
	if v == nil {
		return nil, nil
	}
	c, err := obj(v, coverageKeys, "coverage")
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	for _, k := range coverageKeys {
		if out[k], err = count(c[k], "coverage."+k); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func checkMetrics(v any, pv *policy.Value) (map[string]any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("metrics file must be a JSON object")
	}
	if u := unknownKeys(m, TopKeys); len(u) > 0 {
		return nil, fail("not a metrics/1 file: unknown key %s", strings.Join(u, ", "))
	}
	var missing []string
	for _, k := range TopKeys {
		if _, ok := m[k]; !ok {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		return nil, fail("not a metrics/1 file: missing key %s", strings.Join(missing, ", "))
	}
	if s, ok := m["schema_version"].(string); !ok || s != eval.SchemaVersion {
		return nil, fail("schema_version must be %s, got %s", eval.SchemaVersion, repr(m["schema_version"], child(pv, "schema_version")))
	}
	kind, ok := m["kind"].(string)
	if !ok || !slices.Contains(eval.Kinds, kind) {
		return nil, fail("kind must be one of %s, got %s", strings.Join(eval.Kinds, ", "), repr(m["kind"], child(pv, "kind")))
	}
	cohort, ok := m["cohort"].(map[string]any)
	if !ok || !sameKeys(cohort, eval.CohortKeys) {
		return nil, fail("cohort must have exactly the keys %s", strings.Join(eval.CohortKeys, ", "))
	}
	synthetic := kind == "synthetic"
	if !synthetic && hasIDs(m) {
		return nil, fail("a gold metrics file must not carry ids")
	}
	esc, err := obj(m["escalation"], slices.Concat([]string{"counts"}, EscProportions, EscScores), "escalation")
	if err != nil {
		return nil, err
	}
	counts, err := obj(esc["counts"], CountKeys, "escalation.counts")
	if err != nil {
		return nil, err
	}
	escCounts := map[string]any{}
	for _, k := range CountKeys {
		if escCounts[k], err = count(counts[k], "escalation.counts."+k); err != nil {
			return nil, err
		}
	}
	escalation := map[string]any{"counts": escCounts}
	for _, k := range EscProportions {
		if escalation[k], err = proportion(esc[k], "escalation."+k); err != nil {
			return nil, err
		}
	}
	for _, k := range EscScores {
		if escalation[k], err = score(esc[k], "escalation."+k, false); err != nil {
			return nil, err
		}
	}
	sample, err := obj(m["sample"], eval.SampleKeys, "sample")
	if err != nil {
		return nil, err
	}
	return build(map[string]any{"schema_version": m["schema_version"], "kind": kind},
		field{"run_at", func() (any, error) { return text(m["run_at"], "run_at", false) }},
		field{"cohort", func() (any, error) {
			out := map[string]any{}
			for _, k := range eval.CohortKeys {
				x, err := text(cohort[k], "cohort."+k, true)
				if err != nil {
					return nil, err
				}
				out[k] = x
			}
			return out, nil
		}},
		field{"sample", func() (any, error) {
			out := map[string]any{}
			for _, k := range eval.SampleKeys {
				x, err := intOrNull(sample[k], "sample."+k)
				if err != nil {
					return nil, err
				}
				out[k] = x
			}
			return out, nil
		}},
		field{"config", func() (any, error) { return checkConfig(m["config"], child(pv, "config")) }},
		field{"coverage", func() (any, error) { return checkCoverage(m["coverage"]) }},
		field{"operations", func() (any, error) { return checkOperations(m["operations"], child(pv, "operations")) }},
		field{"n", func() (any, error) { return count(m["n"], "n") }},
		field{"directional_below", func() (any, error) { return count(m["directional_below"], "directional_below") }},
		field{"averaging", func() (any, error) { return text(m["averaging"], "averaging", false) }},
		field{"escalation", func() (any, error) { return escalation, nil }},
		field{"action", func() (any, error) {
			return checkBlock("action", m["action"], child(pv, "action"), route.Actions, synthetic)
		}},
		field{"intent", func() (any, error) {
			return checkBlock("intent", m["intent"], child(pv, "intent"), route.Intents, synthetic)
		}},
	)
}
