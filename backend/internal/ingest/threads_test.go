package ingest

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
)

var base = time.Date(2017, 10, 31, 22, 0, 0, 0, time.UTC)

type tweetSpec struct {
	id      uint64
	author  string
	inbound bool
	minute  int
	text    string
	resp    []uint64
	irt     uint64
	hasIRT  bool
}

func cust(id uint64, minute int) tweetSpec {
	return tweetSpec{id: id, author: "cust_a", inbound: true, minute: minute}
}

func brandTw(id uint64, minute int) tweetSpec {
	return tweetSpec{id: id, author: "BrandA", minute: minute}
}

func (s tweetSpec) to(p uint64) tweetSpec         { s.irt, s.hasIRT = p, true; return s }
func (s tweetSpec) lists(ids ...uint64) tweetSpec { s.resp = ids; return s }
func (s tweetSpec) by(a string) tweetSpec         { s.author = a; return s }
func (s tweetSpec) saying(text string) tweetSpec  { s.text = text; return s }
func twitterDate(t time.Time) string              { return t.Format("Mon Jan 2 15:04:05 -0700 2006") }
func writeCSV(t *testing.T, body string) string   { return writeFile(t, "in.csv", body) }
func writeFile(t *testing.T, n, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), n)
	must(t, os.WriteFile(p, []byte(body), 0o600))
	return p
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func csvOf(specs []tweetSpec) string {
	var b strings.Builder
	b.WriteString(hdr)
	for _, s := range specs {
		text := s.text
		if text == "" {
			text = "SENTINEL_TEXT_" + strconv.FormatUint(s.id, 10)
		}
		var resp []string
		for _, r := range s.resp {
			resp = append(resp, strconv.FormatUint(r, 10))
		}
		irt := ""
		if s.hasIRT {
			irt = strconv.FormatUint(s.irt, 10)
		}
		inb := "False"
		if s.inbound {
			inb = "True"
		}
		fmt.Fprintf(&b, "%d,%s,%s,%s,%s,\"%s\",%s\n", s.id, s.author, inb,
			twitterDate(base.Add(time.Duration(s.minute)*time.Minute)), text, strings.Join(resp, ","), irt)
	}
	return b.String()
}

func build(t *testing.T, csv string) ([]Thread, ThreadStats) {
	t.Helper()
	var got []Thread
	st, err := BuildThreads(writeCSV(t, csv), Options{TempDir: t.TempDir()}, func(th Thread) error {
		got = append(got, th)
		return nil
	})
	must(t, err)
	return got, st
}

func roots(ths []Thread) []uint64 {
	var r []uint64
	for _, th := range ths {
		r = append(r, th.RootID)
	}
	return r
}

func turnIDs(th Thread) []uint64 {
	var r []uint64
	for _, tu := range th.Turns {
		r = append(r, tu.ID)
	}
	return r
}

func odd(st ThreadStats) [7]int {
	return [7]int{st.DuplicateIDs, st.BrokenLinks, st.Cycles, st.CycleTweets, st.BrandStarted, st.ParentLinkDisagreements, st.ResponseIDFallbacks}
}

func mixedFixture() []tweetSpec {
	return []tweetSpec{
		cust(1, 0).lists(2),
		brandTw(2, 1).to(1).lists(3),
		cust(3, 1).to(2),
		cust(4, 1).lists(5),
		brandTw(5, 1).by("BrandB"),
		brandTw(6, 2).to(999),
		cust(7, 3).to(6),
		cust(8, 4).to(9),
		brandTw(9, 5).to(8),
		cust(10, 6).to(10),
		brandTw(11, 7),
		cust(12, 8).to(11),
		cust(1, 9).saying("SENTINEL_TEXT_DUP"),
		cust(13, 10).lists(14),
		cust(15, 10).lists(14),
		brandTw(14, 11).to(13),
		cust(16, 1),
	}
}

func TestMixedFixture(t *testing.T) {
	ths, st := build(t, csvOf(mixedFixture()))
	want := ThreadStats{TweetsSeen: 16, TweetsPlaced: 9, TweetsNotPlaced: 7, ThreadsBuilt: 5, DuplicateIDs: 1,
		BrokenLinks: 1, Cycles: 2, CycleTweets: 3, BrandStarted: 1, ParentLinkDisagreements: 1, ResponseIDFallbacks: 1}
	want.Load = st.Load
	if !reflect.DeepEqual(st, want) {
		t.Errorf("stats %+v\nwant  %+v", st, want)
	}
	if st.Load.RowsParsed != 17 || st.Load.RowsRead != 17 {
		t.Errorf("load %+v", st.Load)
	}
	if got := roots(ths); !reflect.DeepEqual(got, []uint64{1, 4, 16, 13, 15}) {
		t.Errorf("roots %v", got)
	}
	if got := turnIDs(ths[0]); !reflect.DeepEqual(got, []uint64{1, 2, 3}) {
		t.Errorf("turns %v (ties broken by id)", got)
	}
	if !reflect.DeepEqual(ths[1].BrandIDs, []string{"BrandB"}) || !ths[1].BrandReplied {
		t.Errorf("thread 4 brands %v", ths[1].BrandIDs)
	}
	if ths[0].Turns[0].Text != "SENTINEL_TEXT_1" || ths[0].Turns[0].AuthorID != "cust_a" {
		t.Errorf("first copy not kept: %+v", ths[0].Turns[0])
	}
	if ths[2].BrandReplied || len(ths[2].BrandIDs) != 0 {
		t.Errorf("thread 16 %+v", ths[2])
	}
}

