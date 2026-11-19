package policy

import (
	"errors"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// Every thread, customer and brand here is made up by hand. Expected values
// are the literals of tests/test_context_signals.py, never recomputed.

var day0 = time.Date(2021, 3, 1, 9, 0, 0, 0, time.UTC)

const week = 7 * 24 * time.Hour

var knownBrands = []string{"ShopCo", "OtherBrand"}

func day(n int, hours ...int) time.Time {
	h := 0
	if len(hours) > 0 {
		h = hours[0]
	}
	return day0.Add(time.Duration(n)*24*time.Hour + time.Duration(h)*time.Hour)
}

type reply struct {
	brand string
	at    time.Time
}

// realThread is Python's real_thread: one inbound first turn, then brand replies.
func realThread(root uint64, customer string, start time.Time, text string, replies ...reply) ingest.Thread {
	th := ingest.Thread{RootID: root, Turns: []ingest.Turn{{ID: root, AuthorID: customer, Inbound: true, CreatedAt: start, Text: text}}}
	for i, r := range replies {
		th.Turns = append(th.Turns, ingest.Turn{ID: root + uint64(i) + 1, AuthorID: r.brand, CreatedAt: r.at, Text: "made-up brand reply"})
		th.BrandIDs = append(th.BrandIDs, r.brand)
	}
	th.BrandReplied = len(replies) > 0
	return th
}

type tturn struct {
	author  string
	inbound bool
	at      time.Time
	text    string
}

// turnsThread is Python's turns_thread.
func turnsThread(root uint64, turns ...tturn) ingest.Thread {
	th := ingest.Thread{RootID: root}
	for i, t := range turns {
		th.Turns = append(th.Turns, ingest.Turn{ID: root + uint64(i), AuthorID: t.author, Inbound: t.inbound, CreatedAt: t.at, Text: t.text})
		if !t.inbound {
			th.BrandIDs = append(th.BrandIDs, t.author)
			th.BrandReplied = true
		}
	}
	return th
}

// rec is Python's rec helper.
func rec(root uint64, customer string, start time.Time, brands []string, replies ...BrandReply) ContactRecord {
	b := slices.Clone(brands)
	slices.Sort(b)
	if b == nil {
		b = []string{}
	}
	if replies == nil {
		replies = []BrandReply{}
	}
	return ContactRecord{RootID: root, Customer: customer, Start: start, BrandsAtStart: b, BrandReplies: replies}
}

var rule = map[string]string{"window_days": "7", "min_conversations": "3", "effect": `"escalate"`, "report_window_hours": "72"}

var ruleKeys = []string{"window_days", "min_conversations", "effect", "report_window_hours"}

// ruleJSON renders a repeat_contact block in Python's dict(RULE, ...) order,
// with overrides (raw JSON) and a dropped key.
func ruleJSON(over map[string]string, drop string) string {
	var parts []string
	for _, k := range ruleKeys {
		if k == drop {
			continue
		}
		v := rule[k]
		if o, ok := over[k]; ok {
			v = o
		}
		parts = append(parts, `"`+k+`": `+v)
	}
	return `{"repeat_contact": {` + strings.Join(parts, ", ") + `}}`
}

func writeFile(t *testing.T, dir, name, body string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

const pre = "repeat_contact rule in "

func TestLoadRepeatContactErrors(t *testing.T) {
	dir := t.TempDir()
	cases := []struct{ name, body, problem string }{
		{"not-json", "{not json", "not valid JSON"},
		{"no-block", `{"public_reply_rules": []}`, "'repeat_contact' is missing or is not an object"},
		{"block-not-object", `{"repeat_contact": [1]}`, "'repeat_contact' is missing or is not an object"},
		{"min-below-2", ruleJSON(map[string]string{"min_conversations": "1"}, ""), "'min_conversations' must be an int of at least 2"},
		{"min-float", ruleJSON(map[string]string{"min_conversations": "3.0"}, ""), "'min_conversations' must be an int of at least 2"},
		{"effect-other", ruleJSON(map[string]string{"effect": `"notify"`}, ""), `'effect' must be "escalate"`},
		{"bool-min", ruleJSON(map[string]string{"min_conversations": "true"}, ""), "'min_conversations' must be an int of at least 2"},
		{"bool-window", ruleJSON(map[string]string{"window_days": "true"}, ""), "'window_days' must be a finite number above 0"},
		{"bool-report", ruleJSON(map[string]string{"report_window_hours": "true"}, ""), "'report_window_hours' must be a finite number above 0"},
		{"zero-window", ruleJSON(map[string]string{"window_days": "0"}, ""), "'window_days' must be a finite number above 0"},
		{"str-window", ruleJSON(map[string]string{"window_days": `"7"`}, ""), "'window_days' must be a finite number above 0"},
	}
	for _, k := range ruleKeys {
		cases = append(cases, struct{ name, body, problem string }{"no-" + k, ruleJSON(nil, k), "'" + k + "' is missing"})
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := writeFile(t, dir, c.name+".json", c.body)
			_, err := LoadRepeatContactRule(p)
			var rce *RepeatContactError
			if !errors.As(err, &rce) {
				t.Fatalf("err %v (%T), want *RepeatContactError", err, err)
			}
			if want := pre + p + ": " + c.problem; err.Error() != want {
				t.Errorf("got %q\nwant %q", err.Error(), want)
			}
		})
	}
	t.Run("missing", func(t *testing.T) {
		p := filepath.Join(dir, "missing.json")
		_, err := LoadRepeatContactRule(p)
		var rce *RepeatContactError
		if !errors.As(err, &rce) {
			t.Fatalf("err %v, want *RepeatContactError", err)
		}
		msg := err.Error()
		if head := pre + p + ": cannot read file ("; !strings.HasPrefix(msg, head) || !strings.HasSuffix(msg, ")") {
			t.Errorf("got %q, want %q...)", msg, head)
		}
	})
}

