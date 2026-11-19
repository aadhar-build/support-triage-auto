package route

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
)

// syntheticRepeatCounts stands in for context_signals.contact_counts over the
// rules file's window_days (7-day) window, until that port lands. These are
// Python's values for the seven synthetic cases that have a `customer`.
var syntheticRepeatCounts = map[uint64]int{57: 1, 58: 2, 59: 3, 60: 1, 61: 1, 62: 1, 63: 1}

// syntheticThreads mirrors run_synthetic._thread for routing: RootID is the
// 1-based position, turn i has ID position*1000+i.
func syntheticThreads(t *testing.T, casesJSON string) []ingest.Thread {
	t.Helper()
	b, err := os.ReadFile(casesJSON)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Turns []struct {
				Inbound bool   `json:"inbound"`
				Text    string `json:"text"`
			} `json:"turns"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatal(err)
	}
	var out []ingest.Thread
	for p, c := range doc.Cases {
		pos := uint64(p + 1)
		th := ingest.Thread{RootID: pos}
		for i, tu := range c.Turns {
			author := "made_up_brand"
			if tu.Inbound {
				author = "made_up_customer"
			}
			th.Turns = append(th.Turns, ingest.Turn{ID: pos*1000 + uint64(i+1), AuthorID: author, Inbound: tu.Inbound, CreatedAt: time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC), Text: tu.Text})
		}
		out = append(out, th)
	}
	return out
}

func minConversations(t *testing.T) int {
	t.Helper()
	b, err := os.ReadFile("policies/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := policy.ParseJSON(b)
	if err != nil {
		t.Fatal(err)
	}
	rc, ok := v.Get("repeat_contact")
	if !ok {
		t.Fatal("rules.json has no repeat_contact")
	}
	mc, ok := rc.Get("min_conversations")
	if !ok || !mc.IsInt() {
		t.Fatal("repeat_contact.min_conversations missing or not an int")
	}
	n, err := strconv.Atoi(mc.Num)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestSyntheticTraceGolden(t *testing.T) {
	_, _, casesJSON := parity.Inputs(t)
	root := parity.RepoRoot(t)
	t.Chdir(root)

	tp, err := policy.LoadEscalateTopics("policies/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	version, ok, err := policy.RulesVersion("policies/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	var rulesVersion *string
	if ok {
		rulesVersion = &version
	}
	threads := syntheticThreads(t, casesJSON)
	if len(threads) != 63 {
		t.Fatalf("got %d synthetic cases, want 63", len(threads))
	}
	routed, err := RouteThreads(threads, NewCheapTier(), NewFakeLargerTier(nil), NewFakeAgentTier(), 0.5, tp,
		&RepeatInput{MinConversations: minConversations(t), Counts: syntheticRepeatCounts})
	if err != nil {
		t.Fatal(err)
	}

	// Decoded cross-check: names a routing fault before the byte diff does.
	codes := map[string]int{}
	confs := map[float64]int{}
	for _, r := range routed {
		codes[strings.Join(r.ReasonCodes, "+")]++
		confs[r.Labels.Confidence]++
	}
	wantCodes := map[string]int{"cheap_confident": 27, "low_confidence_to_larger": 22, "escalate_always": 7, "high_risk_to_larger": 6, "cheap_confident+repeat_contact": 1}
	if !reflect.DeepEqual(codes, wantCodes) {
		t.Errorf("reason-code tally %v, want %v", codes, wantCodes)
	}
	wantConfs := map[float64]int{1.0: 28, 0.6: 26, 0.2: 5, 0.75: 4}
	if !reflect.DeepEqual(confs, wantConfs) {
		t.Errorf("final-confidence tally %v, want %v", confs, wantConfs)
	}
	if !slices.Equal(routed[3].TopicID, []string{"refund-compensation-demand"}) {
		t.Errorf("thread 4 topic_id %v", routed[3].TopicID)
	}

	run := RunInfo{CodeVersion: parity.PlaceholderCommit, CodeDirty: false, RulesVersion: rulesVersion, DriftConfigVersion: nil, Threshold: 0.5, Split: "synthetic", Seed: 0}
	dest := filepath.Join(t.TempDir(), "trace.jsonl")
	n, err := WriteTrace(root, dest, func(yield func(Record) bool) {
		for _, r := range routed {
			if !yield(BuildRecord(r, run)) {
				return
			}
		}
	})
	if err != nil || n != 63 {
		t.Fatalf("WriteTrace = %d, %v", n, err)
	}
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	parity.AssertGolden(t, "run_synthetic/trace.jsonl", got)
}
