package route

import (
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
)

var t0 = time.Date(2017, 10, 31, 22, 0, 0, 0, time.UTC)

// thread is one made-up inbound turn (tests/test_tiers.py thread).
func thread(rootID uint64, text string) ingest.Thread {
	return ingest.Thread{RootID: rootID, Turns: []ingest.Turn{{ID: rootID, AuthorID: "cust", Inbound: true, CreatedAt: t0, Text: text}}}
}

type turnSpec struct {
	inbound bool
	text    string
}

// threadOf builds a thread from (inbound, text) pairs (tests/test_cheap_tier.py thread).
func threadOf(rootID uint64, turns ...turnSpec) ingest.Thread {
	th := ingest.Thread{RootID: rootID}
	for i, ts := range turns {
		author := "BrandX"
		if ts.inbound {
			author = "cust_x"
		} else {
			th.BrandReplied = true
			th.BrandIDs = []string{"BrandX"}
		}
		th.Turns = append(th.Turns, ingest.Turn{ID: rootID + uint64(i), AuthorID: author, Inbound: ts.inbound, CreatedAt: t0.Add(time.Duration(i) * time.Minute), Text: ts.text})
	}
	return th
}

func cust(text string) ingest.Thread {
	return threadOf(100, turnSpec{true, text + " SENTINEL_TEXT_100"})
}

func mustLabels(t *testing.T, intent, urgency, action string, c float64) Labels {
	t.Helper()
	l, err := NewLabels(intent, urgency, action, c)
	if err != nil {
		t.Fatalf("NewLabels: %v", err)
	}
	return l
}

func label(t *testing.T, th ingest.Thread) Labels {
	t.Helper()
	l, err := NewCheapTier().Label(th)
	if err != nil {
		t.Fatalf("Label: %v", err)
	}
	return l
}