func TestRepeatContactNonStandardJSON(t *testing.T) {
	dir := t.TempDir()
	for name, lit := range map[string]string{"inf": "Infinity", "ninf": "-Infinity", "nan": "NaN", "surrogate": `"\ud800"`} {
		p := writeFile(t, dir, name+".json", ruleJSON(map[string]string{"report_window_hours": lit}, ""))
		_, err := LoadRepeatContactRule(p)
		if want := pre + p + ": not valid JSON"; err == nil || err.Error() != want {
			t.Errorf("%s: got %v, want %q", name, err, want)
		}
	}
	for name, body := range map[string]string{"bom": "\ufeff" + ruleJSON(nil, ""), "utf8": "{\"x\": \"\xff\"}"} {
		p := writeFile(t, dir, name+".json", body)
		_, err := LoadRepeatContactRule(p)
		if want := pre + p + ": not valid JSON"; err == nil || err.Error() != want {
			t.Errorf("%s: got %v, want %q", name, err, want)
		}
	}
	p := writeFile(t, dir, "big.json", ruleJSON(map[string]string{"report_window_hours": "1e400"}, ""))
	_, err := LoadRepeatContactRule(p)
	if want := pre + p + ": 'report_window_hours' must be a finite number above 0"; err == nil || err.Error() != want {
		t.Errorf("1e400: got %v, want %q", err, want)
	}
	p = writeFile(t, dir, "tiny.json", ruleJSON(map[string]string{"window_days": "1e-400"}, ""))
	_, err = LoadRepeatContactRule(p)
	if want := pre + p + ": 'window_days' must be a finite number above 0"; err == nil || err.Error() != want {
		t.Errorf("1e-400: got %v, want %q", err, want)
	}
}

