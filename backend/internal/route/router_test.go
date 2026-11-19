package route

import (
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
)

// topics mirror the always-escalate topics of tests/fixtures/policy_rules.json.
var topics = []policy.Topic{
	{ID: "t-zorblax", Phrases: []string{"zorblax claim", "quuxfrob"}},
	{ID: "t-florp", Phrases: []string{"can't florp", "wibblewarp"}},
}

type stubTier struct {
	name, modelID string
	labels        Labels
	calls         int
	panics        bool
}

func (s *stubTier) Name() string    { return s.name }
func (s *stubTier) ModelID() string { return s.modelID }
func (s *stubTier) Price() float64  { return 0 }
func (s *stubTier) Label(ingest.Thread) (Labels, error) {
	if s.panics {
		panic("the larger tier must not run on a forced thread")
	}
	s.calls++
	return s.labels, nil
}

type stubAgent struct {
	name, modelID string
	result        *Labels
	calls         int
	seen          Labels
}

func (s *stubAgent) Name() string    { return s.name }
func (s *stubAgent) ModelID() string { return s.modelID }
func (s *stubAgent) Price() float64  { return 0 }
func (s *stubAgent) Act(_ ingest.Thread, l Labels) (Labels, error) {
	s.calls++
	s.seen = l
	if s.result != nil {
		return *s.result, nil
	}
	return l, nil
}

func stubCheap(l Labels) *stubTier  { return &stubTier{name: "cheap", modelID: "stub", labels: l} }
func stubLarger(l Labels) *stubTier { return &stubTier{name: "larger", modelID: "stub", labels: l} }
func raisingLarger() *stubTier      { return &stubTier{name: "larger", modelID: "stub", panics: true} }
func newStubAgent(r *Labels) *stubAgent {
	return &stubAgent{name: "agent", modelID: "stub", result: r}
}

// lab is L() from tests/test_router.py with its defaults.
func lab(opts ...func(*Labels)) Labels {
	l := Labels{"delivery", "normal", "route", 0.9}
	for _, o := range opts {
		o(&l)
	}
	return l
}
func conf(c float64) func(*Labels)  { return func(l *Labels) { l.Confidence = c } }
func urg(u string) func(*Labels)    { return func(l *Labels) { l.Urgency = u } }
func intent(i string) func(*Labels) { return func(l *Labels) { l.Intent = i } }
func act(a string) func(*Labels)    { return func(l *Labels) { l.Action = a } }

func mustRoute(t *testing.T, th ingest.Thread, c, l LabellingTier, a AgentTier, threshold float64, tp []policy.Topic, rep *RepeatInput) RoutedThread {
	t.Helper()
	r, err := RouteThread(th, c, l, a, threshold, tp, rep)
	if err != nil {
		t.Fatalf("RouteThread: %v", err)
	}
	return r
}

func fakes() (LabellingTier, LabellingTier, *FakeAgentTier) {
	return NewCheapTier(), NewFakeLargerTier(nil), NewFakeAgentTier()
}

func TestIsHighRisk(t *testing.T) {
	if IsHighRisk(lab()) {
		t.Error("plain")
	}
	for _, l := range []Labels{lab(urg("high")), lab(intent("account_access")), lab(intent("outage_technical")), lab(act("escalate"))} {
		if !IsHighRisk(l) {
			t.Errorf("%+v not high risk", l)
		}
	}
	if IsHighRisk(Labels{"complaint", "low", "answer", 0.9}) {
		t.Error("complaint low answer")
	}
	if DefaultConfidenceThreshold != 0.5 {
		t.Error("threshold")
	}
}

type runOut struct {
	r             RoutedThread
	cheap, larger *stubTier
	agent         *stubAgent
}

