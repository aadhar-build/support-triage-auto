package eval

import (
	"os"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

func rt(id uint64, intent, action string, tiers ...string) route.RoutedThread {
	l := route.Labels{Intent: intent, Urgency: "normal", Action: action, Confidence: 0.9}
	return route.RoutedThread{ThreadID: id, Labels: l, CheapLabels: l, Tiers: tiers}
}

// handFixture is tests/test_evaluate.py's TestScore.hand_fixture.
func handFixture(t *testing.T) ([]route.RoutedThread, Gold) {
	routed := []route.RoutedThread{
		rt(101, "delivery", "ask", "cheap", "agent"),
		rt(102, "billing", "ask", "cheap", "larger", "agent"),
		rt(103, "complaint", "answer", "cheap", "agent"),
		rt(104, "account_access", "escalate", "cheap", "larger"),
		rt(105, "outage_technical", "escalate", "cheap", "larger"),
		rt(106, "other", "ask", "cheap", "agent"),
	}
	g, err := LoadGold(writeGold(t, goldHeader+
		"101,delivery,ask,false\n102,billing,route,false\n103,complaint,escalate,true\n"+
		"104,outage_technical,route,false\n105,outage_technical,escalate,true\n999,praise,answer,false\n"))
	if err != nil {
		t.Fatal(err)
	}
	return routed, g
}

func ptr[T any](v T) *T { return &v }

// assertReport compares against testdata/score_<name>.{json,md}, which are
// Python's report_to_json / report_to_markdown on the same inputs.
func assertReport(t *testing.T, name string, r Report) {
	t.Helper()
	js, err := ReportToJSON(r)
	if err != nil {
		t.Fatal(err)
	}
	for ext, got := range map[string]string{"json": string(js), "md": ReportToMarkdown(r)} {
		want, err := os.ReadFile("testdata/score_" + name + "." + ext)
		if err != nil {
			t.Fatal(err)
		}
		if got != string(want) {
			t.Errorf("%s.%s differs from Python:\n got: %s\nwant: %s", name, ext, got, want)
		}
	}
}

func TestScorePlaceholderHandFixture(t *testing.T) {
	routed, g := handFixture(t)
	r, err := Score(routed, g, nil, ScoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if r.ThreadsScored != 5 || r.GoldRowsUnmatched != 1 || r.MissedEscalations != 1 || r.FalseEscalations != 1 {
		t.Errorf("counts: %+v", r)
	}
	if *r.CostPer1000Threads != 7.666666666666667 || !r.PricesArePlaceholders {
		t.Errorf("cost %v placeholders %v", *r.CostPer1000Threads, r.PricesArePlaceholders)
	}
	assertReport(t, "placeholder", r)
	js, _ := ReportToJSON(r)
	for _, id := range []string{"101", "106", "999"} {
		// As a JSON value, a key, or a Markdown cell (digits inside floats don't count).
		if strings.Contains(string(js), ": "+id+",") || strings.Contains(string(js), "\""+id+"\"") ||
			strings.Contains(ReportToMarkdown(r), "| "+id+" |") {
			t.Errorf("report holds thread id %s", id)
		}
	}
}

func TestScoreOrderedPrices(t *testing.T) {
	routed, g := handFixture(t)
	prices := []TierPrice{{"agent", 0.5}, {"cheap", 0.001}, {"larger", 0.1}}
	r, err := Score(routed, g, prices, ScoreOptions{SampleSize: ptr[int64](3), Seed: ptr[int64](7), Split: ptr("dev"), Threshold: ptr(0.5)})
	if err != nil {
		t.Fatal(err)
	}
	assertReport(t, "ordered", r)
}

func TestScoreRealUsage(t *testing.T) {
	routed, g := handFixture(t)
	u := DefaultLargerUsage()
	u.LargerMode, u.LargerModel, u.LargerPromptVersion, u.LargerPostMergeEval = "real", ptr("m-1"), ptr("v2"), true
	u.LargerInputTokens, u.LargerOutputTokens, u.LargerCacheWriteTokens, u.LargerCacheReadTokens = 1200, 340, 5, 6
	u.LargerConfirmedUSD, u.LargerUnconfirmedUSD = 0.00375, 0.5
	u.MaxSpendUSD, u.MonthlyCapUSD, u.MonthToDateUSD = ptr(2.5), ptr(200.0), ptr(12.25)
	u.LargerAnswered, u.LargerReservationExceeded = 4, 1
	u.LargerFallbacks = []FallbackCount{{"zzz_new", 1}, {"api_error", 2}}
	r, err := Score(routed, g, nil, ScoreOptions{Usage: &u})
	if err != nil {
		t.Fatal(err)
	}
	assertReport(t, "real", r)
	md := ReportToMarkdown(r)
	for _, want := range []string{"| confirmed_usd | 0.0037 |", "| monthly_cap_usd | 200.0000 |"} {
		if !strings.Contains(md, want) {
			t.Errorf("markdown lacks %q", want)
		}
	}
}

func TestScoreEmptyAndExtraTier(t *testing.T) {
	r, err := Score(nil, Gold{}, nil, ScoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	assertReport(t, "empty", r)

	g, _ := LoadGold(writeGold(t, goldHeader+"1,delivery,ask,0\n"))
	routed := []route.RoutedThread{rt(1, "delivery", "ask", "cheap", "human"), rt(2, "delivery", "ask", "human")}
	prices := []TierPrice{{"cheap", 0.1}, {"larger", 0.2}, {"agent", 0.3}, {"human", 1.7}}
	r, err = Score(routed, g, prices, ScoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	assertReport(t, "extra_tier", r)
}

func TestScoreRefusals(t *testing.T) {
	routed := []route.RoutedThread{rt(1, "delivery", "ask", "cheap", "human")}
	if _, err := Score(routed, Gold{}, nil, ScoreOptions{}); err == nil || err.Error() != "no price given for tier 'human'" {
		t.Errorf("missing price: %v", err)
	}
	dup := []TierPrice{{"cheap", 0}, {"larger", 0.002}, {"agent", 0.01}, {"cheap", 0}}
	if _, err := Score(nil, Gold{}, dup, ScoreOptions{}); err == nil || err.Error() != "price for tier 'cheap' given twice" {
		t.Errorf("duplicate price: %v", err)
	}
	bad := []TierPrice{{"cheap", 0}, {"larger", -1}, {"agent", 0.01}}
	if _, err := Score(nil, Gold{}, bad, ScoreOptions{}); err == nil || err.Error() != "price for 'larger' must be a finite number >= 0, got -1.0" {
		t.Errorf("bad price: %v", err)
	}
}
