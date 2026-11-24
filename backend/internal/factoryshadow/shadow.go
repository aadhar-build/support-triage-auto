// Package factoryshadow observes and validates scheduling decisions without a
// store, a dispatcher, or a mutation boundary.
package factoryshadow

import (
	"context"
	"errors"
	"slices"
	"strings"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryproposer"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factorystate"
)

const (
	StatusObserved = "observed"
	StatusAccepted = "accepted"
	maxJobs        = 256
	maxAttempts    = 3
	unknown        = "unknown"
)

type Proposer interface {
	ProposeWithReport(context.Context, factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error)
}

// Runner observes the checkout before it initializes a model. Leave
// NewProposer nil for observation only.
type Runner struct {
	Observe     func(context.Context, string) (factorystate.Observation, error)
	NewProposer func() (Proposer, error)
}

// Report contains aggregate observations and fixed status values. It omits
// job IDs, revisions, paths, prompts, model text and provider diagnostics.
type Report struct {
	Status      string            `json:"status"`
	ElapsedMS   int64             `json:"elapsed_ms"`
	Observation *Inventory        `json:"observation,omitempty"`
	Attempts    []ProviderAttempt `json:"attempts,omitempty"`
	Action      string            `json:"action,omitempty"`
	Candidate   int               `json:"candidate,omitempty"`
	Decision    string            `json:"decision,omitempty"`
	Cost        string            `json:"cost_estimate"`
	Limitations []string          `json:"limitations"`
}

type Inventory struct {
	Jobs             int            `json:"jobs"`
	ReadyJobs        int            `json:"ready_jobs"`
	EligibleBuilds   int            `json:"eligible_builds"`
	BuildCapacity    int            `json:"configured_build_capacity"`
	ActiveBuilds     int            `json:"observed_build_lane_jobs"`
	SpecStatuses     map[string]int `json:"ready_local_spec_statuses,omitempty"`
	QuestionStatuses map[string]int `json:"ready_local_question_statuses,omitempty"`
}

type ProviderAttempt struct {
	Provider    string `json:"provider"`
	Model       string `json:"model"`
	DurationMS  *int64 `json:"duration_ms,omitempty"`
	Cost        string `json:"cost_estimate"`
	Disposition string `json:"disposition"`
	Phase       string `json:"phase,omitempty"`
	Failure     string `json:"failure,omitempty"`
}

func (r Runner) Run(ctx context.Context, checkout string) (report Report) {
	started := time.Now()
	defer func() { report.ElapsedMS = time.Since(started).Milliseconds() }()
	report = Report{
		Status: "observation-failed",
		Cost:   unknown,
		Limitations: []string{
			"point-in-time-local-observation", "remote-base-unverified", "capacity-leases-unverified",
			"owner-authorization-unverified", "spec-binding-unverified",
		},
	}
	if ctx.Err() != nil {
		report.Status = "canceled"
		return report
	}
	if r.Observe == nil {
		return report
	}
	observed, err := r.Observe(ctx, checkout)
	switch {
	case ctx.Err() != nil:
		report.Status = "canceled"
		return report
	case errors.Is(err, factorystate.ErrBusy):
		report.Status = "observation-busy"
		return report
	case errors.Is(err, factorystate.ErrStaleCheckout):
		report.Status = "stale-local-checkout"
		return report
	case err != nil:
		return report
	}
	observed.Snapshot.Jobs = slices.Clone(observed.Snapshot.Jobs)
	report.Observation = summarize(observed)
	if report.Observation == nil {
		report.Status = "invalid-observation"
		return report
	}
	if r.NewProposer == nil {
		report.Status = StatusObserved
		return report
	}
	if ctx.Err() != nil {
		report.Status = "canceled"
		return report
	}
	proposer, err := r.NewProposer()
	if err != nil || proposer == nil {
		report.Status = "model-configuration-invalid"
		return report
	}
	if ctx.Err() != nil {
		report.Status = "canceled"
		return report
	}
	view := observed.Snapshot
	view.Jobs = slices.Clone(view.Jobs)
	proposal, attempts, err := proposer.ProposeWithReport(ctx, view)
	report.Attempts = summarizeAttempts(attempts)
	if ctx.Err() != nil {
		report.Status = "canceled"
		return report
	}
	if err != nil {
		report.Status = "no-model-decision"
		return report
	}
	transition, err := factorycontroller.Validate(observed.Snapshot, proposal)
	if err != nil {
		report.Status = "proposal-rejected"
		report.Decision = rejection(err)
		return report
	}
	report.Status = StatusAccepted
	report.Action = string(proposal.Action)
	if proposal.Action == factorycontroller.ActionPickupBuild {
		report.Candidate = slices.IndexFunc(observed.Snapshot.Jobs, func(job factorycontroller.Job) bool {
			return job.ID == transition.JobID
		}) + 1
	}
	return report
}

