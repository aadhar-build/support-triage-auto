package ingest

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"unicode/utf8"
)

// fieldLimit is CPython's default csv.field_size_limit(), in code points.
const fieldLimit = 131072

// ErrFieldTooLarge and ErrInvalidUTF8 abort a read, as Python raises on both.
var (
	ErrFieldTooLarge = errors.New("field larger than field limit (131072)")
	ErrInvalidUTF8   = errors.New("invalid UTF-8 in input")
)

const (
	stStartRecord = iota
	stStartField
	stInField
	stInQuoted
	stQuoteInQuoted
	stEatCRNL
)

// eol marks the end of a line, as CPython's reader feeds it after each line
// the newline="" file iterator yields.
const eol rune = -1

// csvReader is a port of CPython's _csv reader with the excel dialect and
// strict=False, reading a newline="" UTF-8 stream. It tracks byte offsets so a
// record can be re-parsed later from its start.
type csvReader struct {
	br     *bufio.Reader
	off    int64
	state  int
	fields []string
	field  []byte
	nRunes int
}

func newCSVReader(r io.Reader, bufSize int) *csvReader {
	return &csvReader{br: bufio.NewReaderSize(r, bufSize)}
}

func (r *csvReader) save() {
	r.fields = append(r.fields, string(r.field))
	r.field = r.field[:0]
	r.nRunes = 0
}

func (r *csvReader) add(c rune) error {
	if r.nRunes >= fieldLimit {
		return ErrFieldTooLarge
	}
	r.field = utf8.AppendRune(r.field, c)
	r.nRunes++
	return nil
}

func isNL(c rune) bool { return c == '\n' || c == '\r' }

func (r *csvReader) process(c rune) error {
	switch r.state {
	case stStartRecord:
		if c == eol {
			return nil
		}
		if isNL(c) {
			r.state = stEatCRNL
			return nil
		}
		r.state = stStartField
		fallthrough
	case stStartField:
		switch {
		case isNL(c) || c == eol:
			r.save()
			if c == eol {
				r.state = stStartRecord
			} else {
				r.state = stEatCRNL
			}
		case c == '"':
			r.state = stInQuoted
		case c == ',':
			r.save()
		default:
			r.state = stInField
			return r.add(c)
		}
	case stInField:
		switch {
		case isNL(c) || c == eol:
			r.save()
			if c == eol {
				r.state = stStartRecord
			} else {
				r.state = stEatCRNL
			}
		case c == ',':
			r.save()
			r.state = stStartField
		default:
			return r.add(c)
		}
	case stInQuoted:
		switch {
		case c == eol:
		case c == '"':
			r.state = stQuoteInQuoted
		default:
			return r.add(c)
		}
	case stQuoteInQuoted:
		switch {
		case c == '"':
			r.state = stInQuoted
			return r.add(c)
		case c == ',':
			r.save()
			r.state = stStartField
		case isNL(c) || c == eol:
			r.save()
			if c == eol {
				r.state = stStartRecord
			} else {
				r.state = stEatCRNL
			}
		default:
			r.state = stInField
			return r.add(c)
		}
	case stEatCRNL:
		switch {
		case isNL(c):
		case c == eol:
			r.state = stStartRecord
		default:
			return fmt.Errorf("new-line character seen in unquoted field")
		}
	}
	return nil
}

// next returns the next record and the byte offset where it starts. A blank
// line gives an empty, non-nil-error record. io.EOF means no more records.
func (r *csvReader) next() ([]string, int64, error) {
	start := r.off
	r.state = stStartRecord
	r.fields = nil
	r.field = r.field[:0]
	r.nRunes = 0
	lineLen := 0
	for {
		c, size, err := r.br.ReadRune()
		if err == io.EOF {
			if lineLen > 0 {
				if err := r.process(eol); err != nil {
					return nil, start, err
				}
				if r.state == stStartRecord {
					return r.record(), start, nil
				}
			}
			if r.nRunes != 0 || r.state == stInQuoted {
				r.save()
				return r.record(), start, nil
			}
			return nil, start, io.EOF
		}
		if err != nil {
			return nil, start, err
		}
		if c == utf8.RuneError && size == 1 {
			return nil, start, ErrInvalidUTF8
		}
		r.off += int64(size)
		lineLen++
		if err := r.process(c); err != nil {
			return nil, start, err
		}
		endsLine := c == '\n'
		if c == '\r' {
			b, perr := r.br.Peek(1)
			endsLine = perr != nil || b[0] != '\n'
		}
		if endsLine {
			lineLen = 0
			if err := r.process(eol); err != nil {
				return nil, start, err
			}
			if r.state == stStartRecord {
				return r.record(), start, nil
			}
		}
	}
}

func (r *csvReader) record() []string {
	if r.fields == nil {
		return []string{}
	}
	return r.fields
}
