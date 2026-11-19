package ingest

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf16"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// isoformat is Python datetime.isoformat() for a second-precision aware time.
func isoformat(t time.Time) string {
	_, off := t.Zone()
	sign := '+'
	if off < 0 {
		sign, off = '-', -off
	}
	return fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d%c%02d:%02d", t.Year(), int(t.Month()), t.Day(),
		t.Hour(), t.Minute(), t.Second(), sign, off/3600, off%3600/60)
}

// pyString is json.dumps(s, ensure_ascii=True).
func pyString(s string) string {
	var b strings.Builder
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
			case r >= 0x20 && r <= 0x7e:
				b.WriteRune(r)
			case r > 0xffff:
				r1, r2 := utf16.EncodeRune(r)
				fmt.Fprintf(&b, `\u%04x\u%04x`, r1, r2)
			default:
				fmt.Fprintf(&b, `\u%04x`, r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// pyFloat is Python float repr (as json.dumps writes finite floats).
func pyFloat(f float64) string {
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64) // [-]d[.ddd]e±XX
	mant, expS, _ := strings.Cut(e, "e")
	exp, _ := strconv.Atoi(expS)
	neg := strings.HasPrefix(mant, "-")
	mant = strings.TrimPrefix(mant, "-")
	digits := strings.Replace(mant, ".", "", 1)
	var out string
	if exp >= -4 && exp < 16 {
		if exp < 0 {
			out = "0." + strings.Repeat("0", -exp-1) + digits
		} else if len(digits) <= exp+1 {
			out = digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
		} else {
			out = digits[:exp+1] + "." + digits[exp+1:]
		}
	} else {
		sign := "+"
		if exp < 0 {
			sign, exp = "-", -exp
		}
		out = mant + "e" + sign + fmt.Sprintf("%02d", exp)
	}
	if neg {
		out = "-" + out
	}
	return out
}

// kv is an object member; pretty renders json.dumps(sort_keys=True, indent=2).
type kv struct {
	k string
	v any // string (raw JSON scalar) or []kv
}

func pretty(b *strings.Builder, v any, level int) {
	switch x := v.(type) {
	case string:
		b.WriteString(x)
	case []kv:
		if len(x) == 0 {
			b.WriteString("{}")
			return
		}
		sort.Slice(x, func(i, j int) bool { return x[i].k < x[j].k })
		b.WriteString("{\n")
		for i, m := range x {
			b.WriteString(strings.Repeat("  ", level+1) + pyString(m.k) + ": ")
			pretty(b, m.v, level+1)
			if i < len(x)-1 {
				b.WriteString(",")
			}
			b.WriteString("\n")
		}
		b.WriteString(strings.Repeat("  ", level) + "}")
	}
}

func itoa(n int) string { return strconv.Itoa(n) }

func fractionJSON(f Fraction) string {
	if f.IsInt {
		v, _ := strconv.ParseInt(f.Literal, 10, 64)
		return strconv.FormatInt(v, 10)
	}
	return pyFloat(f.Value)
}

func pyBool(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

// canonical runs BuildThreads plus a Splitter on csvPath and renders the two
// golden files.
func canonical(t *testing.T, csvPath string) (threadsJSONL, statsJSON []byte) {
	t.Helper()
	cfg, err := LoadSplitConfig(filepath.Join(parity.RepoRoot(t), "data", "split.json"))
	must(t, err)
	sp := NewSplitter(cfg)
	var lines bytes.Buffer
	st, err := BuildThreads(csvPath, Options{TempDir: t.TempDir()}, func(th Thread) error {
		var brands, turns []string
		for _, id := range th.BrandIDs {
			brands = append(brands, pyString(id))
		}
		for _, tu := range th.Turns {
			sum := sha256.Sum256([]byte(tu.Text))
			turns = append(turns, `{"author_id":`+pyString(tu.AuthorID)+`,"created_at":`+pyString(isoformat(tu.CreatedAt))+
				`,"inbound":`+pyBool(tu.Inbound)+`,"text_sha256":"`+hex.EncodeToString(sum[:])+`","tweet_id":"`+
				strconv.FormatUint(tu.ID, 10)+`"}`)
		}
		fmt.Fprintf(&lines, `{"brand_ids":[%s],"brand_replied":%s,"root_tweet_id":"%d","split":"%s","turns":[%s]}`+"\n",
			strings.Join(brands, ","), pyBool(th.BrandReplied), th.RootID, sp.Add(th.RootID), strings.Join(turns, ","))
		return nil
	})
	must(t, err)
	rep := sp.Report()
	var reasons []kv
	for k, n := range st.Load.SkipReasons {
		reasons = append(reasons, kv{k, itoa(n)})
	}
	var counts, shares, targets []kv
	for i, n := range SplitNames {
		counts = append(counts, kv{n, itoa(rep.Counts[n])})
		shares = append(shares, kv{n, pyFloat(rep.Shares[n])})
		targets = append(targets, kv{n, fractionJSON(rep.Targets[i])})
	}
	obj := []kv{
		{"load", []kv{{"rows_read", itoa(st.Load.RowsRead)}, {"rows_parsed", itoa(st.Load.RowsParsed)},
			{"rows_skipped", itoa(st.Load.RowsSkipped)}, {"skip_reasons", reasons}}},
		{"threads", []kv{{"tweets_seen", itoa(st.TweetsSeen)}, {"duplicate_ids", itoa(st.DuplicateIDs)},
			{"threads_built", itoa(st.ThreadsBuilt)}, {"tweets_placed", itoa(st.TweetsPlaced)},
			{"tweets_not_placed", itoa(st.TweetsNotPlaced)}, {"broken_links", itoa(st.BrokenLinks)},
			{"cycles", itoa(st.Cycles)}, {"cycle_tweets", itoa(st.CycleTweets)}, {"brand_started", itoa(st.BrandStarted)},
			{"parent_link_disagreements", itoa(st.ParentLinkDisagreements)}, {"response_id_fallbacks", itoa(st.ResponseIDFallbacks)}}},
		{"split", []kv{{"seed", strconv.FormatInt(rep.Seed, 10)}, {"total", itoa(rep.Total)},
			{"counts", counts}, {"shares", shares}, {"targets", targets}}},
	}
	var sb strings.Builder
	pretty(&sb, obj, 0)
	sb.WriteString("\n")
	return lines.Bytes(), []byte(sb.String())
}

func TestPyFloat(t *testing.T) {
	cases := map[float64]string{0: "0.0", 1: "1.0", 0.1: "0.1", 0.05: "0.05", 1.0 / 3: "0.3333333333333333",
		5e-05: "5e-05", 1e16: "1e+16", 0.15000000000000002: "0.15000000000000002", 1234567.0: "1234567.0", 1.5e-7: "1.5e-07"}
	for f, want := range cases {
		if got := pyFloat(f); got != want {
			t.Errorf("pyFloat(%v) = %s, want %s", f, got, want)
		}
	}
}

func TestPyString(t *testing.T) {
	cases := map[string]string{
		"<a&b>":       "\"<a&b>\"",
		"\u00e9":      "\"\\u00e9\"",
		"\u2028":      "\"\\u2028\"",
		"\U0001F600":  "\"\\ud83d\\ude00\"",
		"\x1c":        "\"\\u001c\"",
		"q\"\\\n\r\t": "\"q\\\"\\\\\\n\\r\\t\"",
		"\b\f\x7f":    "\"\\b\\f\\u007f\"",
	}
	for in, want := range cases {
		if got := pyString(in); got != want {
			t.Errorf("pyString(%q) = %s, want %s", in, got, want)
		}
	}
}

func TestPrettyEmpty(t *testing.T) {
	var b strings.Builder
	pretty(&b, []kv{{"b", "1"}, {"a", []kv{}}}, 0)
	if want := "{\n  \"a\": {},\n  \"b\": 1\n}"; b.String() != want {
		t.Errorf("got %q, want %q", b.String(), want)
	}
}

func TestParityGenerated(t *testing.T) {
	twcs, _, _ := parity.Inputs(t)
	th, st := canonical(t, twcs)
	parity.AssertGolden(t, "ingest/generated.threads.jsonl", th)
	parity.AssertGolden(t, "ingest/generated.stats.json", st)
}

func oddPath(t *testing.T) string {
	return filepath.Join(parity.RepoRoot(t), "testdata", "ingest", "odd_cases.twcs.txt")
}

func TestParityOddCases(t *testing.T) {
	th, st := canonical(t, oddPath(t))
	parity.AssertGolden(t, "ingest/odd_cases.threads.jsonl", th)
	parity.AssertGolden(t, "ingest/odd_cases.stats.json", st)
}

// TestOddCasesCoverage checks that the committed odd-case golden exercises
// every branch. Cases in testdata/ingest/odd_cases.twcs.txt, by row id:
//
//	100-103  a 4-turn thread with two brands (oc_BrandX, oc_BrandY); 100 has
//	         CRLF inside quotes, 101 has "" inside quotes, 102 has "ab"cd,
//	         103 has ab"cd
//	200      offset date -0530 and a text of ` "a"` (space before a quote)
//	199/200  root-time tie (same instant, different offsets) broken by id
//	300      ends with a bare CR; 301 is reached by the min-lister fallback
//	         (a blank line follows 301)
//	400/2    both list 402, whose in_response_to is 400: a disagreement
//	500/501  broken link (500 replies to a missing 999999)
//	600/601  two-tweet cycle; 602 is a self-reply cycle
//	700/701  brand-started thread
//	second 100  duplicate id (first wins)
//	`""` line, 800 (short row), 801 (long row)  field_count
//	x12 tweet_id, 802 author_id, 803 inbound, 804 created_at (Feb 30),
//	805 response_tweet_id ("1,,2"), 806 in_response_to_tweet_id
//	1, 2     roots that land in dev and eval
func TestOddCasesCoverage(t *testing.T) {
	var st struct {
		Load struct {
			SkipReasons map[string]int `json:"skip_reasons"`
		} `json:"load"`
		Threads map[string]int `json:"threads"`
	}
	must(t, json.Unmarshal(readGolden(t, "ingest/odd_cases.stats.json"), &st))
	for _, r := range []string{"field_count", "tweet_id", "author_id", "inbound", "created_at", "response_tweet_id", "in_response_to_tweet_id"} {
		if st.Load.SkipReasons[r] < 1 {
			t.Errorf("skip reason %s = %d, want >= 1", r, st.Load.SkipReasons[r])
		}
	}
	for _, c := range []string{"duplicate_ids", "broken_links", "cycles", "brand_started", "parent_link_disagreements", "response_id_fallbacks"} {
		if st.Threads[c] < 1 {
			t.Errorf("%s = %d, want >= 1", c, st.Threads[c])
		}
	}
	if st.Threads["cycle_tweets"] <= st.Threads["cycles"] {
		t.Errorf("cycle_tweets %d <= cycles %d", st.Threads["cycle_tweets"], st.Threads["cycles"])
	}
	long, twoBrands := false, false
	sc := bufio.NewScanner(bytes.NewReader(readGolden(t, "ingest/odd_cases.threads.jsonl")))
	for sc.Scan() {
		var th struct {
			BrandIDs []string          `json:"brand_ids"`
			Turns    []json.RawMessage `json:"turns"`
		}
		must(t, json.Unmarshal(sc.Bytes(), &th))
		long = long || len(th.Turns) >= 3
		twoBrands = twoBrands || len(th.BrandIDs) == 2
	}
	if !long || !twoBrands {
		t.Errorf("thread with >=3 turns: %v, with 2 brands: %v", long, twoBrands)
	}
	fx, err := os.ReadFile(oddPath(t))
	must(t, err)
	for name, quirk := range map[string]string{
		"CRLF in quotes": "\"made up line one\r\nline two\"", `""`: `""hi""`, `"ab"cd`: `,"ab"cd,`,
		`ab"cd`: `,ab"cd,`, ` "a"`: `, "a",`, "bare CR": ",\r3", "blank line": "\n\n", `""-only line`: "\n\"\"\n",
		"-0530": "-0530", "short row": "\n800,oc_cust10,True\n", "long row": ",,,extra\n",
	} {
		if !bytes.Contains(fx, []byte(quirk)) {
			t.Errorf("fixture lacks %s", name)
		}
	}
	if !bytes.HasPrefix(fx, []byte(hdr)) {
		t.Error("fixture does not start with the header")
	}
}

func readGolden(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(parity.GoldenPath(t, name))
	must(t, err)
	return b
}
