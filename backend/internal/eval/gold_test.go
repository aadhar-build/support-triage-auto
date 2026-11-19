package eval

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
)

const goldHeader = "thread_id,intent,action,should_escalate\n"

func writeGold(t *testing.T, data string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "gold.csv")
	if err := os.WriteFile(p, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func goldMap(g Gold) map[string]GoldRow {
	out := map[string]GoldRow{}
	for _, id := range g.IDs() {
		out[id] = g.rows[id]
	}
	return out
}

func assertGoldErr(t *testing.T, name, data, want string) {
	t.Helper()
	_, err := LoadGold(writeGold(t, data))
	var ge *GoldFileError
	if !errors.As(err, &ge) || ge.Msg != want {
		t.Errorf("%s: got %v, want GoldFileError %q", name, err, want)
	}
}

// Mirrors tests/test_evaluate.py::TestLoadGold.
func TestLoadGoldPythonSuite(t *testing.T) {
	g, err := LoadGold(writeGold(t, "note,thread_id,intent,action,should_escalate,extra\n"+
		"a,1,delivery,ask,false,x\n"+
		"b,2,billing,route,TRUE,y\n"+
		"c,3,outage_technical,escalate,1,z\n"))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]GoldRow{
		"1": {"delivery", "ask", false},
		"2": {"billing", "route", true},
		"3": {"outage_technical", "escalate", true},
	}
	if !reflect.DeepEqual(goldMap(g), want) || g.Len() != 3 {
		t.Errorf("valid gold = %v", goldMap(g))
	}
	if r, ok := g.Lookup(2); !ok || r != want["2"] {
		t.Errorf("Lookup(2) = %v %v", r, ok)
	}
	if g, err := LoadGold(writeGold(t, goldHeader)); err != nil || g.Len() != 0 {
		t.Errorf("header only: %v %v", g.Len(), err)
	}
	if g, err := LoadGold(writeGold(t, "\ufeff"+goldHeader+"7,billing,ask,0\n")); err != nil || g.Len() != 1 {
		t.Errorf("BOM: %v %v", g.Len(), err)
	}
	assertGoldErr(t, "missing column", "thread_id,intent,action\n1,delivery,ask\n",
		"gold file is missing column(s): should_escalate")
	for bad, repr := range map[string]string{"abc": "'abc'", "-1": "'-1'", "1.5": "'1.5'", "": "''"} {
		assertGoldErr(t, "bad id "+bad, goldHeader+"1,delivery,ask,0\n"+bad+",delivery,ask,0\n",
			"line 3: thread_id must be a non-negative integer, got "+repr)
	}
	assertGoldErr(t, "duplicate", goldHeader+"1,delivery,ask,0\n1,billing,ask,0\n", "line 3: duplicate thread_id 1")
	assertGoldErr(t, "unknown intent", goldHeader+"1,weather,ask,0\n", "line 2: unknown intent 'weather'")
	assertGoldErr(t, "unknown action", goldHeader+"1,delivery,ignore,0\n", "line 2: unknown action 'ignore'")
	for bad, repr := range map[string]string{"yes": "'yes'", "": "''", "2": "'2'", "t": "'t'"} {
		assertGoldErr(t, "bad bool "+bad, goldHeader+"1,delivery,ask,"+bad+"\n",
			"line 2: should_escalate must be true/false/1/0, got "+repr)
	}
	assertGoldErr(t, "empty", "", "gold file is empty: expected columns thread_id, intent, action, should_escalate")
	assertGoldErr(t, "short row", goldHeader+"1,delivery\n", "line 2: row has too few fields")
	if _, err := LoadGold(filepath.Join(t.TempDir(), "nope.csv")); !errors.Is(err, fs.ErrNotExist) {
		t.Errorf("missing file: %v", err)
	}
}

