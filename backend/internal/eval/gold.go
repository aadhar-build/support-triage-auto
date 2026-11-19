package eval

import (
	"bytes"
	"fmt"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// GoldColumns are the columns a gold CSV must have, in Python's order.
var GoldColumns = []string{"thread_id", "intent", "action", "should_escalate"}

// GoldRow is one gold label.
type GoldRow struct {
	Intent, Action string
	ShouldEscalate bool
}

// Gold holds gold rows keyed by canonical decimal thread id, so ids of any
// width behave as Python's unbounded ints.
type Gold struct {
	rows map[string]GoldRow
	ids  []string
}

// Lookup returns the gold row for a routed thread id.
func (g Gold) Lookup(threadID uint64) (GoldRow, bool) {
	r, ok := g.rows[strconv.FormatUint(threadID, 10)]
	return r, ok
}

// Len is the number of gold rows.
func (g Gold) Len() int { return len(g.ids) }

// IDs returns the canonical decimal ids in file order.
func (g Gold) IDs() []string { return slices.Clone(g.ids) }

// GoldFileError is Python's GoldFileError: the gold CSV is empty, lacks a
// column, or has a bad value.
type GoldFileError struct{ Msg string }

func (e *GoldFileError) Error() string { return e.Msg }

// GoldDecodeError is Python's UnicodeDecodeError on a gold CSV.
type GoldDecodeError struct{ Offset int }

func (e *GoldDecodeError) Error() string {
	return fmt.Sprintf("gold file is not valid UTF-8 (byte offset %d)", e.Offset)
}

func goldErr(format string, a ...any) error { return &GoldFileError{fmt.Sprintf(format, a...)} }

var asciiDigits = regexp.MustCompile(`^[0-9]+$`)

const textChunk = 8192

// decodeChecker reproduces when CPython's TextIOWrapper (8192-byte chunks,
// incremental UTF-8 decoder) would have raised UnicodeDecodeError, so a
// decode error and a row error are reported in Python's order.
type decodeChecker struct {
	data    []byte
	checked int // data[:checked] is known valid
}

// invalidAt returns the offset of the first invalid byte in data[from:to],
// ignoring an incomplete but so-far-valid sequence at to (the incremental
// decoder keeps it for the next chunk), and the length of that tail.
func invalidAt(data []byte, from, to int) (bad, tail int) {
	for i := from; i < to; {
		r, size := utf8.DecodeRune(data[i:to])
		if r == utf8.RuneError && size == 1 {
			if !utf8.FullRune(data[i:to]) {
				return -1, to - i
			}
			return i, 0
		}
		i += size
	}
	return -1, 0
}

// before is called before a record ending at raw offset e is checked.
func (d *decodeChecker) before(e int) error {
	n := len(d.data)
	b := min((e+textChunk-1)/textChunk*textChunk, n)
	bad, tail := invalidAt(d.data, d.checked, b)
	if bad < 0 && e > 0 && d.data[e-1] == '\r' && e == b-tail {
		// The reader must look past a trailing \r to see whether \n follows.
		if b == n {
			if tail > 0 {
				return &GoldDecodeError{b - tail}
			}
		} else {
			b = min(b+textChunk, n)
			bad, tail = invalidAt(d.data, d.checked, b)
		}
	}
	if bad >= 0 {
		return &GoldDecodeError{bad}
	}
	d.checked = max(d.checked, b-tail)
	return nil
}

// LoadGold reads and validates a gold CSV as Python's load_gold does. A
// missing file returns the OS error unchanged.
func LoadGold(path string) (Gold, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Gold{}, err
	}
	dc := &decodeChecker{data: data}
	bom := 0
	if bytes.HasPrefix(data, []byte("\xef\xbb\xbf")) {
		bom = 3
	}
	g := Gold{rows: map[string]GoldRow{}}
	var header []string
	seenHeader := false
	err = ingest.ReadPyCSV(data[bom:], func(fields []string, line, end int) error {
		if err := dc.before(bom + end); err != nil {
			return err
		}
		if !seenHeader {
			seenHeader = true
			if len(fields) == 0 {
				return goldErr("gold file is empty: expected columns %s", strings.Join(GoldColumns, ", "))
			}
			header = make([]string, len(fields))
			for i, h := range fields {
				header[i] = ingest.PyStrip(h)
			}
			var missing []string
			for _, c := range GoldColumns {
				if !slices.Contains(header, c) {
					missing = append(missing, c)
				}
			}
			if len(missing) > 0 {
				return goldErr("gold file is missing column(s): %s", strings.Join(missing, ", "))
			}
			return nil
		}
		if len(fields) == 0 {
			return nil // DictReader skips blank records
		}
		return g.addRow(header, fields, line)
	})
	if err == nil && !seenHeader {
		err = goldErr("gold file is empty: expected columns %s", strings.Join(GoldColumns, ", "))
	}
	if err == ingest.ErrInvalidUTF8 {
		bad, _ := invalidAt(data, 0, len(data))
		return Gold{}, &GoldDecodeError{max(bad, 0)}
	}
	if err != nil {
		return Gold{}, err
	}
	if bad, tail := invalidAt(data, dc.checked, len(data)); bad >= 0 || tail > 0 {
		return Gold{}, &GoldDecodeError{len(data) - tail}
	}
	return g, nil
}

// addRow mirrors DictReader's row dict (last duplicate header wins, short
// rows fill with None) and load_gold's checks, in Python's order.
func (g *Gold) addRow(header, fields []string, line int) error {
	row := map[string]*string{}
	for i := 0; i < min(len(header), len(fields)); i++ {
		row[header[i]] = &fields[i]
	}
	for _, h := range header[min(len(header), len(fields)):] {
		row[h] = nil
	}
	v := map[string]string{}
	for _, c := range GoldColumns {
		p := row[c]
		if p == nil {
			return goldErr("line %d: row has too few fields", line)
		}
		v[c] = ingest.PyStrip(*p)
	}
	if !asciiDigits.MatchString(v["thread_id"]) {
		return goldErr("line %d: thread_id must be a non-negative integer, got %s", line, route.PyRepr(v["thread_id"]))
	}
	id := strings.TrimLeft(v["thread_id"], "0")
	if id == "" {
		id = "0"
	}
	if _, dup := g.rows[id]; dup {
		return goldErr("line %d: duplicate thread_id %s", line, id)
	}
	if !slices.Contains(route.Intents, v["intent"]) {
		return goldErr("line %d: unknown intent %s", line, route.PyRepr(v["intent"]))
	}
	if !slices.Contains(route.Actions, v["action"]) {
		return goldErr("line %d: unknown action %s", line, route.PyRepr(v["action"]))
	}
	esc := asciiLower(v["should_escalate"])
	if esc != "true" && esc != "1" && esc != "false" && esc != "0" {
		return goldErr("line %d: should_escalate must be true/false/1/0, got %s", line, route.PyRepr(v["should_escalate"]))
	}
	g.rows[id] = GoldRow{v["intent"], v["action"], esc == "true" || esc == "1"}
	g.ids = append(g.ids, id)
	return nil
}

func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}