func TestLoadRepeatContactOK(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "ok.json", `{"repeat_contact": {"window_days": 7, "min_conversations": 3, "effect": "escalate", "report_window_hours": 72, "note": "made-up note"}, "other": 1}`)
	r, err := LoadRepeatContactRule(p)
	if err != nil {
		t.Fatal(err)
	}
	if r.WindowDays.String() != "7" || !r.WindowDays.IsInt || r.WindowDays.Float != 7 ||
		r.MinConversations != 3 || r.ReportWindowHours.String() != "72" || r.Effect != "escalate" {
		t.Errorf("got %+v", r)
	}
	frac, err := LoadRepeatContactRule(writeFile(t, dir, "frac.json", ruleJSON(map[string]string{"window_days": "1.5"}, "")))
	if err != nil || frac.WindowDays.Float != 1.5 || frac.WindowDays.IsInt || frac.WindowDays.String() != "1.5" {
		t.Errorf("1.5: got %+v, %v", frac, err)
	}
	for _, lit := range []string{"7.0", "7e0"} {
		r, err := LoadRepeatContactRule(writeFile(t, dir, "f.json", ruleJSON(map[string]string{"window_days": lit}, "")))
		if err != nil || r.WindowDays.String() != "7.0" || r.WindowDays.IsInt {
			t.Errorf("%s: got %+v, %v", lit, r, err)
		}
	}
	r, err = LoadRepeatContactRule(writeFile(t, dir, "f.json", ruleJSON(map[string]string{"report_window_hours": "1.50"}, "")))
	if err != nil || r.ReportWindowHours.String() != "1.5" {
		t.Errorf("1.50: got %+v, %v", r, err)
	}
	big := writeFile(t, dir, "big.json", ruleJSON(map[string]string{"min_conversations": "99999999999999999999"}, ""))
	_, err = LoadRepeatContactRule(big)
	if want := pre + big + ": 'min_conversations' must be an int of at least 2"; err == nil || err.Error() != want {
		t.Errorf("big int: got %v, want %q", err, want)
	}
}

func TestRepeatContactFromData(t *testing.T) {
	root := parity.RepoRoot(t)
	fixture := filepath.Join(root, "tests", "fixtures", "repeat_contact_rules.json")
	b, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	data, err := ParseJSON(b)
	if err != nil {
		t.Fatal(err)
	}
	got, err := RepeatContactFromData(data, fixture)
	if err != nil {
		t.Fatal(err)
	}
	want, err := LoadRepeatContactRule(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("from data %+v, loader %+v", got, want)
	}
	other, _ := ParseJSON([]byte(`{"public_reply_rules": []}`))
	_, err = RepeatContactFromData(other, "made-up-source")
	if want := "repeat_contact rule in made-up-source: 'repeat_contact' is missing or is not an object"; err == nil || err.Error() != want {
		t.Errorf("got %v, want %q", err, want)
	}
}

func TestContactRecordOf(t *testing.T) {
	r, ok := ContactRecordOf(realThread(10, "cust_zed", day(0), "@shopco where is my parcel"), knownBrands)
	if want := rec(10, "cust_zed", day(0), []string{"ShopCo"}); !ok || !reflect.DeepEqual(r, want) {
		t.Errorf("got %+v %v, want %+v", r, ok, want)
	}
	two, _ := ContactRecordOf(realThread(11, "cust_zed", day(0), "@SHOPCO and @otherbrand help"), knownBrands)
	if want := []string{"OtherBrand", "ShopCo"}; !reflect.DeepEqual(two.BrandsAtStart, want) {
		t.Errorf("two: got %v, want %v", two.BrandsAtStart, want)
	}
	none := map[string]ingest.Thread{
		"longer handle":   realThread(12, "c", day(0), "@ShopCoUK hello"),
		"unknown mention": realThread(13, "c", day(0), "@Unknown hello"),
		"unknown reply":   realThread(14, "c", day(0), "hello", reply{"Unknown", day(0, 1)}),
		"outbound first": {RootID: 15, BrandIDs: []string{"ShopCo"}, BrandReplied: true,
			Turns: []ingest.Turn{{ID: 15, AuthorID: "ShopCo", CreatedAt: day(0), Text: "@ShopCo news"}}},
		"no turns": {RootID: 16},
	}
	for name, th := range none {
		if r, ok := ContactRecordOf(th, knownBrands); ok {
			t.Errorf("%s: got %+v, want none", name, r)
		}
	}
	only, ok := ContactRecordOf(realThread(17, "c", day(0), "hello", reply{"ShopCo", day(0, 2)}), knownBrands)
	if !ok || len(only.BrandsAtStart) != 0 || !reflect.DeepEqual(only.BrandReplies, []BrandReply{{"ShopCo", day(0, 2)}}) {
		t.Errorf("reply only: got %+v %v", only, ok)
	}

	plus2 := time.FixedZone("+02:00", 2*3600)
	utc, _ := ContactRecordOf(realThread(1, "c", time.Date(2021, 3, 1, 11, 0, 0, 0, plus2), "@ShopCo hi"), knownBrands)
	if !utc.Start.Equal(day(0)) || utc.Start.Location() != time.UTC {
		t.Errorf("start %v (%v), want %v UTC", utc.Start, utc.Start.Location(), day(0))
	}
}