func runRoute(t *testing.T, cheapL Labels, largerL *Labels, threshold float64, agentResult *Labels) runOut {
	t.Helper()
	l := cheapL
	if largerL != nil {
		l = *largerL
	}
	o := runOut{cheap: stubCheap(cheapL), larger: stubLarger(l), agent: newStubAgent(agentResult)}
	o.r = mustRoute(t, thread(5, "SENTINEL_TEXT"), o.cheap, o.larger, o.agent, threshold, noTopics, nil)
	return o
}

func tiersOf(r RoutedThread) string { return strings.Join(r.Tiers, ",") }

func TestRoutePlain(t *testing.T) {
	o := runRoute(t, lab(conf(0.9)), nil, 0.5, nil)
	if tiersOf(o.r) != "cheap" || o.r.Labels != lab(conf(0.9)) || o.r.CheapLabels != lab(conf(0.9)) {
		t.Errorf("got %+v", o.r)
	}
	if o.cheap.calls != 1 || o.larger.calls != 0 || o.agent.calls != 0 {
		t.Errorf("calls %d %d %d", o.cheap.calls, o.larger.calls, o.agent.calls)
	}
}

func TestRouteActionable(t *testing.T) {
	c, l, a := fakes()
	r := mustRoute(t, thread(6, "thanks, love it SENTINEL_TEXT_6"), c, l, a, 0.5, noTopics, nil)
	if r.CheapLabels.Action != "answer" || r.CheapLabels.Confidence != 0.75 || tiersOf(r) != "cheap,agent" || a.Calls != 1 || r.Labels != r.CheapLabels {
		t.Errorf("got %+v calls %d", r, a.Calls)
	}
}

func TestRouteAgentResultIsFinal(t *testing.T) {
	final := lab(act("escalate"))
	o := runRoute(t, lab(act("ask")), nil, 0.5, &final)
	if tiersOf(o.r) != "cheap,agent" || o.r.Labels != final {
		t.Errorf("got %+v", o.r)
	}
	if !reflect.DeepEqual(o.r.Steps, []TierStep{{"cheap", "stub", lab(act("ask"))}, {"agent", "stub", final}}) {
		t.Errorf("steps %+v", o.r.Steps)
	}
}

func TestRouteLowConfidence(t *testing.T) {
	larger := lab(intent("billing"), conf(1.0))
	o := runRoute(t, lab(conf(0.3)), &larger, 0.5, nil)
	if tiersOf(o.r) != "cheap,larger" || o.r.Labels != larger || o.r.CheapLabels != lab(conf(0.3)) || o.larger.calls != 1 || o.agent.calls != 0 {
		t.Errorf("got %+v", o.r)
	}
	if o := runRoute(t, lab(act("ask"), conf(0.2)), nil, 0.5, nil); tiersOf(o.r) != "cheap,larger,agent" {
		t.Errorf("then actionable: %v", o.r.Tiers)
	}
}

func TestRouteEachHighRiskCondition(t *testing.T) {
	for _, l := range []Labels{lab(urg("high")), lab(intent("account_access")), lab(intent("outage_technical")), lab(act("escalate"))} {
		o := runRoute(t, l, nil, 0.5, nil)
		if tiersOf(o.r) != "cheap,larger" || o.larger.calls != 1 || o.r.ReasonCodes[0] != HighRiskToLarger {
			t.Errorf("%+v: %+v", l, o.r)
		}
	}
}

func TestRouteOverrideToEscalateSkipsAgent(t *testing.T) {
	esc := Labels{"complaint", "high", "escalate", 1.0}
	agent := NewFakeAgentTier()
	r := mustRoute(t, thread(9, "hello there SENTINEL_TEXT_9"), NewCheapTier(), NewFakeLargerTier(map[uint64]Labels{9: esc}), agent, 0.5, noTopics, nil)
	if r.CheapLabels.Action != "ask" || tiersOf(r) != "cheap,larger" || r.Labels != esc || agent.Calls != 0 {
		t.Errorf("got %+v", r)
	}
}