// Outcomes from Python's load_gold on this box, on the same bytes.
func TestLoadGoldCSVQuirks(t *testing.T) {
	empty := "gold file is empty: expected columns thread_id, intent, action, should_escalate"
	assertGoldErr(t, "blank first line", "\n"+goldHeader+"1,delivery,ask,0\n", empty)
	assertGoldErr(t, "blank only", "\n", empty)
	assertGoldErr(t, "missing two", "thread_id,action\n", "gold file is missing column(s): intent, should_escalate")
	extra := strings.Replace(goldHeader, "\n", ",x\n", 1)
	assertGoldErr(t, "quoted two-line field", extra+"1,delivery,ask,0,\"p\nq\"\n2,weather,ask,0,y\n", "line 4: unknown intent 'weather'")
	assertGoldErr(t, "bad row is itself a quoted two-line record", extra+"1,weather,ask,0,\"p\nq\"\n", "line 3: unknown intent 'weather'")
	assertGoldErr(t, "blank before bad row", goldHeader+"\n1,weather,ask,0\n", "line 3: unknown intent 'weather'")
	assertGoldErr(t, "007 then 7", goldHeader+"007,delivery,ask,0\n7,delivery,ask,0\n", "line 3: duplicate thread_id 7")
	assertGoldErr(t, "delivéry", goldHeader+"1,delivéry,ask,0\n", "line 2: unknown intent 'delivéry'")
	crlf := strings.ReplaceAll(goldHeader, "\n", "\r\n")
	assertGoldErr(t, "CRLF line 3", crlf+"1,delivery,ask,0\r\n2,weather,ask,0\r\n", "line 3: unknown intent 'weather'")
	assertGoldErr(t, "final line without newline", goldHeader+"1,delivery,ask,0\n2,weather,ask,0", "line 3: unknown intent 'weather'")
	assertGoldErr(t, "duplicate header, short row", "thread_id,intent,action,should_escalate,intent\n1,billing,ask,0\n", "line 2: row has too few fields")
	assertGoldErr(t, "bad bool repr of stripped value", goldHeader+"1,delivery,ask, Yes \n", "line 2: should_escalate must be true/false/1/0, got 'Yes'")
	assertGoldErr(t, "non-ASCII digit id", goldHeader+"1,delivery,ask,0\n١,delivery,ask,0\n", "line 3: thread_id must be a non-negative integer, got '١'")

	loads := []struct {
		name, data string
		want       map[string]GoldRow
	}{
		{"bare CR", strings.ReplaceAll(goldHeader, "\n", "\r") + "1,delivery,ask,0\r2,billing,route,true\r",
			map[string]GoldRow{"1": {"delivery", "ask", false}, "2": {"billing", "route", true}}},
		{"quote mid id", goldHeader + "\"1\"2,delivery,ask,0\n", map[string]GoldRow{"12": {"delivery", "ask", false}}},
		{"stray quote in extra column", extra + "1,delivery,ask,0,a\"b\n", map[string]GoldRow{"1": {"delivery", "ask", false}}},
		{"id above 2^64-1", goldHeader + "99999999999999999999999,delivery,ask,0\n",
			map[string]GoldRow{"99999999999999999999999": {"delivery", "ask", false}}},
		{"U+001C padding", "\x1cthread_id\x1c,intent,action,should_escalate\n\x1c5\x1c,delivery,ask,0\n",
			map[string]GoldRow{"5": {"delivery", "ask", false}}},
		{"duplicate header, last wins", "thread_id,intent,action,should_escalate,intent\n1,weather,ask,0,billing\n",
			map[string]GoldRow{"1": {"billing", "ask", false}}},
		{"whitespace stripped", " thread_id , intent ,action,should_escalate\n 3 , billing , ask , TRUE \n",
			map[string]GoldRow{"3": {"billing", "ask", true}}},
	}
	for _, c := range loads {
		g, err := LoadGold(writeGold(t, c.data))
		if err != nil || !reflect.DeepEqual(goldMap(g), c.want) {
			t.Errorf("%s: got %v, %v; want %v", c.name, goldMap(g), err, c.want)
		}
	}
	if g, _ := LoadGold(writeGold(t, goldHeader+"99999999999999999999999,delivery,ask,0\n")); g.Len() != 1 {
		t.Error("huge id should load")
	}
}

