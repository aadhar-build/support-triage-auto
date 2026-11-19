package route

import (
	"errors"
	"fmt"
	"math"
	"slices"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
)

// DefaultConfidenceThreshold is a hand-picked placeholder, not tuned.
const DefaultConfidenceThreshold = 0.5

// Reason codes.
const (
	CheapConfident        = "cheap_confident"
	LowConfidenceToLarger = "low_confidence_to_larger"
	HighRiskToLarger      = "high_risk_to_larger"
	EscalateAlways        = "escalate_always"
	RepeatContact         = "repeat_contact"
)

// CascadeCodes are the three cascade codes in order.
var CascadeCodes = []string{CheapConfident, LowConfidenceToLarger, HighRiskToLarger}

// ReasonCodes is the registry of every reason code any module may emit.
var ReasonCodes = append(slices.Clone(CascadeCodes), EscalateAlways, RepeatContact)

// IsKnownReasonCode is true only when code is a string in codes. A nil codes
// means ReasonCodes; an empty non-nil slice knows no code.
func IsKnownReasonCode(code any, codes []string) bool {
	if codes == nil {
		codes = ReasonCodes
	}
	s, ok := code.(string)
	return ok && slices.Contains(codes, s)
}

// TierStep is one tier's labels in a routed thread.
type TierStep struct {
	Tier, ModelID string
	Labels        Labels
}

// RoutedThread is router.RoutedThread.
type RoutedThread struct {
	ThreadID            uint64
	Labels, CheapLabels Labels
	Tiers               []string
	Steps               []TierStep
	ReasonCodes         []string
	TopicID             []string
}

// RepeatInput is the repeat-contact rule's threshold and the per-root counts.
type RepeatInput struct {
	MinConversations int
	Counts           map[uint64]int
}

// IsHighRisk is router.is_high_risk.
func IsHighRisk(l Labels) bool {
	return l.Urgency == "high" || l.Intent == "account_access" || l.Intent == "outage_technical" || l.Action == "escalate"
}

func checkThreshold(th float64) error {
	if math.IsNaN(th) || math.IsInf(th, 0) || th < 0 || th > 1 {
		return errors.New("threshold must be a number in [0, 1], got " + reprFloat(th))
	}
	return nil
}

type named interface {
	Name() string
	ModelID() string
}

func checkModelIDs(tiers ...named) error {
	for _, t := range tiers {
		if t.ModelID() == "" {
			return errors.New("tier " + reprStr(t.Name()) + " has no model_id: every tier must set a non-empty string")
		}
	}
	return nil
}

func checkRepeat(r *RepeatInput) error {
	if r != nil && r.Counts == nil {
		return errors.New("repeat_rule and repeat_counts must be given together")
	}
	return nil
}

// errNilTopics is Python's TypeError text from tuple(None). The exact wording
// is pending a Fable tiebreak (job 068 round 2); change it only here.
const errNilTopics = "'NoneType' object is not iterable"

// check runs Python's argument checks in its order: threshold, model ids,
// repeat, then topics (tuple(topics)). A nil topics is refused so a caller
// that forgot to load topics can't silently turn escalate_always off.
func check(threshold float64, cheap, larger LabellingTier, agent AgentTier, topics []policy.Topic, repeat *RepeatInput) error {
	if err := checkThreshold(threshold); err != nil {
		return err
	}
	if err := checkModelIDs(cheap, larger, agent); err != nil {
		return err
	}
	if err := checkRepeat(repeat); err != nil {
		return err
	}
	if topics == nil {
		return errors.New(errNilTopics)
	}
	return nil
}

// RouteThread routes one thread. topics are the always-escalate topics and are
// required: an empty slice turns that rule off, nil is refused. repeat nil
// means no repeat-contact rule.
func RouteThread(th ingest.Thread, cheap, larger LabellingTier, agent AgentTier, threshold float64, topics []policy.Topic, repeat *RepeatInput) (RoutedThread, error) {
	if err := check(threshold, cheap, larger, agent, topics, repeat); err != nil {
		return RoutedThread{}, err
	}
	return routeOne(th, cheap, larger, agent, threshold, topics, repeat)
}