func summarize(observed factorystate.Observation) *Inventory {
	snapshot := observed.Snapshot
	if strings.TrimSpace(snapshot.BaseRevision) == "" || len(snapshot.Jobs) > maxJobs || len(observed.ReadyJobs) > maxJobs {
		return nil
	}
	noAction := factorycontroller.Proposal{Action: factorycontroller.ActionNoAction, StateRevision: snapshot.Revision}
	if _, err := factorycontroller.Validate(snapshot, noAction); err != nil {
		return nil
	}
	inventory := &Inventory{
		Jobs: len(snapshot.Jobs), BuildCapacity: snapshot.BuildCapacity, ActiveBuilds: snapshot.ActiveBuilds,
		SpecStatuses: make(map[string]int), QuestionStatuses: make(map[string]int),
	}
	for _, job := range snapshot.Jobs {
		if strings.TrimSpace(job.ID) == "" || strings.TrimSpace(job.Revision) == "" || job.Round < 0 {
			return nil
		}
		if job.Stage == factorycontroller.StageReady {
			inventory.ReadyJobs++
		}
		pickup := factorycontroller.Proposal{
			Action: factorycontroller.ActionPickupBuild, StateRevision: snapshot.Revision,
			JobID: job.ID, JobRevision: job.Revision,
		}
		_, err := factorycontroller.Validate(snapshot, pickup)
		if errors.Is(err, factorycontroller.ErrInvalidSnapshot) {
			return nil
		}
		if err == nil {
			inventory.EligibleBuilds++
		}
	}
	for _, job := range observed.ReadyJobs {
		switch job.Spec {
		case factorystate.SpecMissing, factorystate.SpecInvalid, factorystate.SpecBusy,
			factorystate.SpecReceiptMissing, factorystate.SpecReceiptInvalid,
			factorystate.SpecBaseUnverified, factorystate.SpecBaseMismatch, factorystate.SpecUnbound:
			inventory.SpecStatuses[string(job.Spec)]++
		default:
			inventory.SpecStatuses["unknown"]++
		}
		switch job.Questions {
		case factorystate.QuestionsUnknown, factorystate.QuestionsOpen, factorystate.QuestionsClear:
			inventory.QuestionStatuses[string(job.Questions)]++
		default:
			inventory.QuestionStatuses["unknown"]++
		}
	}
	return inventory
}

func summarizeAttempts(attempts []factoryproposer.Attempt) []ProviderAttempt {
	var result []ProviderAttempt
	for _, attempt := range attempts[:min(len(attempts), maxAttempts)] {
		summary := ProviderAttempt{Provider: unknown, Model: reportModel(attempt.Provider, attempt.Model), Cost: unknown, Disposition: unknown}
		switch attempt.Provider {
		case "claude", "codex":
			summary.Provider = attempt.Provider
		}
		if attempt.Duration > 0 {
			elapsedMS := attempt.Duration.Milliseconds()
			summary.DurationMS = &elapsedMS
		}
		switch attempt.Disposition {
		case factoryproposer.Accepted, factoryproposer.Unavailable, factoryproposer.AuthFailed, factoryproposer.UnsafeProfile,
			factoryproposer.Failed, factoryproposer.Invalid, factoryproposer.TimedOut,
			factoryproposer.OutputTooLarge, factoryproposer.Canceled:
			summary.Disposition = string(attempt.Disposition)
		}
		switch attempt.Phase {
		case factoryproposer.PhaseSetup, factoryproposer.PhaseLogin, factoryproposer.PhaseModel, factoryproposer.PhaseDecision:
			summary.Phase = string(attempt.Phase)
		case "":
		default:
			summary.Phase = "unknown"
		}
		switch attempt.Failure {
		case factoryproposer.FailureRunner, factoryproposer.FailureNativeExit, factoryproposer.FailureCLIArgs,
			factoryproposer.FailureCLIConfig, factoryproposer.FailureAuth, factoryproposer.FailureNetwork, factoryproposer.FailureModel:
			summary.Failure = string(attempt.Failure)
		case "":
		default:
			summary.Failure = "unknown"
		}
		result = append(result, summary)
	}
	return result
}

// Report only public model names known to this provider. A generic syntax
// check would also accept private aliases, deployment IDs and credentials.
func reportModel(provider, model string) string {
	switch provider {
	case "claude":
		switch model {
		case "opus", "sonnet", "haiku":
			return model
		}
	case "codex":
		if model == "gpt-6.1-terra" {
			return model
		}
	}
	return unknown
}

func rejection(err error) string {
	switch {
	case errors.Is(err, factorycontroller.ErrStale):
		return "stale-revision"
	case errors.Is(err, factorycontroller.ErrWrongStage):
		return "wrong-stage"
	case errors.Is(err, factorycontroller.ErrOwnerAuthorization):
		return "owner-authorization-required"
	case errors.Is(err, factorycontroller.ErrPrerequisites):
		return "prerequisites-incomplete"
	case errors.Is(err, factorycontroller.ErrNoCapacity):
		return "no-build-capacity"
	case errors.Is(err, factorycontroller.ErrInvalidSnapshot):
		return "invalid-observation"
	default:
		return "invalid-proposal"
	}
}