func TestRouteThresholdBoundary(t *testing.T) {
	cases := []struct {
		c, threshold float64
		tiers        string
	}{
		{0.5, 0.5, "cheap"},
		{0.49, 0.5, "cheap,larger"},
		{0.7, 0.7, "cheap"},
		{0.7, 0.71, "cheap,larger"},
	}
	for _, c := range cases {
		if o := runRoute(t, lab(conf(c.c)), nil, c.threshold, nil); tiersOf(o.r) != c.tiers {
			t.Errorf("%v/%v: %v", c.c, c.threshold, o.r.Tiers)
		}
	}
}

func TestRouteTierNamesComeFromTiers(t *testing.T) {
	c := stubCheap(lab(act("ask"), conf(0.1)))
	c.name = "c2"
	l := stubLarger(lab(act("ask")))
	l.name = "l2"
	a := newStubAgent(nil)
	a.name = "a2"
	if r := mustRoute(t, thread(1, "x"), c, l, a, 0.5, noTopics, nil); tiersOf(r) != "c2,l2,a2" {
		t.Errorf("tiers %v", r.Tiers)
	}
}

func TestRouteThreadsOrderAndIDs(t *testing.T) {
	c, l, a := fakes()
	var ths []ingest.Thread
	for _, id := range []uint64{30, 10, 20} {
		ths = append(ths, thread(id, "package SENTINEL_TEXT"))
	}
	out, err := RouteThreads(ths, c, l, a, 0.5, noTopics, nil)
	if err != nil || len(out) != 3 || out[0].ThreadID != 30 || out[1].ThreadID != 10 || out[2].ThreadID != 20 {
		t.Errorf("got %+v, %v", out, err)
	}
	out, err = RouteThreads(nil, c, l, a, 0.5, noTopics, nil)
	if err != nil || len(out) != 0 {
		t.Errorf("empty: %v %v", out, err)
	}
}

func TestRouteBadThreshold(t *testing.T) {
	c, l, a := fakes()
	cases := map[float64]string{
		-0.01:        "threshold must be a number in [0, 1], got -0.01",
		1.01:         "threshold must be a number in [0, 1], got 1.01",
		math.NaN():   "threshold must be a number in [0, 1], got nan",
		math.Inf(1):  "threshold must be a number in [0, 1], got inf",
		math.Inf(-1): "threshold must be a number in [0, 1], got -inf",
	}
	for bad, msg := range cases {
		if _, err := RouteThread(thread(1, "x"), c, l, a, bad, noTopics, nil); err == nil || err.Error() != msg {
			t.Errorf("RouteThread(%v) = %v", bad, err)
		}
		if _, err := RouteThreads([]ingest.Thread{thread(1, "x")}, c, l, a, bad, noTopics, nil); err == nil || err.Error() != msg {
			t.Errorf("RouteThreads(%v) = %v", bad, err)
		}
	}
	mustRoute(t, thread(1, "x"), c, l, a, 0, noTopics, nil)
	mustRoute(t, thread(1, "x"), c, l, a, 1, noTopics, nil)
}

func route1(t *testing.T, text string, threshold float64, larger LabellingTier, rootID uint64) RoutedThread {
	t.Helper()
	if larger == nil {
		larger = NewFakeLargerTier(nil)
	}
	return mustRoute(t, thread(rootID, text), NewCheapTier(), larger, NewFakeAgentTier(), threshold, noTopics, nil)
}