func TestDeepChain(t *testing.T) {
	const n = 5000
	var specs []tweetSpec
	for i := uint64(n); i >= 2; i-- {
		s := cust(i, int(i)).to(i - 1)
		if i%2 == 0 {
			s = brandTw(i, int(i)).to(i - 1)
		}
		specs = append(specs, s)
	}
	specs = append(specs, cust(1, 0))
	ths, st := build(t, csvOf(specs))
	if len(ths) != 1 || len(ths[0].Turns) != n || odd(st) != [7]int{} {
		t.Fatalf("threads %d, stats %+v", len(ths), st)
	}
	for i, tu := range ths[0].Turns {
		if tu.ID != uint64(i+1) {
			t.Fatalf("turn %d is %d", i, tu.ID)
		}
	}
}

func TestLongCycle(t *testing.T) {
	const n = 5000
	var specs []tweetSpec
	for i := uint64(1); i <= n; i++ {
		specs = append(specs, cust(i, int(i)).to(i%n+1))
	}
	ths, st := build(t, csvOf(specs))
	if len(ths) != 0 || st.Cycles != 1 || st.CycleTweets != n || st.TweetsNotPlaced != n {
		t.Fatalf("threads %d, stats %+v", len(ths), st)
	}
}

func TestFirstWinsDuplicates(t *testing.T) {
	ths, st := build(t, csvOf([]tweetSpec{
		cust(1, 0).saying("SENTINEL_TEXT_FIRST"),
		cust(2, 1),
		cust(1, 0).saying("SENTINEL_TEXT_SECOND").lists(2),
	}))
	if !reflect.DeepEqual(roots(ths), []uint64{1, 2}) || ths[0].Turns[0].Text != "SENTINEL_TEXT_FIRST" {
		t.Fatalf("threads %+v", ths)
	}
	if odd(st) != [7]int{1, 0, 0, 0, 0, 0, 0} || st.TweetsSeen != 2 {
		t.Errorf("stats %+v", st)
	}
}

func TestMinListerFallback(t *testing.T) {
	ths, st := build(t, csvOf([]tweetSpec{
		cust(7, 0).by("cust_b").lists(9),
		cust(4, 1).lists(9),
		brandTw(9, 2),
	}))
	if !reflect.DeepEqual(roots(ths), []uint64{7, 4}) || !reflect.DeepEqual(turnIDs(ths[1]), []uint64{4, 9}) {
		t.Fatalf("threads %+v", ths)
	}
	if odd(st) != [7]int{0, 0, 0, 0, 0, 1, 1} {
		t.Errorf("stats %+v", st)
	}
}

func TestOddCounters(t *testing.T) {
	cases := map[string]struct {
		specs []tweetSpec
		roots []uint64
		odd   [7]int
	}{
		"broken link":  {[]tweetSpec{cust(1, 0), brandTw(2, 1).to(999).lists(3), cust(3, 2).to(2)}, []uint64{1}, [7]int{0, 1, 0, 0, 0, 0, 0}},
		"two cycles":   {[]tweetSpec{cust(1, 0), cust(100, 1).to(101), brandTw(101, 2).to(100), cust(102, 3).to(101), cust(200, 4).to(200)}, []uint64{1}, [7]int{0, 0, 2, 3, 0, 0, 0}},
		"brand start":  {[]tweetSpec{brandTw(1, 0).lists(2), cust(2, 1).to(1), cust(3, 2)}, []uint64{3}, [7]int{0, 0, 0, 0, 1, 0, 0}},
		"disagreement": {[]tweetSpec{cust(1, 0).lists(3), cust(5, 1).by("cust_b").lists(3), brandTw(3, 2).to(1)}, []uint64{1, 5}, [7]int{0, 0, 0, 0, 0, 1, 0}},
		"fallback":     {[]tweetSpec{cust(1, 0).lists(2, 777), brandTw(2, 1)}, []uint64{1}, [7]int{0, 0, 0, 0, 0, 0, 1}},
		"self listing": {[]tweetSpec{cust(5, 0).to(5).lists(5), brandTw(6, 1).to(5)}, nil, [7]int{0, 0, 1, 1, 0, 0, 0}},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			ths, st := build(t, csvOf(c.specs))
			if !reflect.DeepEqual(roots(ths), c.roots) || odd(st) != c.odd {
				t.Errorf("roots %v odd %v, want %v %v", roots(ths), odd(st), c.roots, c.odd)
			}
		})
	}
}