func TestLoadGoldDecodeAndFieldErrors(t *testing.T) {
	var de *GoldDecodeError
	_, err := LoadGold(writeGold(t, goldHeader+"1,delivery,ask,0\n\xff"))
	if !errors.As(err, &de) || errors.Is(err, ingest.ErrInvalidUTF8) {
		t.Errorf("invalid UTF-8: got %v", err)
	}
	_, err = LoadGold(writeGold(t, goldHeader+"1,delivery,ask,0,"+strings.Repeat("a", 131073)+"\n"))
	if !errors.Is(err, ingest.ErrFieldTooLarge) {
		t.Errorf("field too large: got %v", err)
	}
}

// pad mirrors the Python fixture builder: prefix plus one filler row
// "2,delivery,ask,0,aaa...\n" so the result is exactly upto bytes.
func pad(prefix string, upto int) string {
	head := "2,delivery,ask,0,"
	return prefix + head + strings.Repeat("a", upto-len(prefix)-len(head)-1) + "\n"
}

// Outcomes from Python's load_gold on this box on the same bytes ("decode"
// means UnicodeDecodeError).
func TestLoadGoldDecodePrecedence(t *testing.T) {
	const bad = "1,weather,ask,0\n"
	const bom = "\xef\xbb\xbf"
	h := goldHeader
	hcr := strings.ReplaceAll(goldHeader, "\n", "\r")
	trim := func(s string) string { return s[:len(s)-1] }
	weather2 := "line 2: unknown intent 'weather'"
	weather3 := "line 3: unknown intent 'weather'"
	cases := []struct {
		name, data, want string
	}{
		{"bad row, invalid byte in a later chunk", pad(h+bad, 9000) + "\xff\n", weather2},
		{"bad row, invalid byte in the same chunk", h + bad + "2,delivery,ask,0\n\xff\n", "decode"},
		{"multi-byte split by the chunk boundary", trim(pad(h+bad, 8191)) + "\xc3\xa9\n3,delivery,ask,0\n", weather2},
		{"truncated tail at end of file after an LF row", h + bad + "x\xc3", weather2},
		{"lone truncated byte at end of file after an LF row", h + bad + "\xc3", weather2},
		{"truncated tail at end of file after a CR row", hcr + "1,weather,ask,0\r\xc3", "decode"},
		{"invalid byte at end of file after the bad row", h + bad + "\xff", "decode"},
		{"BOM: raw offsets keep the invalid byte in chunk 2", pad(bom+h+bad, 8193) + "\xff\n", weather2},
		{"BOM: raw offsets put the bad row in chunk 2", trim(pad(bom+h, 8194)) + "\n9,weather,ask,0\n" + strings.Repeat("z", 20) + "\xff\n", "decode"},
		{"row ends on the boundary with LF", pad(h, 8192-len(bad)) + bad + "\xff\n", weather3},
		{"row ends on the boundary with CR", trim(pad(hcr, 8192-len(bad))) + "\r" + trim(bad) + "\r" + "\xff\r", "decode"},
		{"truncated tail in chunk 2 after a boundary LF row", pad(h, 8192-len(bad)) + bad + "\xc3", weather3},
		{"valid file, invalid byte after the last row", h + "1,delivery,ask,0\n\xff", "decode"},
		{"valid file, invalid byte in a later chunk", pad(h+"1,delivery,ask,0\n", 9000) + "\xfe", "decode"},
	}
	for _, c := range cases {
		_, err := LoadGold(writeGold(t, c.data))
		var de *GoldDecodeError
		var ge *GoldFileError
		switch {
		case c.want == "decode":
			if !errors.As(err, &de) {
				t.Errorf("%s: got %v, want GoldDecodeError", c.name, err)
			}
		case !errors.As(err, &ge) || ge.Msg != c.want:
			t.Errorf("%s: got %v, want %q", c.name, err, c.want)
		}
	}
}