func TestReasonCodeRegistry(t *testing.T) {
	if !reflect.DeepEqual(CascadeCodes, []string{"cheap_confident", "low_confidence_to_larger", "high_risk_to_larger"}) {
		t.Errorf("CascadeCodes %v", CascadeCodes)
	}
	if !reflect.DeepEqual(ReasonCodes, []string{"cheap_confident", "low_confidence_to_larger", "high_risk_to_larger", "escalate_always", "repeat_contact"}) {
		t.Errorf("ReasonCodes %v", ReasonCodes)
	}
	if EscalateAlways != "escalate_always" || RepeatContact != "repeat_contact" {
		t.Error("constants")
	}
	for _, c := range ReasonCodes {
		if !IsKnownReasonCode(c, nil) {
			t.Errorf("%s unknown", c)
		}
	}
	for _, bad := range []any{"no_such_code", nil, 3, []byte("cheap_confident"), "", "cheap_confident:x"} {
		if IsKnownReasonCode(bad, nil) {
			t.Errorf("%v known", bad)
		}
	}
	if !IsKnownReasonCode("x", []string{"x"}) || IsKnownReasonCode("cheap_confident", []string{"x"}) || IsKnownReasonCode("cheap_confident", []string{}) {
		t.Error("custom codes")
	}
}

func assertCodes(t *testing.T, r RoutedThread, first string) {
	t.Helper()
	if r.ReasonCodes[0] != first {
		t.Errorf("first code %v, want %s", r.ReasonCodes, first)
	}
	n := 0
	for _, c := range r.ReasonCodes {
		if slicesContains(CascadeCodes, c) {
			n++
		}
		if !IsKnownReasonCode(c, nil) {
			t.Errorf("unknown %s", c)
		}
	}
	if n != 1 {
		t.Errorf("cascade codes %v", r.ReasonCodes)
	}
}

func slicesContains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func TestReasonCodesCascade(t *testing.T) {
	r := route1(t, "package delivery SENTINEL_TEXT", 0.5, nil, 11)
	if r.CheapLabels.Confidence != 0.75 || slicesContains(r.Tiers, "larger") {
		t.Errorf("%+v", r)
	}
	assertCodes(t, r, CheapConfident)
	r = route1(t, "hello SENTINEL_TEXT", 0.5, nil, 11)
	if IsHighRisk(r.CheapLabels) {
		t.Error("hello high risk")
	}
	assertCodes(t, r, LowConfidenceToLarger)
	r = route1(t, "password login locked out SENTINEL_TEXT", 0.5, nil, 11)
	if r.CheapLabels.Confidence != 0.9 {
		t.Errorf("%+v", r.CheapLabels)
	}
	assertCodes(t, r, HighRiskToLarger)
	r = route1(t, "urgent hello SENTINEL_TEXT", 0.5, nil, 11)
	if !(r.CheapLabels.Confidence < 0.5) || !IsHighRisk(r.CheapLabels) {
		t.Errorf("%+v", r.CheapLabels)
	}
	assertCodes(t, r, HighRiskToLarger)
	assertCodes(t, route1(t, "package delivery", 0.8, nil, 11), LowConfidenceToLarger)
	assertCodes(t, route1(t, "package delivery", 0.75, nil, 11), CheapConfident)
}

func TestStepsRecordEachTier(t *testing.T) {
	override := Labels{"billing", "normal", "ask", 1.0}
	r := route1(t, "hello SENTINEL_TEXT", 0.5, NewFakeLargerTier(map[uint64]Labels{11: override}), 11)
	want := []TierStep{{"cheap", "keywords", Labels{"other", "normal", "ask", 0.2}}, {"larger", "fake-larger", override}, {"agent", "fake-agent", override}}
	if tiersOf(r) != "cheap,larger,agent" || !reflect.DeepEqual(r.Steps, want) {
		t.Errorf("got %+v", r)
	}
}