func TestContactRecordCaseCollision(t *testing.T) {
	th := realThread(1, "c", day(0), "@shopco hi", reply{"ShopCo", day(0, 1)}, reply{"SHOPCO", day(0, 2)})
	for _, known := range [][]string{{"SHOPCO", "ShopCo"}, {"ShopCo", "SHOPCO"}} {
		r, ok := ContactRecordOf(th, known)
		if !ok || !reflect.DeepEqual(r.BrandsAtStart, []string{"SHOPCO"}) ||
			!reflect.DeepEqual(r.BrandReplies, []BrandReply{{"SHOPCO", day(0, 2)}}) {
			t.Errorf("known %v: got %+v %v", known, r, ok)
		}
	}
}

func threeRecords(t *testing.T) []ContactRecord {
	t.Helper()
	var out []ContactRecord
	for _, d := range []int{0, 2, 5} {
		r, ok := ContactRecordOf(realThread(uint64(100+d), "cust_amber", day(d), "@ShopCo where is my parcel"), []string{"ShopCo"})
		if !ok {
			t.Fatal("no record")
		}
		out = append(out, r)
	}
	return out
}

func checkCounts(t *testing.T, name string, got, want map[uint64]int) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Errorf("%s: got %v, want %v", name, got, want)
	}
}

func TestContactCounts(t *testing.T) {
	shop, other := []string{"ShopCo"}, []string{"OtherBrand"}
	records := threeRecords(t)
	want := map[uint64]int{100: 1, 102: 2, 105: 3}
	checkCounts(t, "no look-ahead", ContactCounts(records, week), want)
	for seed := int64(0); seed < 5; seed++ {
		sh := slices.Clone(records)
		rand.New(rand.NewSource(seed)).Shuffle(len(sh), func(i, j int) { sh[i], sh[j] = sh[j], sh[i] })
		checkCounts(t, "shuffled", ContactCounts(sh, week), want)
	}
	checkCounts(t, "first alone", ContactCounts(records[:1], week), map[uint64]int{100: 1})

	checkCounts(t, "tie-break", ContactCounts([]ContactRecord{rec(9, "c", day(0), shop), rec(7, "c", day(0), shop), rec(8, "c", day(0), shop)}, week),
		map[uint64]int{7: 1, 8: 2, 9: 3})

	checkCounts(t, "reply only", ContactCounts([]ContactRecord{rec(1, "c", day(0), shop), rec(2, "c", day(1), nil, BrandReply{"ShopCo", day(1, 1)})}, week),
		map[uint64]int{1: 1, 2: 0})

	x := rec(3, "c", day(2), shop)
	for _, c := range []struct {
		at   time.Time
		want int
	}{{day(3), 1}, {day(1), 2}, {day(2), 2}} {
		got := ContactCounts([]ContactRecord{rec(1, "c", day(0), nil, BrandReply{"ShopCo", c.at}), x}, week)[3]
		if got != c.want {
			t.Errorf("known by then (reply %v): got %d, want %d", c.at, got, c.want)
		}
	}

	checkCounts(t, "own reply", ContactCounts([]ContactRecord{rec(5, "c", day(0), nil, BrandReply{"ShopCo", day(0)})}, week), map[uint64]int{5: 0})
	checkCounts(t, "own reply other", ContactCounts([]ContactRecord{rec(6, "c", day(0), other, BrandReply{"ShopCo", day(0)})}, week), map[uint64]int{6: 1})

	wk := append(threeRecords(t), rec(109, "cust_amber", day(9), shop), rec(120, "cust_amber", day(20), shop),
		rec(201, "cust_amber", day(5, 1), other), rec(301, "cust_birch", day(5, 2), shop))
	checkCounts(t, "window and keys", ContactCounts(wk, week), map[uint64]int{100: 1, 102: 2, 105: 3, 109: 3, 120: 1, 201: 1, 301: 1})
	checkCounts(t, "inclusive edge", ContactCounts([]ContactRecord{rec(1, "c", day(0), shop), rec(2, "c", day(7), shop)}, week), map[uint64]int{1: 1, 2: 2})

	checkCounts(t, "multi-brand", ContactCounts([]ContactRecord{rec(1, "c", day(0), shop), rec(2, "c", day(1), shop),
		rec(3, "c", day(2), other), rec(4, "c", day(3), []string{"ShopCo", "OtherBrand"})}, week), map[uint64]int{1: 1, 2: 2, 3: 1, 4: 3})

	in := threeRecords(t)
	before := slices.Clone(in)
	ContactCounts(in, week)
	if !reflect.DeepEqual(in, before) {
		t.Error("input mutated")
	}
	checkCounts(t, "empty", ContactCounts(nil, week), map[uint64]int{})

	checkCounts(t, "duplicate root", ContactCounts([]ContactRecord{rec(1, "c", day(0), shop), rec(1, "c", day(1), shop)}, week), map[uint64]int{1: 2})

	plus2 := time.FixedZone("+02:00", 2*3600)
	checkCounts(t, "non-UTC start", ContactCounts([]ContactRecord{rec(1, "c", day(0), shop), rec(2, "c", day(7).In(plus2), shop)}, week), map[uint64]int{1: 1, 2: 2})
}

