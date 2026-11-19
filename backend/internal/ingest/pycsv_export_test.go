package ingest

import (
	"reflect"
	"testing"
)

type pyRec struct {
	Fields  []string
	LineNum int
}

// Expected records and line numbers are Python 3.12's csv.reader(strict=False)
// over io.StringIO(s, newline="") and its reader.line_num, on this box.
func TestReadPyCSV(t *testing.T) {
	cases := []struct {
		name, in string
		want     []pyRec
	}{
		{"bare CR", "a,b\rc,d\r", []pyRec{{[]string{"a", "b"}, 1}, {[]string{"c", "d"}, 2}}},
		{"CRLF", "a,b\r\nc,d\r\n", []pyRec{{[]string{"a", "b"}, 1}, {[]string{"c", "d"}, 2}}},
		{"quote mid field", "\"1\"2,x\n", []pyRec{{[]string{"12", "x"}, 1}}},
		{"blank first line", "\na,b\nc,d\n", []pyRec{{[]string{}, 1}, {[]string{"a", "b"}, 2}, {[]string{"c", "d"}, 3}}},
		{"quoted two-line field", "h\n\"x\ny\",z\nw\n", []pyRec{{[]string{"h"}, 1}, {[]string{"x\ny", "z"}, 3}, {[]string{"w"}, 4}}},
		{"final line without newline", "a\nb", []pyRec{{[]string{"a"}, 1}, {[]string{"b"}, 2}}},
		{"U+001C kept by the reader", "\x1cid\x1c,v\n", []pyRec{{[]string{"\x1cid\x1c", "v"}, 1}}},
	}
	for _, c := range cases {
		var got []pyRec
		err := ReadPyCSV([]byte(c.in), func(f []string, lineNum, end int) error {
			got = append(got, pyRec{append([]string{}, f...), lineNum})
			return nil
		})
		if err != nil || !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: got %v, %v; want %v", c.name, got, err, c.want)
		}
	}
}

func TestReadPyCSVEndOffsets(t *testing.T) {
	var ends []int
	_ = ReadPyCSV([]byte("h\n\"x\ny\",z\r\nw"), func(_ []string, _, end int) error {
		ends = append(ends, end)
		return nil
	})
	if want := []int{2, 11, 12}; !reflect.DeepEqual(ends, want) {
		t.Errorf("ends = %v, want %v", ends, want)
	}
}

func TestReadPyCSVInvalidUTF8(t *testing.T) {
	err := ReadPyCSV([]byte("a\n\xff\n"), func([]string, int, int) error { return nil })
	if err != ErrInvalidUTF8 {
		t.Errorf("got %v, want ErrInvalidUTF8", err)
	}
}

func TestPyStrip(t *testing.T) {
	if got := PyStrip("\x1c a \x1f"); got != "a" {
		t.Errorf("PyStrip = %q, want %q", got, "a")
	}
}
