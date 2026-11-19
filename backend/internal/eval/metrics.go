package eval

import (
	"encoding/json"
	"errors"
	"fmt"
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
