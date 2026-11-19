package ingest

import (
	"bytes"
	"io"
)

// ReadPyCSV runs the CPython-compatible csv reader over data and yields every
// record (a blank line as zero fields) with Python's reader.line_num and end,
// the bytes of data consumed through the end of that record. Reader errors
// (ErrFieldTooLarge, ErrInvalidUTF8, ...) and yield's errors are returned
// unchanged.
func ReadPyCSV(data []byte, yield func(fields []string, lineNum, end int) error) error {
	r := newCSVReader(bytes.NewReader(data), 64*1024)
	lineNum, prev := 0, 0
	for {
		fields, _, err := r.next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		end := int(r.off)
		lineNum += countLineEnds(data, prev, end)
		prev = end
		if err := yield(fields, lineNum, end); err != nil {
			return err
		}
	}
}

// countLineEnds counts the line ends in data[from:to] as Python's line_num
// does: \r\n once, a bare \r or \n once, and a final unterminated line once.
// A \r at to-1 followed by \n at to is counted when the \n is reached.
func countLineEnds(data []byte, from, to int) int {
	n := 0
	for i := from; i < to; i++ {
		switch data[i] {
		case '\n':
			n++
		case '\r':
			if i+1 >= len(data) || data[i+1] != '\n' {
				n++
			}
		}
	}
	if to > from && to == len(data) && data[to-1] != '\n' && data[to-1] != '\r' {
		n++
	}
	return n
}

// PyStrip is Python's str.strip() with no arguments.
func PyStrip(s string) string { return pyStrip(s) }
