package policy

import (
	"fmt"
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode"
)

// pyRepr renders a JSON value the way Python's repr renders what json.loads
// returns for it: None/True/False, ints, shortest float repr, str with Python's
// quote choice and escapes, lists and dicts in key order.
func pyRepr(v Value) string {
	var b strings.Builder
	writeRepr(&b, v)
	return b.String()
}

func writeRepr(b *strings.Builder, v Value) {
	switch v.Kind {
	case Null:
		b.WriteString("None")
	case Bool:
		if v.Bool {
			b.WriteString("True")
		} else {
			b.WriteString("False")
		}
	case Number:
		b.WriteString(reprNumber(v))
	case String:
		b.WriteString(reprStr(v.Str))
	case Array:
		b.WriteByte('[')
		for i, item := range v.Items {
			if i > 0 {
				b.WriteString(", ")
			}
			writeRepr(b, item)
		}
		b.WriteByte(']')
	case Object:
		b.WriteByte('{')
		for i, k := range v.Keys {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(reprStr(k))
			b.WriteString(": ")
			writeRepr(b, v.Vals[k])
		}
		b.WriteByte('}')
	}
}

func reprNumber(v Value) string {
	if v.IsInt() {
		n, ok := new(big.Int).SetString(v.Num, 10)
		if !ok {
			return v.Num
		}
		return n.String()
	}
	f, err := strconv.ParseFloat(v.Num, 64)
	if err != nil && !math.IsInf(f, 0) {
		return v.Num
	}
	return reprFloat(f)
}

// reprFloat is Python's float repr: the shortest round-trip digits, in
// scientific notation when the decimal exponent is below -4 or at least 16.
func reprFloat(f float64) string {
	switch {
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	case math.IsNaN(f):
		return "nan"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64)
	exp, _ := strconv.Atoi(e[strings.IndexByte(e, 'e')+1:])
	if f != 0 && (exp < -4 || exp >= 16) {
		return e
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// reprStr is Python's str repr.
func reprStr(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case r == rune(quote):
			b.WriteByte('\\')
			b.WriteByte(quote)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == ' ' || unicode.IsPrint(r):
			b.WriteRune(r)
		case r < 0x100:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x10000:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}