func TestMissingModelIDRefused(t *testing.T) {
	okC, okL, okA := stubCheap(lab()), stubLarger(lab()), newStubAgent(nil)
	emptyL := stubLarger(lab())
	emptyL.name, emptyL.modelID = "emptylarger", ""
	emptyC := stubCheap(lab())
	emptyC.name, emptyC.modelID = "nonecheap", ""
	emptyA := newStubAgent(nil)
	emptyA.name, emptyA.modelID = "noagent", ""
	cases := []struct {
		c, l LabellingTier
		a    AgentTier
		name string
	}{
		{emptyC, okL, okA, "nonecheap"},
		{okC, emptyL, okA, "emptylarger"},
		{okC, okL, emptyA, "noagent"},
	}
	for _, c := range cases {
		msg := "tier '" + c.name + "' has no model_id: every tier must set a non-empty string"
		if _, err := RouteThread(thread(1, "x"), c.c, c.l, c.a, 0.5, noTopics, nil); err == nil || err.Error() != msg {
			t.Errorf("RouteThread: %v", err)
		}
		for _, ths := range [][]ingest.Thread{{thread(1, "x")}, nil} {
			if _, err := RouteThreads(ths, c.c, c.l, c.a, 0.5, noTopics, nil); err == nil || err.Error() != msg {
				t.Errorf("RouteThreads: %v", err)
			}
		}
	}
	if okC.calls != 0 {
		t.Errorf("cheap called %d", okC.calls)
	}
	// Threshold is checked before model ids.
	if _, err := RouteThread(thread(1, "x"), emptyC, okL, okA, 2, noTopics, nil); err == nil || !strings.HasPrefix(err.Error(), "threshold") {
		t.Errorf("order: %v", err)
	}
}

func TestRoutedFieldsUnchanged(t *testing.T) {
	cases := []struct {
		text, tiers   string
		cheap, labels Labels
	}{
		{"package delivery", "cheap,agent", Labels{"delivery", "normal", "ask", 0.75}, Labels{"delivery", "normal", "ask", 0.75}},
		{"hello", "cheap,larger,agent", Labels{"other", "normal", "ask", 0.2}, Labels{"other", "normal", "ask", 1.0}},
		{"password login locked out", "cheap,larger", Labels{"account_access", "normal", "route", 0.9}, Labels{"account_access", "normal", "route", 1.0}},
		{"thanks, love it", "cheap,agent", Labels{"praise", "low", "answer", 0.75}, Labels{"praise", "low", "answer", 0.75}},
	}
	for _, c := range cases {
		r := route1(t, c.text+" SENTINEL_TEXT", 0.5, nil, 42)
		if r.ThreadID != 42 || tiersOf(r) != c.tiers || r.CheapLabels != c.cheap || r.Labels != c.labels {
			t.Errorf("%q: %+v", c.text, r)
		}
		if r.TopicID == nil || len(r.TopicID) != 0 {
			t.Errorf("%q: TopicID %#v", c.text, r.TopicID)
		}
	}
}

func brandThread(rootID uint64, customer, brand string) ingest.Thread {
	return ingest.Thread{RootID: rootID, BrandIDs: []string{"brand"}, BrandReplied: true, Turns: []ingest.Turn{
		{ID: rootID, AuthorID: "cust", Inbound: true, CreatedAt: t0, Text: customer},
		{ID: rootID + 1, AuthorID: "brand", Inbound: false, CreatedAt: t0, Text: brand},
	}}
}

func TestEscalateAlwaysForced(t *testing.T) {
	cheap := Labels{"praise", "low", "answer", 0.9}
	r := mustRoute(t, thread(3, "hi, quuxfrob"), stubCheap(cheap), raisingLarger(), newStubAgent(nil), 0.5, topics, nil)
	if r.Labels != (Labels{"praise", "low", "escalate", 0.9}) || r.CheapLabels != cheap {
		t.Errorf("got %+v", r)
	}
	if !reflect.DeepEqual(r.ReasonCodes, []string{EscalateAlways}) || !reflect.DeepEqual(r.TopicID, []string{"t-zorblax"}) {
		t.Errorf("codes %v topics %v", r.ReasonCodes, r.TopicID)
	}
	both := mustRoute(t, thread(4, "can't florp, quuxfrob"), stubCheap(cheap), raisingLarger(), newStubAgent(nil), 0.5, topics, nil)
	if !reflect.DeepEqual(both.TopicID, []string{"t-zorblax", "t-florp"}) || !reflect.DeepEqual(both.ReasonCodes, []string{EscalateAlways}) {
		t.Errorf("both: %+v", both)
	}
}

