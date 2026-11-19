package route

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
)

// PyJSONIndent is json.dumps(v, sort_keys=True, indent=2), without a
// trailing newline. Strings and floats go through writeJSONString and
// reprFloat, so escaping and float repr come from one place. NaN and ±Inf
// are refused, as is any kind not listed below.
func PyJSONIndent(v any) ([]byte, error) {
	var b strings.Builder
	if err := writeIndent(&b, v, 0); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

// PyRepr is Python repr over the Record value kinds (see pyRepr).
func PyRepr(v any) string { return pyRepr(v) }

func writeIndentList(b *strings.Builder, n, depth int, item func(i int) error) error {
	if n == 0 {
		b.WriteString("[]")
		return nil
	}
	b.WriteByte('[')
	for i := 0; i < n; i++ {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteByte('\n')
		b.WriteString(strings.Repeat("  ", depth+1))
		if err := item(i); err != nil {
			return err
		}
	}
	b.WriteByte('\n')
	b.WriteString(strings.Repeat("  ", depth))
	b.WriteByte(']')
	return nil
}

func writeIndent(b *strings.Builder, v any, depth int) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint64:
		b.WriteString(strconv.FormatUint(x, 10))
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return errors.New("pyjson: cannot encode " + reprFloat(x))
		}
		b.WriteString(reprFloat(x))
	case string:
		writeJSONString(b, x)
	case *string:
		if x == nil {
			b.WriteString("null")
			return nil
		}
		writeJSONString(b, *x)
	case *int64:
		if x == nil {
			b.WriteString("null")
			return nil
		}
		b.WriteString(strconv.FormatInt(*x, 10))
	case *float64:
		if x == nil {
			b.WriteString("null")
			return nil
		}
		return writeIndent(b, *x, depth)
	case []any:
		return writeIndentList(b, len(x), depth, func(i int) error { return writeIndent(b, x[i], depth+1) })
	case []string:
		return writeIndentList(b, len(x), depth, func(i int) error { return writeIndent(b, x[i], depth+1) })
	case []int:
		return writeIndentList(b, len(x), depth, func(i int) error { return writeIndent(b, x[i], depth+1) })
	case [][]int:
		return writeIndentList(b, len(x), depth, func(i int) error { return writeIndent(b, x[i], depth+1) })
	case []float64:
		return writeIndentList(b, len(x), depth, func(i int) error { return writeIndent(b, x[i], depth+1) })
	case map[string]any:
		if len(x) == 0 {
			b.WriteString("{}")
			return nil
		}
		b.WriteByte('{')
		for i, k := range sortedKeys(x) {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteByte('\n')
			b.WriteString(strings.Repeat("  ", depth+1))
			writeJSONString(b, k)
			b.WriteString(": ")
			if err := writeIndent(b, x[k], depth+1); err != nil {
				return err
			}
		}
		b.WriteByte('\n')
		b.WriteString(strings.Repeat("  ", depth))
		b.WriteByte('}')
	default:
		return fmt.Errorf("pyjson: unsupported type %T", v)
	}
	return nil
}
