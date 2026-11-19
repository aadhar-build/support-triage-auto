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

// decodeSim simulates CPython's TextIOWrapper reads (8192-byte chunks, a
// non-final incremental UTF-8 decode per read, a final decode on the
// zero-byte read) so a decode error and a row error are reported in
// Python's order. Offsets are raw file offsets, BOM included.
type decodeSim struct {
	data []byte
	pos  int  // bytes read
	dec  int  // bytes decoded into complete characters
	eof  bool // the zero-byte read (final decode) has happened
}

// heldTail reports whether data[i:end] is an incomplete sequence CPython's
// decoder holds for the next read: utf8.FullRune is false, or it is \xED
// followed by \xA0-\xBF.
func heldTail(data []byte, i, end int) bool {
	t := data[i:end]
	return !utf8.FullRune(t) || (len(t) == 2 && t[0] == 0xED && t[1] >= 0xA0 && t[1] <= 0xBF)
}

// read performs one read and decode.
func (d *decodeSim) read() error {
	if d.eof {
		return nil
	}
	n := len(d.data)
	if d.pos == n {
		d.eof = true
		if d.dec < n {
			return &GoldDecodeError{d.dec}
		}
		return nil
	}
	d.pos = min(d.pos+textChunk, n)
	for i := d.dec; i < d.pos; {
		r, size := utf8.DecodeRune(d.data[i:d.pos])
		if r == utf8.RuneError && size == 1 {
			if heldTail(d.data, i, d.pos) {
				break
			}
			return &GoldDecodeError{i}
		}
		i += size
		d.dec = i
	}
	return nil
}

// before is called before a record ending at raw offset e is checked.
func (d *decodeSim) before(e int) error {
	for d.pos < e {
		if err := d.read(); err != nil {
			return err
		}
	}
	n := len(d.data)
	switch {
	case e > 0 && d.data[e-1] == '\r':
		// The reader looks past a bare \r for a \n, reading until one
		// complete character after e is decoded or the final decode runs.
		for d.dec <= e && !d.eof {
			if err := d.read(); err != nil {
				return err
			}
		}
	case e == n && (e == 0 || d.data[e-1] != '\n'):
		// No line end at end of file: the reader takes the zero-byte read.
		for !d.eof {
			if err := d.read(); err != nil {
				return err
			}
		}
	}
	return nil
}

// finish runs the remaining reads and the final decode.
func (d *decodeSim) finish() error {
	for !d.eof {
		if err := d.read(); err != nil {
			return err
		}
	}
	return nil
}

// LoadGold reads and validates a gold CSV as Python's load_gold does. A
// missing file returns the OS error unchanged.
func LoadGold(path string) (Gold, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Gold{}, err
	}
	dc := &decodeSim{data: data}
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
		if ferr := dc.finish(); ferr != nil {
			return Gold{}, ferr
		}
		return Gold{}, &GoldDecodeError{len(data)}
	}
	if err != nil {
		return Gold{}, err
	}
	if err := dc.finish(); err != nil {
		return Gold{}, err
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
