package eval

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// runAtRE is Python's _RUN_AT_RE restricted to ASCII digits (owner decision Q1).
var runAtRE = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$`)

// CheckRunAt is Python's metrics.check_run_at. The first 19 bytes must be a
// real date and time; year 0, which time.Parse accepts, is refused as
// strptime refuses it.
func CheckRunAt(s string) error {
	if !runAtRE.MatchString(s) {
		return errors.New("run_at must be an ISO 8601 UTC time ending in Z, got " + route.PyRepr(s))
	}
	tm, err := time.Parse("2006-01-02T15:04:05", s[:19])
	if err != nil || tm.Year() < 1 {
		return errors.New("run_at is not a valid time: " + route.PyRepr(s))
	}
	return nil
}

// CohortKeys, CohortNames and SampleKeys are Python's COHORT_KEYS, COHORT_NAMES
// and SAMPLE_KEYS.
var (
	CohortKeys  = []string{"name", "split", "gold_revision", "brand_population"}
	CohortNames = map[string][]string{"gold": {"gold_random", "hard_case"}, "synthetic": {"synthetic"}}
	SampleKeys  = []string{"size", "seed"}
)

var revisionRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

func checkExactKeys(name string, m map[string]any, keys []string) error {
	ok := m != nil && len(m) == len(keys)
	for _, k := range keys {
		if _, has := m[k]; !has {
			ok = false
		}
	}
	if !ok {
		return fmt.Errorf("%s must be an object with exactly the keys %s", name, strings.Join(keys, ", "))
	}
	return nil
}

// checkCohort is Python's _check_cohort. Values are type-asserted before any
// comparison, so a map or slice value never reaches ==.
func checkCohort(kind string, c map[string]any) error {
	if err := checkExactKeys("cohort", c, CohortKeys); err != nil {
		return err
	}
	if name, ok := c["name"].(string); !ok || !slices.Contains(CohortNames[kind], name) {
		return fmt.Errorf("cohort name %s does not fit kind %s", route.PyRepr(c["name"]), route.PyRepr(kind))
	}
	switch c["split"].(type) {
	case nil, string:
	default:
		return errors.New("cohort split must be a string or null")
	}
	if rev, ok := c["gold_revision"].(string); !ok || !revisionRE.MatchString(rev) {
		return errors.New("cohort gold_revision must be 'sha256:' + 64 hex digits")
	}
	if bp, ok := c["brand_population"].(string); !ok || bp != "all" {
		return errors.New("cohort brand_population must be 'all'")
	}
	return nil
}

// checkSample is Python's _check_sample: each value is nil or an integer kind
// route's writer prints, never a bool or a float.
func checkSample(s map[string]any) error {
	if err := checkExactKeys("sample", s, SampleKeys); err != nil {
		return err
	}
	for _, k := range SampleKeys {
		switch s[k].(type) {
		case nil, int, int64, uint64:
		default:
			return fmt.Errorf("sample %s must be an int or null", k)
		}
	}
	return nil
}

// ComparisonKey is Python's comparison_key: the fields that must match for two
// metrics files to be compared, looked up in Python's order (cohort first).
func ComparisonKey(m map[string]any) ([6]any, error) {
	var key [6]any
	fail := func(detail string) ([6]any, error) {
		return [6]any{}, errors.New("metrics lacks a comparison field: " + detail)
	}
	cohort, ok := m["cohort"]
	if !ok {
		return fail(route.PyRepr("cohort"))
	}
	for i, k := range []string{"schema_version", "kind"} {
		v, ok := m[k]
		if !ok {
			return fail(route.PyRepr(k))
		}
		key[i] = v
	}
	c, isMap := cohort.(map[string]any)
	if !isMap {
		return fail(notSubscriptable(cohort))
	}
	for i, k := range CohortKeys {
		v, ok := c[k]
		if !ok {
			return fail(route.PyRepr(k))
		}
		key[2+i] = v
	}
	return key, nil
}

// notSubscriptable is Python 3.12's TypeError text for v["name"] when v is
// not a dict, by Go type.
func notSubscriptable(v any) string {
	switch x := v.(type) {
	case nil:
		return "'NoneType' object is not subscriptable"
	case []any:
		return "list indices must be integers or slices, not str"
	case string:
		return "string indices must be integers, not 'str'"
	case bool:
		return "'bool' object is not subscriptable"
	case int, int64, uint64:
		return "'int' object is not subscriptable"
	case json.Number:
		if isIntLiteral(string(x)) {
			return "'int' object is not subscriptable"
		}
	}
	return "'float' object is not subscriptable"
}

// isIntLiteral reports whether a JSON number literal has no fraction or
// exponent, as Python's json module would decode it to int (any size).
func isIntLiteral(s string) bool {
	return !strings.ContainsAny(s, ".eE")
}

// DirectionalBelow is Python's DIRECTIONAL_BELOW: a rate over fewer cases is
// marked directional.
const DirectionalBelow = 30

// Proportion is Python's metrics.proportion: {value, num, n, ci, directional}.
func Proportion(num, den int) map[string]any {
	var value, ci any
	if den != 0 {
		value = float64(num) / float64(den)
	}
	if lo, hi, ok := WilsonInterval(num, den); ok {
		ci = []any{lo, hi}
	}
	return map[string]any{"value": value, "num": num, "n": den, "ci": ci, "directional": den < DirectionalBelow}
}

// ToJSON is Python's metrics.to_json: json.dumps(sort_keys=True, indent=2) + "\n".
func ToJSON(m map[string]any) ([]byte, error) {
	b, err := route.PyJSONIndent(m)
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

// SchemaVersion, Kinds, Averaged and Averaging are Python's SCHEMA_VERSION,
// KINDS, AVERAGED and AVERAGING.
const SchemaVersion = "metrics/1"

var (
	Kinds    = []string{"gold", "synthetic"}
	Averaged = []string{"tpr", "tnr", "precision", "f1", "f2", "f0_5"}
)

const Averaging = "Macro and weighted averages run over the included classes: the union of gold and " +
	"predicted classes (support > 0 or predicted > 0), listed in included_classes. A " +
	"per-class value that is null counts as zero in the average (zero_filled counts " +
	"them) and still shows as null in per_class. Macro is the unweighted mean; " +
	"weighted is sum(support * value) / sum(support)."

// ScoredCase is Python's metrics.ScoredCase. A nil CaseID is Python's None.
type ScoredCase struct {
	CaseID                 *string
	GoldIntent, GoldAction string
	GoldEscalate           bool
	PredIntent, PredAction string
}

// NewScoredCase checks the labels in Python's __post_init__ order.
func NewScoredCase(caseID *string, goldIntent, goldAction string, goldEscalate bool, predIntent, predAction string) (ScoredCase, error) {
	for _, f := range []struct {
		name, v string
		labels  []string
	}{
		{"gold_intent", goldIntent, route.Intents}, {"pred_intent", predIntent, route.Intents},
		{"gold_action", goldAction, route.Actions}, {"pred_action", predAction, route.Actions},
	} {
		if !slices.Contains(f.labels, f.v) {
			return ScoredCase{}, fmt.Errorf("unknown %s %s", f.name, route.PyRepr(f.v))
		}
	}
	return ScoredCase{caseID, goldIntent, goldAction, goldEscalate, predIntent, predAction}, nil
}

// CasesFromRouted is Python's cases_from_routed: one case per routed thread
// with a gold row, in routed order, with no case id.
func CasesFromRouted(routed []route.RoutedThread, gold Gold) ([]ScoredCase, error) {
	var out []ScoredCase
	for _, r := range routed {
		g, ok := gold.Lookup(r.ThreadID)
		if !ok {
			continue
		}
		c, err := NewScoredCase(nil, g.Intent, g.Action, g.ShouldEscalate, r.Labels.Intent, r.Labels.Action)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, nil
}

// SyntheticExpected and SyntheticActual are the parts of run_synthetic's
// per-case result that metrics reads.
type SyntheticExpected struct {
	Intent   string `json:"intent"`
	Action   string `json:"action"`
	Escalate bool   `json:"escalate"`
}

type SyntheticActual struct {
	Intent string `json:"intent"`
	Action string `json:"action"`
}

// SyntheticResult is one run_synthetic per-case result; only these fields are read.
type SyntheticResult struct {
	ID       string            `json:"id"`
	Expected SyntheticExpected `json:"expected"`
	Actual   SyntheticActual   `json:"actual"`
}

// CasesFromSynthetic is Python's cases_from_synthetic.
func CasesFromSynthetic(results []SyntheticResult) ([]ScoredCase, error) {
	out := make([]ScoredCase, 0, len(results))
	for _, r := range results {
		id := r.ID
		c, err := NewScoredCase(&id, r.Expected.Intent, r.Expected.Action, r.Expected.Escalate, r.Actual.Intent, r.Actual.Action)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, nil
}

func score(value any, n int, directional bool) map[string]any {
	return map[string]any{"value": value, "n": n, "ci": nil, "directional": directional}
}

// fbeta is Python's _fbeta. f1 and f2 take Python's integer path; f0_5 is
// float arithmetic in Python's order, each product wrapped so it can't fuse.
func fbeta(key string, tp, fp, fn int) any {
	switch key {
	case "f1", "f2":
		b2 := 1
		if key == "f2" {
			b2 = 4
		}
		den := (1+b2)*tp + b2*fn + fp
		if den == 0 {
			return nil
		}
		return float64((1+b2)*tp) / float64(den)
	}
	num := float64(1.25 * float64(tp))
	den := num + float64(0.25*float64(fn)) + float64(fp)
	if den == 0 {
		return nil
	}
	return num / den
}

func fscores(dst map[string]any, tp, fp, fn, n int, directional bool) {
	for _, k := range []string{"f1", "f2", "f0_5"} {
		dst[k] = score(fbeta(k, tp, fp, fn), n, directional)
	}
}

// bigFloat converts an exact integer to float64 rounding to nearest-even, as
// Python's float(int) does.
func bigFloat(x *big.Int) float64 {
	f, _ := new(big.Float).SetInt(x).Float64()
	return f
}

func escalation(tp, fp, tn, fn int) map[string]any {
	n := tp + fp + tn + fn
	pos, neg := tp+fn, tn+fp
	out := map[string]any{
		"counts": map[string]any{"tp": tp, "fp": fp, "tn": tn, "fn": fn, "positives": pos, "predicted_positives": tp + fp},
		"tpr":    Proportion(tp, pos), "tnr": Proportion(tn, neg),
		"fpr": Proportion(fp, neg), "fnr": Proportion(fn, pos),
		"precision": Proportion(tp, tp+fp), "npv": Proportion(tn, tn+fn),
		"accuracy": Proportion(tp+tn, n), "prevalence": Proportion(pos, n),
	}
	fscores(out, tp, fp, fn, n, pos < DirectionalBelow)
	thin := pos < DirectionalBelow || neg < DirectionalBelow
	var ba any
	tpr, ok1 := out["tpr"].(map[string]any)["value"].(float64)
	tnr, ok2 := out["tnr"].(map[string]any)["value"].(float64)
	if ok1 && ok2 {
		ba = (tpr + tnr) / 2
	}
	out["balanced_accuracy"] = score(ba, n, thin)
	b := big.NewInt
	factors := new(big.Int).Mul(new(big.Int).Mul(b(int64(tp+fp)), b(int64(tp+fn))), new(big.Int).Mul(b(int64(tn+fp)), b(int64(tn+fn))))
	var mcc any
	if factors.Sign() != 0 {
		num := new(big.Int).Sub(new(big.Int).Mul(b(int64(tp)), b(int64(tn))), new(big.Int).Mul(b(int64(fp)), b(int64(fn))))
		mcc = bigFloat(num) / math.Sqrt(bigFloat(factors))
	}
	out["mcc"] = score(mcc, n, thin)
	return out
}

// average is Python's _average: zero-filled over the included classes.
func average(perClass map[string]map[string]any, included []string, key string, weighted bool, n int) map[string]any {
	total, weight := 0.0, 0.0
	zeroFilled := 0
	directional := false
	for _, label := range included {
		value, ok := perClass[label][key].(map[string]any)["value"].(float64)
		if !ok {
			zeroFilled++
			value = 0.0
		}
		support := perClass[label]["support"].(int)
		w := 1
		if weighted {
			w = support
		}
		total += float64(float64(w) * value)
		weight += float64(w)
		if support < DirectionalBelow {
			directional = true
		}
	}
	var v any
	if len(included) > 0 && weight != 0 {
		v = total / weight
	}
	out := score(v, n, directional)
	out["zero_filled"] = zeroFilled
	return out
}

// classes is Python's _classes: confusion matrix, per-class blocks, averages,
// accuracy and kappa.
func classes(labels []string, counts [][]int, n int) map[string]any {
	k := len(labels)
	support, predicted := make([]int, k), make([]int, k)
	for r := range k {
		for c := range k {
			support[r] += counts[r][c]
			predicted[c] += counts[r][c]
		}
	}
	perClass := map[string]map[string]any{}
	perClassOut := map[string]any{}
	for i, label := range labels {
		tp := counts[i][i]
		fn, fp := support[i]-tp, predicted[i]-tp
		tn := n - tp - fn - fp
		e := map[string]any{"support": support[i], "predicted": predicted[i], "tp": tp, "fp": fp, "tn": tn, "fn": fn,
			"tpr": Proportion(tp, support[i]), "tnr": Proportion(tn, tn+fp), "precision": Proportion(tp, predicted[i])}
		fscores(e, tp, fp, fn, n, support[i] < DirectionalBelow)
		perClass[label] = e
		perClassOut[label] = e
	}
	included := []string{}
	for i, label := range labels {
		if support[i] != 0 || predicted[i] != 0 {
			included = append(included, label)
		}
	}
	averages := map[string]any{}
	for _, name := range []string{"macro", "weighted"} {
		avg := map[string]any{}
		for _, key := range Averaged {
			avg[key] = average(perClass, included, key, name == "weighted", n)
		}
		avg["included_classes"] = slices.Clone(included)
		averages[name] = avg
	}
	correct, expectedNum := 0, 0
	for i := range k {
		correct += counts[i][i]
		expectedNum += support[i] * predicted[i]
	}
	var kappa any
	if n != 0 && expectedNum != n*n {
		pO, pE := float64(correct)/float64(n), float64(expectedNum)/float64(n*n)
		kappa = (pO - pE) / (1 - pE)
	}
	return map[string]any{
		"confusion_matrix": map[string]any{"labels": slices.Clone(labels), "counts": counts},
		"per_class":        perClassOut, "macro": averages["macro"], "weighted": averages["weighted"],
		"accuracy": Proportion(correct, n), "kappa": score(kappa, n, n < DirectionalBelow),
	}
}

// ComputeOptions are compute's keyword arguments. Nil Config, Coverage or
// Operations is null.
type ComputeOptions struct {
	RunAt          string
	Cohort, Sample map[string]any
	Config         any
	Coverage       any
	Operations     any
}

// deepCopy copies maps and slices so the output shares nothing with the caller.
func deepCopy(v any) any {
	switch x := v.(type) {
	case map[string]any:
		if x == nil {
			return x
		}
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = deepCopy(e)
		}
		return out
	case []any:
		if x == nil {
			return x
		}
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = deepCopy(e)
		}
		return out
	case []string:
		return slices.Clone(x)
	case []int:
		return slices.Clone(x)
	case []float64:
		return slices.Clone(x)
	case [][]int:
		out := make([][]int, len(x))
		for i, e := range x {
			out[i] = slices.Clone(e)
		}
		return out
	}
	return v
}

// Compute is Python's metrics.compute. Checks run in Python's order: kind,
// run_at, cohort, sample. Only synthetic-kind output carries case ids.
func Compute(cases []ScoredCase, kind string, opts ComputeOptions) (map[string]any, error) {
	if !slices.Contains(Kinds, kind) {
		return nil, errors.New("kind must be 'gold' or 'synthetic', got " + route.PyRepr(kind))
	}
	if err := CheckRunAt(opts.RunAt); err != nil {
		return nil, err
	}
	if err := checkCohort(kind, opts.Cohort); err != nil {
		return nil, err
	}
	if err := checkSample(opts.Sample); err != nil {
		return nil, err
	}
	withIDs := kind == "synthetic"
	type block struct {
		labels []string
		counts [][]int
		ids    [][][]any
	}
	blocks := []*block{{labels: route.Actions}, {labels: route.Intents}}
	for _, b := range blocks {
		k := len(b.labels)
		b.counts = make([][]int, k)
		b.ids = make([][][]any, k)
		for i := range k {
			b.counts[i] = make([]int, k)
			b.ids[i] = make([][]any, k)
		}
	}
	tp, fp, tn, fn, n := 0, 0, 0, 0, 0
	for _, c := range cases {
		n++
		predEsc := c.PredAction == "escalate"
		switch {
		case c.GoldEscalate && predEsc:
			tp++
		case c.GoldEscalate:
			fn++
		case predEsc:
			fp++
		default:
			tn++
		}
		for i, pair := range [][2]string{{c.GoldAction, c.PredAction}, {c.GoldIntent, c.PredIntent}} {
			b := blocks[i]
			r, col := slices.Index(b.labels, pair[0]), slices.Index(b.labels, pair[1])
			b.counts[r][col]++
			if withIDs {
				var id any
				if c.CaseID != nil {
					id = *c.CaseID
				}
				b.ids[r][col] = append(b.ids[r][col], id)
			}
		}
	}
	out := map[string]any{
		"schema_version": SchemaVersion, "kind": kind, "run_at": opts.RunAt,
		"cohort": deepCopy(opts.Cohort), "sample": deepCopy(opts.Sample),
		"config": deepCopy(opts.Config), "coverage": deepCopy(opts.Coverage), "operations": deepCopy(opts.Operations),
		"n": n, "directional_below": DirectionalBelow, "averaging": Averaging,
		"escalation": escalation(tp, fp, tn, fn),
	}
	for i, name := range []string{"action", "intent"} {
		b := blocks[i]
		cb := classes(b.labels, b.counts, n)
		if withIDs {
			rows := make([]any, len(b.ids))
			for r, row := range b.ids {
				cells := make([]any, len(row))
				for c, cell := range row {
					cells[c] = append([]any{}, cell...)
				}
				rows[r] = cells
			}
			cb["ids"] = rows
		}
		out[name] = cb
	}
	return out, nil
}
