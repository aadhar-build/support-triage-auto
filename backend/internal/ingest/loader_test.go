package ingest

import (
	"errors"
	"io"
	"reflect"
	"strings"
	"testing"
)

const hdr = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"
const okDate = "Tue Oct 31 22:10:47 +0000 2017"

func load(t *testing.T, csv string) ([]Tweet, LoadStats, error) {
	t.Helper()
	var got []Tweet
	st, err := ReadTweets(strings.NewReader(csv), func(tw Tweet) error { got = append(got, tw); return nil })
	return got, st, err
}

func records(t *testing.T, s string) [][]string {
	t.Helper()
	r := newCSVReader(strings.NewReader(s), 16)
	var out [][]string
	for {
		rec, _, err := r.next()
		if err == io.EOF {
			return out
		}
		if err != nil {
			t.Fatalf("csv %q: %v", s, err)
		}
		out = append(out, rec)
	}
}

func TestHeaderErrors(t *testing.T) {
	empty := "empty file or header: expected columns tweet_id, author_id, inbound, created_at, text, response_tweet_id, in_response_to_tweet_id"
	cases := map[string]struct{ in, msg string }{
		"empty file":  {"", empty},
		"blank first": {"\n" + hdr + "1,a,True," + okDate + ",x,,\n", empty},
		"two columns": {"tweet_id,author_id\n1,a\n", "header is missing columns: inbound, created_at, text, response_tweet_id, in_response_to_tweet_id"},
		"bom":         {"\xef\xbb\xbf" + hdr + "1,a,True," + okDate + ",x,,\n", "header is missing columns: tweet_id"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			got, _, err := load(t, c.in)
			var he *HeaderError
			if !errors.As(err, &he) || he.Error() != c.msg {
				t.Fatalf("err = %v, want HeaderError %q", err, c.msg)
			}
			if len(got) != 0 {
				t.Errorf("%d records read", len(got))
			}
		})
	}
}

func TestHeaderFlexible(t *testing.T) {
	in := "extra,in_response_to_tweet_id,text,created_at,inbound,author_id,response_tweet_id,tweet_id\n" +
		"z,,hi," + okDate + ",True,cust,,5\n"
	got, st, err := load(t, in)
	if err != nil || len(got) != 1 || got[0].ID != 5 || got[0].Text != "hi" || got[0].AuthorID != "cust" {
		t.Fatalf("reordered: %v %+v %+v", err, got, st)
	}
	dup := "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id,text\n" +
		"5,cust,True," + okDate + ",first,,,last\n" +
		"6,cust,True," + okDate + ",first,,\n"
	got, st, err = load(t, dup)
	if err != nil || len(got) != 1 || got[0].Text != "last" {
		t.Fatalf("duplicate column: %v %+v", err, got)
	}
	if st.SkipReasons["field_count"] != 1 {
		t.Errorf("7-wide row under an 8-wide header: %v", st.SkipReasons)
	}
}

func row7(id, author, inbound, date, resp, irt string) string {
	return id + "," + author + "," + inbound + "," + date + ",made up text," + resp + "," + irt + "\n"
}

func expectRow(t *testing.T, line string, reason string) Tweet {
	t.Helper()
	got, st, err := load(t, hdr+line)
	if err != nil {
		t.Fatalf("%q: %v", line, err)
	}
	if reason == "" {
		if len(got) != 1 {
			t.Fatalf("%q: skipped %v, want parsed", line, st.SkipReasons)
		}
		return got[0]
	}
	if len(got) != 0 || st.SkipReasons[reason] != 1 || st.RowsSkipped != 1 {
		t.Fatalf("%q: parsed %d, reasons %v, want skip %s", line, len(got), st.SkipReasons, reason)
	}
	return Tweet{}
}

func TestParseIDs(t *testing.T) {
	for _, id := range []string{"012", "12.000", " 12", " 12\x1c"} {
		if tw := expectRow(t, row7(id, "c", "True", okDate, "", ""), ""); tw.ID != 12 {
			t.Errorf("%q -> %d", id, tw.ID)
		}
	}
	for _, id := range []string{"12.5", "\uff11\uff12", "-1"} {
		expectRow(t, row7(id, "c", "True", okDate, "", ""), "tweet_id")
	}
	tw := expectRow(t, row7("1", "c", "True", okDate, `"7, 8.0"`, "3"), "")
	if !reflect.DeepEqual(tw.ResponseIDs, []uint64{7, 8}) || !tw.HasInReplyTo || tw.InReplyTo != 3 {
		t.Errorf("links %+v", tw)
	}
	for _, r := range []string{`"1,,2"`, `"1,"`} {
		expectRow(t, row7("1", "c", "True", okDate, r, ""), "response_tweet_id")
	}
	expectRow(t, row7("1", "c", "True", okDate, "", "x"), "in_response_to_tweet_id")
	expectRow(t, row7("1", " \t", "True", okDate, "", ""), "author_id")
}