func TestEscalateAlwaysLargerSkippedAgentDrafts(t *testing.T) {
	agent := newStubAgent(nil)
	r := mustRoute(t, thread(3, "a zorblax claim"), stubCheap(Labels{"billing", "high", "route", 0.3}), raisingLarger(), agent, 0.5, topics, nil)
	want := Labels{"billing", "high", "escalate", 0.3}
	if tiersOf(r) != "cheap,agent" || len(r.Steps) != 2 || agent.calls != 1 || agent.seen != want || r.Labels != want {
		t.Errorf("got %+v", r)
	}
}

func TestEscalateAlwaysAgentCannotUnforce(t *testing.T) {
	draft := Labels{"delivery", "normal", "answer", 0.99}
	r := mustRoute(t, thread(3, "quuxfrob"), stubCheap(Labels{"praise", "low", "answer", 0.9}), raisingLarger(), newStubAgent(&draft), 0.5, topics, nil)
	if r.Labels != (Labels{"praise", "low", "escalate", 0.9}) || r.Steps[1] != (TierStep{"agent", "stub", draft}) {
		t.Errorf("got %+v", r)
	}
}

func TestEscalateAlwaysBrandTurnNeverForces(t *testing.T) {
	r := mustRoute(t, brandThread(7, "thanks", "quuxfrob"), stubCheap(Labels{"praise", "low", "answer", 0.9}), stubLarger(lab()), newStubAgent(nil), 0.5, topics, nil)
	if r.Labels.Action != "answer" || len(r.TopicID) != 0 {
		t.Errorf("got %+v", r)
	}
}

func TestEscalateAlwaysNoMatchUnchanged(t *testing.T) {
	for _, tp := range [][]policy.Topic{noTopics, topics} {
		c, l, a := fakes()
		r := mustRoute(t, thread(5, "thanks, love it"), c, l, a, 0.5, tp, nil)
		if !reflect.DeepEqual(r.ReasonCodes, []string{CheapConfident}) || len(r.TopicID) != 0 || r.Labels != (Labels{"praise", "low", "answer", 0.75}) {
			t.Errorf("got %+v", r)
		}
	}
}

func TestRouteThreadsForcesToo(t *testing.T) {
	c, l, a := fakes()
	out, err := RouteThreads([]ingest.Thread{thread(1, "hello"), thread(2, "quuxfrob")}, c, l, a, 0.5, topics, nil)
	if err != nil || len(out[0].TopicID) != 0 || !reflect.DeepEqual(out[1].TopicID, []string{"t-zorblax"}) || out[1].Labels.Action != "escalate" {
		t.Errorf("got %+v %v", out, err)
	}
}

// shopco mirrors shopco_threads: three conversations of one made-up customer.
func shopco() []ingest.Thread {
	var out []ingest.Thread
	for _, id := range []uint64{101, 102, 103} {
		out = append(out, thread(id, "@ShopCo where is my parcel"))
	}
	return out
}

// shopcoCounts are contact_counts over shopco with a 7-day window (days 0, 2, 5).
var shopcoCounts = map[uint64]int{101: 1, 102: 2, 103: 3}

