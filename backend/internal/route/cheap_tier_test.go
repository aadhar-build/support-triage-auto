package route

import (
	"reflect"
	"testing"
)

func TestCheapTables(t *testing.T) {
	var keys []string
	for _, ik := range IntentKeywords {
		keys = append(keys, ik.Intent)
	}
	if !reflect.DeepEqual(keys, []string{"delivery", "billing", "account_access", "outage_technical", "complaint", "praise"}) {
		t.Errorf("IntentKeywords order %v", keys)
	}
	if !reflect.DeepEqual(IntentPriority, []string{"account_access", "outage_technical", "billing", "delivery", "complaint", "praise"}) {
		t.Errorf("IntentPriority %v", IntentPriority)
	}
	if NoMatchConfidence != 0.2 || TieConfidence != 0.4 {
		t.Errorf("confidences %v %v", NoMatchConfidence, TieConfidence)
	}
	if !reflect.DeepEqual(ConfidenceByHits, map[int]float64{1: 0.6, 2: 0.75, 3: 0.9}) {
		t.Errorf("ConfidenceByHits %v", ConfidenceByHits)
	}
	if maxN != 2 {
		t.Errorf("maxN = %d, want 2", maxN)
	}
}

func TestCheapSingleKeywordPerIntent(t *testing.T) {
	cases := map[string]string{
		"delivery":         "where is my parcel",
		"billing":          "please send the invoice",
		"account_access":   "i forgot my password",
		"outage_technical": "is there an outage",
		"complaint":        "the agent was rude",
		"praise":           "i love it",
	}
	for intent, text := range cases {
		lab := label(t, cust(text))
		if lab.Intent != intent || lab.Confidence != 0.6 {
			t.Errorf("%q: got %+v", text, lab)
		}
	}
}

func TestCheapNoMatch(t *testing.T) {
	lab := label(t, cust("hello there"))
	if lab.Intent != "other" || lab.Confidence != 0.2 || lab.Action != "ask" {
		t.Errorf("got %+v", lab)
	}
	if l := label(t, threadOf(100, turnSpec{false, "thanks for the refund"}, turnSpec{true, ""})); l.Intent != "other" {
		t.Errorf("empty: %+v", l)
	}
	if l := label(t, threadOf(100, turnSpec{false, "your package has shipped"})); l.Intent != "other" {
		t.Errorf("brand only: %+v", l)
	}
}

func TestCheapTieBreak(t *testing.T) {
	cases := []struct {
		text, intent string
		c            float64
	}{
		{"my package and my refund", "billing", 0.4},
		{"login problem with my package", "account_access", 0.4},
		{"awful but thanks", "complaint", 0.4},
		{"package tracking refund", "delivery", 0.75},
	}
	for _, c := range cases {
		lab := label(t, cust(c.text))
		if lab.Intent != c.intent || lab.Confidence != c.c {
			t.Errorf("%q: got %+v", c.text, lab)
		}
	}
}

func TestCheapConfidenceByHits(t *testing.T) {
	cases := map[string]float64{
		"package tracking":                          0.75,
		"package tracking parcel":                   0.9,
		"package tracking parcel shipping delivery": 0.9,
	}
	for text, c := range cases {
		if got := label(t, cust(text)).Confidence; got != c {
			t.Errorf("%q: %v, want %v", text, got, c)
		}
	}
}

func TestCheapIgnoresOutbound(t *testing.T) {
	base := label(t, cust("where is my package"))
	noisy := threadOf(1, turnSpec{true, "where is my package SENTINEL_TEXT_1"},
		turnSpec{false, "refund invoice password lawyer urgent outage"},
		turnSpec{false, "thank you terrible"})
	if got := label(t, noisy); got != base {
		t.Errorf("got %+v, want %+v", got, base)
	}
}

func TestCheapDistinctHits(t *testing.T) {
	if c := label(t, cust("package package package")).Confidence; c != 0.6 {
		t.Errorf("repeated: %v", c)
	}
	two := threadOf(100, turnSpec{true, "package"}, turnSpec{false, "ok"}, turnSpec{true, "package again"})
	if c := label(t, two).Confidence; c != 0.6 {
		t.Errorf("two turns: %v", c)
	}
}

