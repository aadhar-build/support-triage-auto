package route

import (
	"math"
	"reflect"
	"testing"
)

func TestVocabularies(t *testing.T) {
	cases := []struct {
		got, want []string
	}{
		{Intents, []string{"delivery", "billing", "account_access", "outage_technical", "complaint", "praise", "other"}},
		{Urgencies, []string{"low", "normal", "high"}},
		{Actions, []string{"answer", "ask", "route", "escalate"}},
		{ActionableActions, []string{"answer", "ask"}},
	}
	for _, c := range cases {
		if !reflect.DeepEqual(c.got, c.want) {
			t.Errorf("got %v, want %v", c.got, c.want)
		}
	}
	if !reflect.DeepEqual(PlaceholderPrices, map[string]float64{"cheap": 0, "larger": 0.002, "agent": 0.01}) {
		t.Errorf("PlaceholderPrices = %v", PlaceholderPrices)
	}
}

func TestLabelsValid(t *testing.T) {
	lab := mustLabels(t, "delivery", "normal", "ask", 0.6)
	if lab != (Labels{"delivery", "normal", "ask", 0.6}) {
		t.Errorf("got %+v", lab)
	}
	mustLabels(t, "other", "low", "answer", 0.0)
	mustLabels(t, "praise", "high", "escalate", 1.0)
}

func TestLabelsRejectsBadValues(t *testing.T) {
	cases := []struct {
		intent, urgency, action string
		c                       float64
		msg                     string
	}{
		{"weather", "normal", "ask", 0.5, "unknown intent 'weather'"},
		{"delivery", "critical", "ask", 0.5, "unknown urgency 'critical'"},
		{"delivery", "normal", "ignore", 0.5, "unknown action 'ignore'"},
		{"delivery", "normal", "ask", -0.01, "confidence must be a number in [0, 1], got -0.01"},
		{"delivery", "normal", "ask", 1.01, "confidence must be a number in [0, 1], got 1.01"},
		{"delivery", "normal", "ask", 1.5, "confidence must be a number in [0, 1], got 1.5"},
		{"delivery", "normal", "ask", math.NaN(), "confidence must be a number in [0, 1], got nan"},
		{"delivery", "normal", "ask", math.Inf(1), "confidence must be a number in [0, 1], got inf"},
		{"weather", "critical", "ask", 2, "unknown intent 'weather'"},
		{"it's", "normal", "ask", 0.5, `unknown intent "it's"`},
	}
	for _, c := range cases {
		_, err := NewLabels(c.intent, c.urgency, c.action, c.c)
		if err == nil || err.Error() != c.msg {
			t.Errorf("NewLabels(%q,%q,%q,%v) err = %v, want %q", c.intent, c.urgency, c.action, c.c, err, c.msg)
		}
	}
}

func TestFakeLargerOverride(t *testing.T) {
	override := mustLabels(t, "billing", "high", "escalate", 0.95)
	larger := NewFakeLargerTier(map[uint64]Labels{7: override})
	got, err := larger.Label(thread(7, "my package SENTINEL_TEXT_7"))
	if err != nil || got != override {
		t.Errorf("got %+v, %v", got, err)
	}
}

func TestFakeLargerDefaultIsCheapWithFullConfidence(t *testing.T) {
	larger := NewFakeLargerTier(map[uint64]Labels{7: mustLabels(t, "billing", "high", "escalate", 0.95)})
	th := thread(8, "my package SENTINEL_TEXT_8")
	got, _ := larger.Label(th)
	want := Labels{"delivery", "normal", "ask", 1.0}
	if got != want {
		t.Errorf("got %+v, want %+v", got, want)
	}
	plain, _ := NewFakeLargerTier(nil).Label(th)
	if plain != got {
		t.Errorf("no-override larger = %+v, want %+v", plain, got)
	}
}

func TestFakeAgentPassesThroughAndCounts(t *testing.T) {
	agent := NewFakeAgentTier()
	if agent.Calls != 0 {
		t.Fatalf("Calls = %d", agent.Calls)
	}
	lab := mustLabels(t, "praise", "low", "answer", 0.75)
	for i, th := range []string{"x", "y"} {
		got, err := agent.Act(thread(uint64(i+1), th), lab)
		if err != nil || got != lab {
			t.Errorf("Act = %+v, %v", got, err)
		}
	}
	if agent.Calls != 2 {
		t.Errorf("Calls = %d, want 2", agent.Calls)
	}
}

func TestNamesModelIDsAndPrices(t *testing.T) {
	var cheap LabellingTier = NewCheapTier()
	var larger LabellingTier = NewFakeLargerTier(nil)
	var agent AgentTier = NewFakeAgentTier()
	got := [][3]any{
		{cheap.Name(), cheap.ModelID(), cheap.Price()},
		{larger.Name(), larger.ModelID(), larger.Price()},
		{agent.Name(), agent.ModelID(), agent.Price()},
	}
	want := [][3]any{
		{"cheap", "keywords", 0.0},
		{"larger", "fake-larger", 0.002},
		{"agent", "fake-agent", 0.01},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
	if p := NewCheapTier(0.5).Price(); p != 0.5 {
		t.Errorf("custom cheap price %v", p)
	}
	if p := NewFakeLargerTierPrice(nil, 0.3).Price(); p != 0.3 {
		t.Errorf("custom larger price %v", p)
	}
	if p := NewFakeAgentTier(0.7).Price(); p != 0.7 {
		t.Errorf("custom agent price %v", p)
	}
}

func TestCheckPrice(t *testing.T) {
	for _, ok := range []float64{0, 0.002, 5} {
		if err := CheckPrice("cheap", ok); err != nil {
			t.Errorf("CheckPrice(%v) = %v", ok, err)
		}
	}
	cases := []struct {
		price float64
		msg   string
	}{
		{-0.01, "price for 'larger' must be a finite number >= 0, got -0.01"},
		{math.NaN(), "price for 'larger' must be a finite number >= 0, got nan"},
		{math.Inf(1), "price for 'larger' must be a finite number >= 0, got inf"},
		{math.Inf(-1), "price for 'larger' must be a finite number >= 0, got -inf"},
	}
	for _, c := range cases {
		if err := CheckPrice("larger", c.price); err == nil || err.Error() != c.msg {
			t.Errorf("CheckPrice(%v) = %v, want %q", c.price, err, c.msg)
		}
	}
}