func TestRepeatThirdEscalates(t *testing.T) {
	c, l, a := fakes()
	out, err := RouteThreads(shopco(), c, l, a, 0.5, noTopics, &RepeatInput{MinConversations: 3, Counts: shopcoCounts})
	if err != nil {
		t.Fatal(err)
	}
	third := out[2]
	if third.Labels.Action != "escalate" || third.Labels.Urgency != "high" || !reflect.DeepEqual(third.ReasonCodes, []string{CheapConfident, RepeatContact}) || tiersOf(third) != "cheap" {
		t.Errorf("third %+v", third)
	}
	for _, r := range out[:2] {
		if slicesContains(r.ReasonCodes, RepeatContact) || r.Labels.Action != "ask" {
			t.Errorf("%+v", r)
		}
	}
	c, l, a = fakes()
	four, _ := RouteThreads(shopco(), c, l, a, 0.5, noTopics, &RepeatInput{MinConversations: 4, Counts: shopcoCounts})
	for _, r := range four {
		if r.Labels.Action != "ask" {
			t.Errorf("min 4: %+v", r)
		}
	}
}

func TestRepeatAgentNotCalledWhenForced(t *testing.T) {
	c, l, a := fakes()
	rep := &RepeatInput{MinConversations: 3, Counts: shopcoCounts}
	if _, err := RouteThreads(shopco()[:2], c, l, a, 0.5, noTopics, rep); err != nil || a.Calls != 2 {
		t.Fatalf("calls %d %v", a.Calls, err)
	}
	mustRoute(t, shopco()[2], c, l, a, 0.5, noTopics, rep)
	if a.Calls != 2 {
		t.Errorf("calls %d", a.Calls)
	}
}

func TestRepeatRaisesUrgency(t *testing.T) {
	agent := newStubAgent(nil)
	r := mustRoute(t, thread(5, "x"), stubCheap(Labels{"billing", "normal", "route", 0.3}), stubLarger(Labels{"billing", "normal", "escalate", 0.9}), agent, 0.5, noTopics, &RepeatInput{3, map[uint64]int{5: 3}})
	if r.Labels != (Labels{"billing", "high", "escalate", 0.9}) || !reflect.DeepEqual(r.ReasonCodes, []string{LowConfidenceToLarger, RepeatContact}) || tiersOf(r) != "cheap,larger" || r.CheapLabels != (Labels{"billing", "normal", "route", 0.3}) || agent.calls != 0 {
		t.Errorf("got %+v", r)
	}
}

func TestRepeatNoCodeWhenUnchanged(t *testing.T) {
	r := mustRoute(t, thread(5, "x"), stubCheap(Labels{"billing", "normal", "route", 0.3}), stubLarger(Labels{"billing", "high", "escalate", 0.9}), newStubAgent(nil), 0.5, noTopics, &RepeatInput{3, map[uint64]int{5: 4}})
	if r.Labels != (Labels{"billing", "high", "escalate", 0.9}) || !reflect.DeepEqual(r.ReasonCodes, []string{LowConfidenceToLarger}) {
		t.Errorf("got %+v", r)
	}
	c, l, a := fakes()
	below := mustRoute(t, thread(6, "my parcel never arrived"), c, l, a, 0.5, noTopics, &RepeatInput{3, map[uint64]int{6: 2}})
	if !reflect.DeepEqual(below.ReasonCodes, []string{CheapConfident}) || below.Labels.Action != "ask" {
		t.Errorf("below %+v", below)
	}
}

func TestRepeatWithEscalateAlwaysTopic(t *testing.T) {
	agent := newStubAgent(nil)
	r := mustRoute(t, thread(3, "hi, quuxfrob"), stubCheap(Labels{"praise", "low", "answer", 0.9}), raisingLarger(), agent, 0.5, topics, &RepeatInput{3, map[uint64]int{3: 3}})
	if r.Labels != (Labels{"praise", "high", "escalate", 0.9}) || !reflect.DeepEqual(r.ReasonCodes, []string{EscalateAlways, RepeatContact}) || !reflect.DeepEqual(r.TopicID, []string{"t-zorblax"}) || tiersOf(r) != "cheap,agent" || agent.calls != 1 {
		t.Errorf("got %+v", r)
	}
}

