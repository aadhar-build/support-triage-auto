package factoryshadow

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryproposer"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factorystate"
)

type proposeFunc func(context.Context, factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error)

func (f proposeFunc) ProposeWithReport(ctx context.Context, snapshot factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error) {
	return f(ctx, snapshot)
}

func TestObservationFailureSkipsModel(t *testing.T) {
	for _, test := range []struct {
		name string
		err  error
		want string
	}{
		{"bad read", errors.New("PRIVATE-ISSUE-TEXT"), "observation-failed"},
		{"busy", factorystate.ErrBusy, "observation-busy"},
		{"stale", factorystate.ErrStaleCheckout, "stale-local-checkout"},
		{"invalid snapshot", nil, "invalid-observation"},
	} {
		t.Run(test.name, func(t *testing.T) {
			runner := Runner{
				Observe: func(context.Context, string) (factorystate.Observation, error) {
					return factorystate.Observation{}, test.err
				},
				NewProposer: func() (Proposer, error) {
					t.Fatal("model initialized without a valid observation")
					return nil, nil
				},
			}
			result := runner.Run(context.Background(), "/trusted/checkout")
			if result.Status != test.want || result.Observation != nil || result.Action != "" {
				t.Fatalf("unexpected report %+v", result)
			}
		})
	}
}

func TestModelCannotAuthorizeItsOwnPickup(t *testing.T) {
	observed := readyObservation()
	runner := withProposer(observed, func(_ context.Context, view factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error) {
		if view.Jobs[0].Owner != "" {
			t.Fatal("test requires an unauthorized job")
		}
		view.Jobs[0].Owner = factorycontroller.OwnerAuthorized
		view.Jobs[0].Spec = factorycontroller.SpecEvidence{
			JobID: view.Jobs[0].ID, JobRevision: "1", BaseRevision: "base", Digest: "digest", QuestionsClear: true,
		}
		proposal := factorycontroller.Proposal{Action: factorycontroller.ActionPickupBuild,
			StateRevision: view.Revision, JobID: view.Jobs[0].ID, JobRevision: "1"}
		return proposal, nil, nil
	})
	result := runner.Run(context.Background(), "/trusted/checkout")
	if result.Status != "proposal-rejected" || result.Decision != "owner-authorization-required" || result.Action != "" || result.Candidate != 0 {
		t.Fatalf("unexpected report %+v", result)
	}
	if observed.Snapshot.Jobs[0].Owner != "" || observed.Snapshot.Jobs[0].Spec.Digest != "" {
		t.Fatal("the observer's snapshot was changed")
	}
}

func TestValidNoActionProducesSanitizedReport(t *testing.T) {
	const privateText = "PRIVATE-ISSUE-TEXT"
	observed := readyObservation()
	runner := withProposer(observed, func(_ context.Context, view factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error) {
		return factorycontroller.Proposal{Action: factorycontroller.ActionNoAction, StateRevision: view.Revision}, []factoryproposer.Attempt{
			{Provider: "claude", Model: privateText, Disposition: factoryproposer.Failed,
				Phase: factoryproposer.PhaseModel, Failure: factoryproposer.FailureNetwork, Rejection: privateText},
			{Provider: privateText, Model: privateText, Disposition: factoryproposer.Disposition(privateText),
				Phase: factoryproposer.Phase(privateText), Failure: factoryproposer.Failure(privateText)},
			{Provider: "codex", Disposition: factoryproposer.Accepted, Phase: factoryproposer.PhaseDecision},
			{Provider: "codex", Disposition: factoryproposer.Accepted},
		}, nil
	})
	result := runner.Run(context.Background(), "/trusted/checkout")
	if result.Status != "accepted" || result.Action != "no-action" || result.Candidate != 0 || result.Cost != "unknown" {
		t.Fatalf("unexpected report %+v", result)
	}
	stats := result.Observation
	if stats == nil || stats.Jobs != 1 || stats.ReadyJobs != 1 || stats.EligibleBuilds != 0 || stats.SpecStatuses["missing-spec"] != 1 || stats.QuestionStatuses["unknown"] != 1 {
		t.Fatalf("unexpected observation %+v", stats)
	}
	if len(result.Attempts) != 3 || result.Attempts[0].Provider != "claude" || result.Attempts[1].Provider != "unknown" || result.Attempts[1].Disposition != "unknown" || result.Attempts[2].Disposition != "accepted" {
		t.Fatalf("unexpected summary %+v", result.Attempts)
	}
	output, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(output), privateText) || strings.Contains(string(output), "1-private-issue-title") {
		t.Fatalf("report contains private text: %s", output)
	}
	var encoded struct {
		Attempts []map[string]string `json:"attempts"`
	}
	if err := json.Unmarshal(output, &encoded); err != nil {
		t.Fatal(err)
	}
	if encoded.Attempts[0]["phase"] != "model" || encoded.Attempts[0]["failure"] != "network" ||
		encoded.Attempts[1]["phase"] != "unknown" || encoded.Attempts[1]["failure"] != "unknown" || encoded.Attempts[2]["phase"] != "decision" {
		t.Fatalf("unexpected encoded attempts %+v", encoded.Attempts)
	}
	if _, present := encoded.Attempts[2]["failure"]; present {
		t.Fatalf("accepted attempt reports a failure: %+v", encoded.Attempts[2])
	}
}

func TestProviderErrorDoesNotBecomeNoAction(t *testing.T) {
	runner := withProposer(readyObservation(), func(_ context.Context, view factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error) {
		return factorycontroller.Proposal{Action: factorycontroller.ActionNoAction, StateRevision: view.Revision}, nil, errors.New("PRIVATE-PROVIDER-OUTPUT")
	})
	result := runner.Run(context.Background(), "/trusted/checkout")
	if result.Status != "no-model-decision" || result.Action != "" || result.Observation == nil {
		t.Fatalf("unexpected report %+v", result)
	}
}

func withProposer(observed factorystate.Observation, propose proposeFunc) Runner {
	return Runner{
		Observe:     func(context.Context, string) (factorystate.Observation, error) { return observed, nil },
		NewProposer: func() (Proposer, error) { return propose, nil },
	}
}

func readyObservation() factorystate.Observation {
	const id = "1-private-issue-title"
	return factorystate.Observation{
		Snapshot: factorycontroller.Snapshot{Revision: "observed", BaseRevision: "base", BuildCapacity: 2,
			Jobs: []factorycontroller.Job{{ID: id, Revision: "1", Stage: factorycontroller.StageReady, NextRoundAvailable: true}}},
		ReadyJobs: []factorystate.ReadyJobObservation{{JobID: id, Spec: factorystate.SpecMissing, Questions: factorystate.QuestionsUnknown}},
	}
}
