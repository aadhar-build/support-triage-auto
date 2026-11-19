// Package route is the Go port of triage.tiers, triage.cheap_tier,
// triage.router and triage.trace: label vocabularies, the tiers, the cascade
// router and the audit trace writer.
package route

import (
	"errors"
	"math"
	"slices"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
)

// Vocabularies, in the order of the Python tuples.
var (
	Intents           = []string{"delivery", "billing", "account_access", "outage_technical", "complaint", "praise", "other"}
	Urgencies         = []string{"low", "normal", "high"}
	Actions           = []string{"answer", "ask", "route", "escalate"}
	ActionableActions = []string{"answer", "ask"}
)

// PlaceholderPrices are made-up per-call figures, not real prices.
var PlaceholderPrices = map[string]float64{"cheap": 0, "larger": 0.002, "agent": 0.01}

// Labels is triage.tiers.Labels. Compare with ==.
type Labels struct {
	Intent, Urgency, Action string
	Confidence              float64
}

// NewLabels validates like Labels.__post_init__, in the same order and with
// the same messages.
func NewLabels(intent, urgency, action string, confidence float64) (Labels, error) {
	switch {
	case !slices.Contains(Intents, intent):
		return Labels{}, errors.New("unknown intent " + reprStr(intent))
	case !slices.Contains(Urgencies, urgency):
		return Labels{}, errors.New("unknown urgency " + reprStr(urgency))
	case !slices.Contains(Actions, action):
		return Labels{}, errors.New("unknown action " + reprStr(action))
	case math.IsNaN(confidence) || confidence < 0 || confidence > 1:
		return Labels{}, errors.New("confidence must be a number in [0, 1], got " + reprFloat(confidence))
	}
	return Labels{intent, urgency, action, confidence}, nil
}

// LabellingTier labels one thread.
type LabellingTier interface {
	Name() string
	ModelID() string
	Price() float64
	Label(ingest.Thread) (Labels, error)
}

// AgentTier acts on an actionable thread and returns the final labels.
type AgentTier interface {
	Name() string
	ModelID() string
	Price() float64
	Act(ingest.Thread, Labels) (Labels, error)
}

func priceOr(def float64, price []float64) float64 {
	if len(price) > 0 {
		return price[0]
	}
	return def
}

// FakeLargerTier stands in for a larger model: an override by root id, else
// the cheap tier's labels with confidence 1.0.
type FakeLargerTier struct {
	Overrides map[uint64]Labels
	price     float64
	base      *CheapTier
}

// NewFakeLargerTier uses the placeholder price.
func NewFakeLargerTier(overrides map[uint64]Labels) *FakeLargerTier {
	return NewFakeLargerTierPrice(overrides, PlaceholderPrices["larger"])
}

// NewFakeLargerTierPrice sets a custom price.
func NewFakeLargerTierPrice(overrides map[uint64]Labels, price float64) *FakeLargerTier {
	o := make(map[uint64]Labels, len(overrides))
	for k, v := range overrides {
		o[k] = v
	}
	return &FakeLargerTier{Overrides: o, price: price, base: NewCheapTier()}
}

func (*FakeLargerTier) Name() string     { return "larger" }
func (*FakeLargerTier) ModelID() string  { return "fake-larger" }
func (f *FakeLargerTier) Price() float64 { return f.price }

// Label returns the override for th.RootID, else cheap labels at confidence 1.0.
func (f *FakeLargerTier) Label(th ingest.Thread) (Labels, error) {
	if o, ok := f.Overrides[th.RootID]; ok {
		return o, nil
	}
	b, err := f.base.Label(th)
	if err != nil {
		return Labels{}, err
	}
	return NewLabels(b.Intent, b.Urgency, b.Action, 1.0)
}

// FakeAgentTier returns its input labels unchanged and counts calls.
type FakeAgentTier struct {
	Calls int
	price float64
}

// NewFakeAgentTier takes an optional price; without one it uses the placeholder.
func NewFakeAgentTier(price ...float64) *FakeAgentTier {
	return &FakeAgentTier{price: priceOr(PlaceholderPrices["agent"], price)}
}

func (*FakeAgentTier) Name() string     { return "agent" }
func (*FakeAgentTier) ModelID() string  { return "fake-agent" }
func (f *FakeAgentTier) Price() float64 { return f.price }

// Act counts the call and returns labels unchanged.
func (f *FakeAgentTier) Act(_ ingest.Thread, labels Labels) (Labels, error) {
	f.Calls++
	return labels, nil
}

// CheckPrice refuses NaN, ±Inf and negative prices.
func CheckPrice(name string, price float64) error {
	if math.IsNaN(price) || math.IsInf(price, 0) || price < 0 {
		return errors.New("price for " + reprStr(name) + " must be a finite number >= 0, got " + reprFloat(price))
	}
	return nil
}