func TestCheapTokenMatching(t *testing.T) {
	cases := []struct{ text, intent string }{
		{"i am locked out", "account_access"},
		{"i locked my bike out front", "other"},
		{"the app is not working", "outage_technical"},
		{"not sure it is working", "other"},
		{"thank you", "praise"},
		{"thank the team for you", "other"},
		{"the download finished", "other"},
		{"going DOWN again", "outage_technical"},
		{"my 2fa code", "account_access"},
		{"unpackaged", "other"},
	}
	for _, c := range cases {
		if got := label(t, cust(c.text)).Intent; got != c.intent {
			t.Errorf("%q: %s, want %s", c.text, got, c.intent)
		}
	}
}

func TestCheapUrgency(t *testing.T) {
	cases := []struct{ text, urgency string }{
		{"this is fraud", "high"},
		{"need my package asap", "high"},
		{"URGENT please", "high"},
		{"i love it", "low"},
		{"love it, urgent", "high"},
		{"where is my package", "normal"},
		{"hello there", "normal"},
	}
	for _, c := range cases {
		if got := label(t, cust(c.text)).Urgency; got != c.urgency {
			t.Errorf("%q: %s, want %s", c.text, got, c.urgency)
		}
	}
}

func TestCheapActionRules(t *testing.T) {
	cases := []struct{ text, action, urgency string }{
		{"my account was hacked", "escalate", "high"},
		{"package stolen from porch", "escalate", "high"},
		{"i love it but call my lawyer", "escalate", "high"},
		{"cannot log in", "route", "normal"},
		{"app keeps showing an error", "route", "normal"},
		{"password reset urgent", "route", "high"},
		{"terrible service fix it immediately", "escalate", "high"},
		{"terrible service", "answer", "normal"},
		{"where is my package", "ask", "normal"},
		{"wrong bill", "ask", "normal"},
		{"hello there", "ask", "normal"},
		{"thanks so much", "answer", "low"},
	}
	for _, c := range cases {
		lab := label(t, cust(c.text))
		if lab.Action != c.action || lab.Urgency != c.urgency {
			t.Errorf("%q: got %+v", c.text, lab)
		}
	}
}

func TestCheapDeterministic(t *testing.T) {
	th := threadOf(1, turnSpec{true, "my package is late and the agent was rude SENTINEL_TEXT_1"},
		turnSpec{false, "sorry"}, turnSpec{true, "urgent please"})
	if label(t, th) != label(t, th) {
		t.Error("not deterministic")
	}
}

// TestUnicodeLowercase pins Python's str.lower() projected onto [a-z0-9'].
// Expected labels are Python's output for each text.
func TestUnicodeLowercase(t *testing.T) {
	cases := []struct {
		th   []turnSpec
		want Labels
	}{
		{[]turnSpec{{true, "SIGN İN"}}, Labels{"other", "normal", "ask", 0.2}},
		{[]turnSpec{{true, "LOGİN failed"}}, Labels{"other", "normal", "ask", 0.2}},
		{[]turnSpec{{true, "İ LOVE IT"}}, Labels{"praise", "low", "answer", 0.6}},
		{[]turnSpec{{true, "LOCKED OUT"}}, Labels{"account_access", "normal", "route", 0.6}},
		{[]turnSpec{{true, "locked\nout"}}, Labels{"account_access", "normal", "route", 0.6}},
		{[]turnSpec{{true, "locked"}, {true, "out"}}, Labels{"account_access", "normal", "route", 0.6}},
		{[]turnSpec{{true, "it’s down"}}, Labels{"outage_technical", "normal", "route", 0.6}},
		// U+212A KELVIN SIGN lowercases to k.
		{[]turnSpec{{true, "LOCKED OUT"}}, Labels{"account_access", "normal", "route", 0.6}},
	}
	for _, c := range cases {
		if got := label(t, threadOf(1, c.th...)); got != c.want {
			t.Errorf("%v: got %+v, want %+v", c.th, got, c.want)
		}
	}
}

func TestCheapModelID(t *testing.T) {
	if NewCheapTier().ModelID() != "keywords" {
		t.Error("model id")
	}
}