func TestRepeatRuleAndCountsTogether(t *testing.T) {
	c, l, a := fakes()
	msg := "repeat_rule and repeat_counts must be given together"
	rep := &RepeatInput{MinConversations: 3}
	if _, err := RouteThread(thread(1, "x"), c, l, a, 0.5, noTopics, rep); err == nil || err.Error() != msg {
		t.Errorf("RouteThread: %v", err)
	}
	if _, err := RouteThreads([]ingest.Thread{thread(1, "x")}, c, l, a, 0.5, noTopics, rep); err == nil || err.Error() != msg {
		t.Errorf("RouteThreads: %v", err)
	}
}

func TestRepeatNoRuleUnchanged(t *testing.T) {
	c, l, a := fakes()
	plain, _ := RouteThreads(shopco(), c, l, a, 0.5, noTopics, nil)
	for _, r := range plain {
		if r.Labels.Action != "ask" || len(r.ReasonCodes) != 1 {
			t.Errorf("%+v", r)
		}
	}
}

type errTier struct{ stubTier }

func (*errTier) Label(ingest.Thread) (Labels, error) { return Labels{}, errors.New("boom") }

func TestTierErrorIsWrapped(t *testing.T) {
	_, l, a := fakes()
	_, err := RouteThread(thread(77, "x"), &errTier{stubTier{name: "cheap", modelID: "e"}}, l, a, 0.5, noTopics, nil)
	if err == nil || !strings.Contains(err.Error(), "77") || !strings.Contains(err.Error(), "boom") {
		t.Errorf("err %v", err)
	}
}

// noTopics turns the always-escalate rule off; nil is refused (review round 1).
var noTopics = []policy.Topic{}

func TestNilTopicsRefused(t *testing.T) {
	c, l, a := fakes()
	msg := errNilTopics
	if _, err := RouteThread(thread(1, "x"), c, l, a, 0.5, nil, nil); err == nil || err.Error() != msg {
		t.Errorf("RouteThread(nil topics) = %v, want %q", err, msg)
	}
	for _, ths := range [][]ingest.Thread{{thread(1, "x")}, nil} {
		if _, err := RouteThreads(ths, c, l, a, 0.5, nil, nil); err == nil || err.Error() != msg {
			t.Errorf("RouteThreads(nil topics) = %v, want %q", err, msg)
		}
	}
	// Python checks threshold, model ids and repeat before tuple(topics).
	if _, err := RouteThread(thread(1, "x"), c, l, a, 2, nil, nil); err == nil || !strings.HasPrefix(err.Error(), "threshold") {
		t.Errorf("threshold before topics: %v", err)
	}
	empty := stubCheap(Labels{"billing", "normal", "route", 0.9})
	empty.modelID = ""
	if _, err := RouteThread(thread(1, "x"), empty, l, a, 0.5, nil, nil); err == nil || !strings.HasPrefix(err.Error(), "tier ") {
		t.Errorf("model ids before topics: %v", err)
	}
	if _, err := RouteThreads(nil, c, l, a, 0.5, nil, &RepeatInput{MinConversations: 3}); err == nil || !strings.HasPrefix(err.Error(), "repeat_rule") {
		t.Errorf("repeat before topics: %v", err)
	}
	sc := stubCheap(Labels{"billing", "normal", "route", 0.9})
	_, _ = RouteThread(thread(1, "x"), sc, l, a, 0.5, nil, nil)
	_, _ = RouteThreads([]ingest.Thread{thread(1, "x")}, sc, l, a, 0.5, nil, nil)
	if sc.calls != 0 || a.Calls != 0 {
		t.Errorf("tiers called on refusal: cheap %d, agent %d", sc.calls, a.Calls)
	}
	// An empty non-nil slice turns the rule off.
	if _, err := RouteThread(thread(1, "x"), c, l, a, 0.5, noTopics, nil); err != nil {
		t.Errorf("empty topics: %v", err)
	}
}