func TestParseInbound(t *testing.T) {
	for in, want := range map[string]bool{" TRUE ": true, "false": false, "False": false} {
		if tw := expectRow(t, row7("1", "c", in, okDate, "", ""), ""); tw.Inbound != want {
			t.Errorf("%q -> %v", in, tw.Inbound)
		}
	}
	for _, in := range []string{"faL\u017fe", "yes"} {
		expectRow(t, row7("1", "c", in, okDate, "", ""), "inbound")
	}
}

func TestParseDates(t *testing.T) {
	ok := map[string]string{
		"Mon Feb 29 10:00:00 +0000 2016": "2016-02-29T10:00:00+00:00",
		"Mon Jan 1 10:00:00 -0530 2017":  "2017-01-01T10:00:00-05:30",
		"Mon Jan 1 10:00:00 +2359 2017":  "2017-01-01T10:00:00+23:59",
	}
	for in, want := range ok {
		tw := expectRow(t, row7("1", "c", "True", in, "", ""), "")
		if got := isoformat(tw.CreatedAt); got != want {
			t.Errorf("%q -> %s, want %s", in, got, want)
		}
	}
	for _, in := range []string{
		"Wed Feb 29 10:00:00 +0000 2017",
		"Mon Jan 1 10:00:60 +0000 2017",
		"Mon Jan 1 10:00:00 +0000 0000",
		"Mon Jan  1 10:00:00 +0000 2017",
		"Mon Jan 1 10:00:00 +2400 2017",
		"Mon Jan 1 10:00:00 +0060 2017",
	} {
		expectRow(t, row7("1", "c", "True", in, "", ""), "created_at")
	}
}

func TestParseCheckOrder(t *testing.T) {
	expectRow(t, row7("x", " ", "maybe", "bad", `"1,"`, "y"), "tweet_id")
	expectRow(t, row7("1", " ", "maybe", "bad", `"1,"`, "y"), "author_id")
	expectRow(t, row7("1", "c", "maybe", "bad", `"1,"`, "y"), "inbound")
	expectRow(t, row7("1", "c", "True", "bad", `"1,"`, "y"), "created_at")
	expectRow(t, row7("1", "c", "True", okDate, `"1,"`, "y"), "response_tweet_id")
	expectRow(t, "1,c,True\n", "field_count")
}

func TestCSVQuirks(t *testing.T) {
	cases := []struct {
		in   string
		want [][]string
	}{
		{"\"a\r\nb\",\"x\"\"y\"\n", [][]string{{"a\r\nb", `x"y`}}},
		{"\"a\rb\"\n\"a\nb\"\n", [][]string{{"a\rb"}, {"a\nb"}}},
		{`"ab"cd` + "\n", [][]string{{"abcd"}}},
		{`ab"cd` + "\n", [][]string{{`ab"cd`}}},
		{` "a"` + "\n", [][]string{{` "a"`}}},
		{"a\rb\nc\r\nd", [][]string{{"a"}, {"b"}, {"c"}, {"d"}}},
		{`"abc`, [][]string{{"abc"}}},
		{"a\x00b,c\n", [][]string{{"a\x00b", "c"}}},
		{"a\n\n\"\"\nb,\n", [][]string{{"a"}, {}, {""}, {"b", ""}}},
	}
	for _, c := range cases {
		if got := records(t, c.in); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%q: got %q, want %q", c.in, got, c.want)
		}
	}
}

func TestCSVBlankAndQuotedOnlyLines(t *testing.T) {
	in := hdr + "\n" + row7("1", "c", "True", okDate, "", "") + "\"\"\n\r\n"
	got, st, err := load(t, in)
	if err != nil || len(got) != 1 || st.RowsRead != 2 || st.SkipReasons["field_count"] != 1 {
		t.Fatalf("err %v, got %d, stats %+v", err, len(got), st)
	}
}

func TestCSVOffsets(t *testing.T) {
	in := "a,b\r\"x\ny\",z\n\nlast"
	r := newCSVReader(strings.NewReader(in), 16)
	var offs []int64
	for {
		rec, off, err := r.next()
		if err == io.EOF {
			break
		}
		if len(rec) > 0 {
			offs = append(offs, off)
		}
	}
	if want := []int64{0, 4, 13}; !reflect.DeepEqual(offs, want) {
		t.Errorf("offsets %v, want %v", offs, want)
	}
}