func TestTurnTiesByID(t *testing.T) {
	// Same instant written with two offsets: the tie is broken by id.
	csv := hdr +
		"30,cust_a,True,Tue Oct 31 22:00:00 +0000 2017,x,,\n" +
		"32,BrandA,False,Tue Oct 31 17:01:00 -0500 2017,x,,30\n" +
		"31,cust_a,True,Tue Oct 31 23:01:00 +0100 2017,x,,30\n" +
		"20,cust_b,True,Tue Oct 31 23:00:00 +0100 2017,x,,\n"
	ths, _ := build(t, csv)
	if !reflect.DeepEqual(roots(ths), []uint64{20, 30}) || !reflect.DeepEqual(turnIDs(ths[1]), []uint64{30, 31, 32}) {
		t.Fatalf("roots %v turns %v", roots(ths), turnIDs(ths[1]))
	}
	if got := isoformat(ths[1].Turns[2].CreatedAt); got != "2017-10-31T17:01:00-05:00" {
		t.Errorf("offset not kept: %s", got)
	}
}

func assertEmptyDir(t *testing.T, dir string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	must(t, err)
	if len(entries) != 0 {
		t.Errorf("%s holds %d entries, want none", dir, len(entries))
	}
}

func TestFatalInputs(t *testing.T) {
	long := strings.Repeat("é", 131073)
	cases := map[string]string{
		"long field":   hdr + "1,cust_a,True," + okDate + "," + long + ",,\n",
		"invalid utf8": hdr + "1,cust_a,True," + okDate + ",bad\xff\xfe,,\n",
		"huge id":      hdr + "99999999999999999999999,cust_a,True," + okDate + ",x,,\n",
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			tmp := t.TempDir()
			called := false
			_, err := BuildThreads(writeCSV(t, csvOK()+body[len(hdr):]), Options{TempDir: tmp}, func(Thread) error { called = true; return nil })
			if err == nil || called {
				t.Errorf("err %v, emit called %v", err, called)
			}
			assertEmptyDir(t, tmp)
		})
	}
	ths, _ := build(t, hdr+"1,cust_a,True,"+okDate+","+strings.Repeat("é", 131072)+",,\n")
	if len(ths) != 1 || len([]rune(ths[0].Turns[0].Text)) != 131072 {
		t.Errorf("131072-character field did not load")
	}
}

// csvOK is a header plus one good row, so fatal rows come after a valid one.
func csvOK() string { return hdr + "5,cust_a,True," + okDate + ",fine,,\n" }

func TestTempFiles(t *testing.T) {
	const textMarker = "UNIQUETEXTMARKER7731"
	const authorMarker = "UNIQUEAUTHORMARKER4419"
	csv := csvOf([]tweetSpec{
		cust(1, 0).saying(textMarker).by(authorMarker).lists(2),
		brandTw(2, 1).to(1).saying(textMarker),
	})
	tmp := t.TempDir()
	checked := false
	_, err := BuildThreads(writeCSV(t, csv), Options{TempDir: tmp}, func(Thread) error {
		entries, err := os.ReadDir(tmp)
		must(t, err)
		if len(entries) != 1 || !entries[0].IsDir() {
			t.Fatalf("TempDir holds %v, want one dir", entries)
		}
		dir := filepath.Join(tmp, entries[0].Name())
		st, _ := os.Stat(dir)
		if st.Mode().Perm() != 0o700 {
			t.Errorf("dir mode %v", st.Mode().Perm())
		}
		files, _ := os.ReadDir(dir)
		if len(files) == 0 {
			t.Error("no index files while emitting")
		}
		for _, f := range files {
			p := filepath.Join(dir, f.Name())
			fi, _ := os.Stat(p)
			if fi.Mode().Perm() != 0o600 {
				t.Errorf("%s mode %v", f.Name(), fi.Mode().Perm())
			}
			b, _ := os.ReadFile(p)
			if bytes.Contains(b, []byte(textMarker)) || bytes.Contains(b, []byte(authorMarker)) {
				t.Errorf("%s holds tweet text or an author id", f.Name())
			}
		}
		checked = true
		return nil
	})
	must(t, err)
	if !checked {
		t.Fatal("emit never ran")
	}
	assertEmptyDir(t, tmp)

	stop := errors.New("stop")
	_, err = BuildThreads(writeCSV(t, csv), Options{TempDir: tmp}, func(Thread) error { return stop })
	if !errors.Is(err, stop) {
		t.Errorf("emit error not returned: %v", err)
	}
	assertEmptyDir(t, tmp)

	_, err = BuildThreads(writeCSV(t, "tweet_id,author_id\n1,a\n"), Options{TempDir: tmp}, func(Thread) error { return nil })
	var he *HeaderError
	if !errors.As(err, &he) {
		t.Errorf("err %v, want HeaderError", err)
	}
	assertEmptyDir(t, tmp)

	_, err = BuildThreads(writeCSV(t, csvOK()+"1,c,True,"+okDate+",bad\xff,,\n"), Options{TempDir: tmp}, func(Thread) error { return nil })
	if err == nil {
		t.Error("fatal input gave no error")
	}
	assertEmptyDir(t, tmp)
}
