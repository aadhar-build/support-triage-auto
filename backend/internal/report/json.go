// Package report is the Go port of the metrics/1 loader and checker in
// triage.report (load_metrics, check_metrics) and of the strict owner-file
// reader the console uses for manifests, metrics and thresholds.
package report

import (
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"strconv"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
)

// MaxFileBytes is the largest owner file the reader accepts.
const MaxFileBytes = 32 << 20

// MaxDepth caps array and object nesting.
const MaxDepth = 512

// ReadJSON reads one owner JSON file strictly and converts it to plain Go
// values: map[string]any, []any, string, bool, nil, int64 and float64.
func ReadJSON(path string) (any, error) {
	_, v, err := readJSON(path)
	return v, err
}

// readJSON returns the parsed policy.Value (which keeps key order) next to
// the converted value.
func readJSON(path string) (policy.Value, any, error) {
	f, err := os.Open(path)
	if err != nil {
		return policy.Value{}, nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, MaxFileBytes+1))
	if err != nil {
		return policy.Value{}, nil, err
	}
	if len(b) > MaxFileBytes {
		return policy.Value{}, nil, errors.New("file is too large")
	}
	if !utf8.Valid(b) {
		return policy.Value{}, nil, errors.New("not valid UTF-8")
	}
	if err := checkDepth(b); err != nil {
		return policy.Value{}, nil, err
	}
	pv, err := policy.ParseJSON(b)
	if err != nil {
		return policy.Value{}, nil, err
	}
	v, err := convert(pv)
	if err != nil {
		return policy.Value{}, nil, err
	}
	return pv, v, nil
}

// checkDepth refuses nesting deeper than MaxDepth, scanning outside strings
// only, before any recursive decoding.
func checkDepth(b []byte) error {
	depth, inStr := 0, false
	for i := 0; i < len(b); i++ {
		c := b[i]
		if inStr {
			switch c {
			case '\\':
				i++
			case '"':
				inStr = false
			}
			continue
		}
		switch c {
		case '"':
			inStr = true
		case '[', '{':
			depth++
			if depth > MaxDepth {
				return fmt.Errorf("nested deeper than %d", MaxDepth)
			}
		case ']', '}':
			depth--
		}
	}
	return nil
}

func convert(v policy.Value) (any, error) {
	switch v.Kind {
	case policy.Null:
		return nil, nil
	case policy.Bool:
		return v.Bool, nil
	case policy.String:
		return v.Str, nil
	case policy.Number:
		if v.IsInt() {
			n, err := strconv.ParseInt(v.Num, 10, 64)
			if err != nil {
				return nil, fmt.Errorf("int %s is out of range", v.Num)
			}
			return n, nil
		}
		f, err := strconv.ParseFloat(v.Num, 64)
		if err != nil || math.IsInf(f, 0) {
			return nil, fmt.Errorf("number %s is out of range", v.Num)
		}
		return f, nil
	case policy.Array:
		out := make([]any, len(v.Items))
		for i, item := range v.Items {
			x, err := convert(item)
			if err != nil {
				return nil, err
			}
			out[i] = x
		}
		return out, nil
	case policy.Object:
		out := make(map[string]any, len(v.Keys))
		for _, k := range v.Keys {
			x, err := convert(v.Vals[k])
			if err != nil {
				return nil, err
			}
			out[k] = x
		}
		return out, nil
	}
	return nil, errors.New("unknown JSON kind")
}
