package fixturegen

import (
	"bytes"
	"encoding/csv"
	"regexp"
	"strings"
	"testing"
	"time"
)

// Hand-written copies of triage.tiers INTENTS and ACTIONS (re-exported by
// src/triage/evaluate.py). Keep in sync with the Python reference.
var allowedIntents = map[string]bool{
	"delivery": true, "billing": true, "account_access": true, "outage_technical": true,
	"complaint": true, "praise": true, "other": true,
}
var allowedActions = map[string]bool{"answer": true, "ask": true, "route": true, "escalate": true}

func generate(t *testing.T, seed int64, threads int) (twcs, gold []byte) {
	t.Helper()
	twcs, gold, err := Generate(seed, threads)
	if err != nil {
		t.Fatalf("Generate(%d, %d): %v", seed, threads, err)
	}
	return twcs, gold
}

func readCSV(t *testing.T, b []byte) [][]string {
	t.Helper()
	r := csv.NewReader(bytes.NewReader(b))
	r.FieldsPerRecord = -1
	rows, err := r.ReadAll()
	if err != nil {
		t.Fatalf("read csv: %v", err)
	}
	if len(rows) < 2 {
		t.Fatalf("csv has %d rows, want a header and data", len(rows))
	}
	return rows
}

func TestGenerateDeterministic(t *testing.T) {
	a1, g1 := generate(t, 65, 2000)
	a2, g2 := generate(t, 65, 2000)
	if !bytes.Equal(a1, a2) {
		t.Error("twcs bytes differ between two calls with the same seed")
	}
	if !bytes.Equal(g1, g2) {
		t.Error("gold bytes differ between two calls with the same seed")
	}
}

func TestGenerateSeedMatters(t *testing.T) {
	a1, g1 := generate(t, 65, 2000)
	a2, g2 := generate(t, 66, 2000)
	if bytes.Equal(a1, a2) {
		t.Error("twcs bytes are equal for seeds 65 and 66")
	}
	if bytes.Equal(g1, g2) {
		t.Error("gold bytes are equal for seeds 65 and 66")
	}
}

func TestGenerateRejectsNonPositiveThreads(t *testing.T) {
	if _, _, err := Generate(65, 0); err == nil {
		t.Error("Generate(65, 0) returned no error")
	}
}

func TestGenerateShape(t *testing.T) {
	twcs, gold := generate(t, 65, 2000)
	trows := readCSV(t, twcs)
	grows := readCSV(t, gold)

	wantT := "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id"
	if got := strings.Join(trows[0], ","); got != wantT {
		t.Errorf("twcs header = %q, want %q", got, wantT)
	}
	wantG := "thread_id,intent,action,should_escalate"
	if got := strings.Join(grows[0], ","); got != wantG {
		t.Errorf("gold header = %q, want %q", got, wantG)
	}

	roots := map[string]bool{}
	for _, r := range trows[1:] {
		if len(r) != 7 {
			t.Fatalf("twcs row has %d fields, want 7: %q", len(r), r)
		}
		if r[2] == "True" && r[6] == "" {
			roots[r[0]] = true
		}
	}
	if len(grows)-1 != 2000 {
		t.Errorf("gold has %d rows, want 2000 (one per thread)", len(grows)-1)
	}
	seen := map[string]bool{}
	for _, r := range grows[1:] {
		if len(r) != 4 {
			t.Fatalf("gold row has %d fields: %q", len(r), r)
		}
		if !roots[r[0]] {
			t.Errorf("gold thread_id %s is not a root tweet id in twcs", r[0])
		}
		if seen[r[0]] {
			t.Errorf("duplicate gold thread_id %s", r[0])
		}
		seen[r[0]] = true
		if !allowedIntents[r[1]] {
			t.Errorf("gold intent %q not allowed", r[1])
		}
		if !allowedActions[r[2]] {
			t.Errorf("gold action %q not allowed", r[2])
		}
		if r[3] != "true" && r[3] != "false" {
			t.Errorf("gold should_escalate %q is not true/false", r[3])
		}
	}
}

// The loader's date rule (src/triage/loader.py _DATE_RE), written out by hand.
var twcsDate = regexp.MustCompile(`^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{2}:\d{2}:\d{2} [+-]\d{4} \d{4}$`)

func TestGenerateCoverage(t *testing.T) {
	twcs, _ := generate(t, 65, 2000)
	rows := readCSV(t, twcs)[1:]

	brands := map[string]bool{}
	weeks := map[[2]int]bool{}
	invalid := 0
	byCustomerBrand := map[string]int{}
	multiTweet := 0
	for _, r := range rows {
		inbound := strings.ToLower(strings.TrimSpace(r[2]))
		if (inbound != "true" && inbound != "false") || !twcsDate.MatchString(r[3]) {
			invalid++
			continue
		}
		ts, err := time.Parse("Mon Jan 02 15:04:05 -0700 2006", r[3])
		if err != nil {
			t.Fatalf("valid-looking date %q does not parse: %v", r[3], err)
		}
		y, w := ts.UTC().ISOWeek()
		weeks[[2]int{y, w}] = true
		if inbound == "false" {
			brands[r[1]] = true
			multiTweet++
		}
		if inbound == "true" && r[6] == "" {
			m := regexp.MustCompile(`@([A-Za-z0-9_]+)`).FindStringSubmatch(r[4])
			if m != nil {
				byCustomerBrand[r[1]+"|"+m[1]]++
			}
		}
	}
	if len(brands) < 4 {
		t.Errorf("brands = %d, want >= 4", len(brands))
	}
	if len(weeks) < 6 {
		t.Errorf("ISO weeks = %d, want >= 6", len(weeks))
	}
	if invalid < 3 {
		t.Errorf("invalid rows = %d, want >= 3", invalid)
	}
	if multiTweet == 0 {
		t.Error("no brand replies, want multi-tweet threads")
	}
	repeat := 0
	for _, n := range byCustomerBrand {
		if n > 1 {
			repeat++
		}
	}
	if repeat == 0 {
		t.Error("no customer has more than one thread with the same brand")
	}
}
