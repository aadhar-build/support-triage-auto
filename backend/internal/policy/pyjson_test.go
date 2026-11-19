package policy

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
)

// pyDumps is Python's json.dumps(v, ensure_ascii=True, sort_keys=True,
// separators=(",", ":")) for the shapes the policy goldens use: nil, bool,
// int, string, []any, []string and map[string]any.
func pyDumps(v any) string {
	var b strings.Builder
	writePyJSON(&b, v)
	return b.String()
}

func writePyJSON(b *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case string:
		writePyString(b, x)
	case []string:
		items := make([]any, len(x))
		for i, s := range x {
			items[i] = s
		}
		writePyJSON(b, items)
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			writePyJSON(b, item)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys) // byte order on UTF-8 is code-point order
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writePyString(b, k)
			b.WriteByte(':')
			writePyJSON(b, x[k])
		}
		b.WriteByte('}')
	default:
		panic(fmt.Sprintf("pyDumps: unsupported %T", v))
	}
}

// writePyString escapes like json's ESCAPE_ASCII: everything outside
// [ -~] except the short escapes becomes lowercase \uXXXX (astral code points
// as surrogate pairs).
func writePyString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r >= ' ' && r <= '~':
				b.WriteRune(r)
			case r > 0xFFFF:
				hi, lo := utf16.EncodeRune(r)
				fmt.Fprintf(b, `\u%04x\u%04x`, hi, lo)
			default:
				fmt.Fprintf(b, `\u%04x`, r)
			}
		}
	}
	b.WriteByte('"')
}

// jsonLines joins pyDumps of each object, one per line.
func jsonLines(objs []map[string]any) []byte {
	var b strings.Builder
	for _, o := range objs {
		b.WriteString(pyDumps(o))
		b.WriteByte('\n')
	}
	return []byte(b.String())
}