// RouteThreads routes threads in order, checking the arguments once first.
func RouteThreads(threads []ingest.Thread, cheap, larger LabellingTier, agent AgentTier, threshold float64, topics []policy.Topic, repeat *RepeatInput) ([]RoutedThread, error) {
	if err := check(threshold, cheap, larger, agent, topics, repeat); err != nil {
		return nil, err
	}
	out := make([]RoutedThread, 0, len(threads))
	for _, th := range threads {
		r, err := routeOne(th, cheap, larger, agent, threshold, topics, repeat)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, nil
}

func cascadeCode(l Labels, threshold float64) string {
	switch {
	case IsHighRisk(l):
		return HighRiskToLarger
	case l.Confidence < threshold:
		return LowConfidenceToLarger
	}
	return CheapConfident
}

// applyRepeat forces escalate and urgency high; the code is added only on a change.
func applyRepeat(r RoutedThread) RoutedThread {
	l := r.Labels
	l.Action, l.Urgency = "escalate", "high"
	if l == r.Labels {
		return r
	}
	r.Labels = l
	r.ReasonCodes = append(slices.Clone(r.ReasonCodes), RepeatContact)
	return r
}

func tierNames(steps []TierStep) []string {
	names := make([]string, len(steps))
	for i, s := range steps {
		names[i] = s.Tier
	}
	return names
}

func routeOne(th ingest.Thread, cheap, larger LabellingTier, agent AgentTier, threshold float64, topics []policy.Topic, repeat *RepeatInput) (RoutedThread, error) {
	wrap := func(err error) error { return fmt.Errorf("thread %d: %w", th.RootID, err) }
	cheapLabels, err := cheap.Label(th)
	if err != nil {
		return RoutedThread{}, wrap(err)
	}
	labels := cheapLabels
	steps := []TierStep{{cheap.Name(), cheap.ModelID(), cheapLabels}}
	var inbound []string
	for _, t := range th.Turns {
		if t.Inbound {
			inbound = append(inbound, t.Text)
		}
	}
	topicID := policy.MatchedTopics(inbound, topics)
	if topicID == nil {
		topicID = []string{}
	}
	fires := repeat != nil && repeat.Counts[th.RootID] >= repeat.MinConversations
	if len(topicID) > 0 {
		forced := cheapLabels
		forced.Action = "escalate"
		draft, err := agent.Act(th, forced) // a draft for the person; its action is discarded
		if err != nil {
			return RoutedThread{}, wrap(err)
		}
		steps = append(steps, TierStep{agent.Name(), agent.ModelID(), draft})
		r := RoutedThread{ThreadID: th.RootID, Labels: forced, CheapLabels: cheapLabels, Tiers: tierNames(steps), Steps: steps, ReasonCodes: []string{EscalateAlways}, TopicID: topicID}
		if fires {
			r = applyRepeat(r)
		}
		return r, nil
	}
	code := cascadeCode(cheapLabels, threshold)
	if code != CheapConfident {
		if labels, err = larger.Label(th); err != nil {
			return RoutedThread{}, wrap(err)
		}
		steps = append(steps, TierStep{larger.Name(), larger.ModelID(), labels})
	}
	if fires {
		return applyRepeat(RoutedThread{ThreadID: th.RootID, Labels: labels, CheapLabels: cheapLabels, Tiers: tierNames(steps), Steps: steps, ReasonCodes: []string{code}, TopicID: topicID}), nil
	}
	if slices.Contains(ActionableActions, labels.Action) {
		if labels, err = agent.Act(th, labels); err != nil {
			return RoutedThread{}, wrap(err)
		}
		steps = append(steps, TierStep{agent.Name(), agent.ModelID(), labels})
	}
	return RoutedThread{ThreadID: th.RootID, Labels: labels, CheapLabels: cheapLabels, Tiers: tierNames(steps), Steps: steps, ReasonCodes: []string{code}, TopicID: topicID}, nil
}