func TestBrandAttribution(t *testing.T) {
	known := []string{"AlphaCo", "BetaCo"}
	cases := []struct {
		name string
		th   ingest.Thread
		want string
	}{
		{"first tweet mention any case", turnsThread(100, tturn{"cust_a", true, day(0), "@ALPHACO my order is late"}), "brand"},
		{"brand reply before last inbound", turnsThread(110, tturn{"cust_a", true, day(0), "my order is late"},
			tturn{"AlphaCo", false, day(0, 1), "made-up brand reply"}, tturn{"cust_a", true, day(0, 2), "still late"}), "brand"},
		{"late brand reply, earlier other", turnsThread(120, tturn{"cust_a", true, day(0), "my order is late"},
			tturn{"BetaCo", false, day(0, 1), "made-up brand reply"}, tturn{"cust_a", true, day(0, 2), "still late"},
			tturn{"AlphaCo", false, day(0, 3), "made-up brand reply"}), "other_brand"},
		{"other brand first tweet mention", turnsThread(130, tturn{"cust_a", true, day(0), "@BetaCo my order is late"}), "other_brand"},
		{"only brand reply after last inbound", turnsThread(140, tturn{"cust_a", true, day(0), "my order is late"},
			tturn{"AlphaCo", false, day(0, 1), "made-up brand reply"}), "unattributed"},
		{"outbound first turn", turnsThread(150, tturn{"AlphaCo", false, day(0), "made-up brand notice"},
			tturn{"cust_a", true, day(0, 1), "@alphaco thanks"}), "unattributed"},
		{"mention outside first tweet", turnsThread(160, tturn{"cust_a", true, day(0), "my order is late"},
			tturn{"cust_a", true, day(0, 1), "@alphaco hello?"}), "unattributed"},
		// Go-only: the moment is the last inbound turn in turn order, not the latest time.
		{"last inbound in turn order", turnsThread(170, tturn{"cust_a", true, day(0, 5), "my order is late"},
			tturn{"AlphaCo", false, day(0, 2), "made-up brand reply"}, tturn{"cust_a", true, day(0, 1), "still late"}), "unattributed"},
	}
	for _, c := range cases {
		if got := BrandAttribution(c.th, "alphaco", known); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}
