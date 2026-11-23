// Package factorycontroller validates model-selected factory decisions against
// trusted state. Its first action admits one ready job to the build lane.
package factorycontroller

import (
	"context"
	"errors"
	"fmt"
	"math"
	"slices"
	"strings"
)

type Stage string

const (
	StageReady Stage = "ready"
	StageBuild Stage = "build"
)

// OccupiesBuildLane reports whether a job at this stage uses a build lane slot.
func (s Stage) OccupiesBuildLane() bool {
	switch s {
	case StageBuild, "gate", "review", "recovery", "approve", "merging":
		return true
	default:
		return false
	}
}

type OwnerStatus string

// OwnerAuthorized means the owner admitted the job and no owner decision or hold blocks pickup.
const OwnerAuthorized OwnerStatus = "authorized"

// SpecEvidence describes a spec verified independently by the trusted store.
type SpecEvidence struct {
	JobID          string
	JobRevision    string
	BaseRevision   string
	Digest         string
	QuestionsClear bool
}

type Job struct {
	ID                 string
	Revision           string
	Stage              Stage
	Round              int
	Owner              OwnerStatus
	Spec               SpecEvidence
	NextRoundAvailable bool
}

// Snapshot is a coherent read of trusted decision inputs. The store issues a
// fresh revision whenever any input changes, including the current base,
// active lane occupants, evidence and owner authorization.
// Jobs may list a subset of the queue. ActiveBuilds covers the whole build lane.
type Snapshot struct {
	Revision      string
	BaseRevision  string
	BuildCapacity int
	ActiveBuilds  int
	Jobs          []Job
}

type Action string

const (
	ActionNoAction    Action = "no-action"
	ActionPickupBuild Action = "pickup-build"
)

// Proposal selects an action and echoes the revisions from the model's view.
// A build pickup requires a job ID and revision. No action leaves both empty.
type Proposal struct {
	Action        Action
	StateRevision string
	JobID         string
	JobRevision   string
}

// Proposer can select any job supplied in the view or choose no action.
// The controller treats its output as untrusted.
type Proposer interface {
	Propose(context.Context, Snapshot) (Proposal, error)
}

type BuildTransition struct {
	StateRevision string
	JobID         string
	JobRevision   string
	NextRound     int
}

// Store owns the state and the atomic mutation boundary. Load returns a snapshot
// that stays stable for the caller. CompareAndSwapBuild atomically matches both
// revisions, moves the job to build and reserves one slot. It issues fresh
// revisions on success and returns false on a mismatch. Revision values must
// never be reused for a different state, even if the contents revert.
// The store must honor cancellation before committing the transition.
type Store interface {
	Load(context.Context) (Snapshot, error)
	CompareAndSwapBuild(context.Context, BuildTransition) (bool, error)
}

var (
	ErrInvalidProposal    = errors.New("invalid factory proposal")
	ErrInvalidSnapshot    = errors.New("invalid factory snapshot")
	ErrStale              = errors.New("factory proposal is stale")
	ErrWrongStage         = errors.New("build pickup requires a ready job")
	ErrOwnerAuthorization = errors.New("build pickup requires current owner authorization")
	ErrPrerequisites      = errors.New("build pickup prerequisites are incomplete")
	ErrNoCapacity         = errors.New("build lane has no available slots")
)

type Controller struct {
	Store    Store
	Proposer Proposer
}

// Step obtains a single proposal from the model and applies it against the
// snapshot the model saw. The caller can inspect the proposal on refusal.
func (c Controller) Step(ctx context.Context) (Proposal, error) {
	if c.Proposer == nil {
		return Proposal{}, errors.New("factory controller requires a proposer")
	}
	snapshot, err := c.load(ctx)
	if err != nil {
		return Proposal{}, err
	}
	view := snapshot
	view.Jobs = slices.Clone(snapshot.Jobs)
	proposal, err := c.Proposer.Propose(ctx, view)
	if err != nil {
		return Proposal{}, fmt.Errorf("propose factory action: %w", err)
	}
	return proposal, c.apply(ctx, snapshot, proposal)
}

// Apply checks a previously obtained proposal against current state and attempts
// the same revision-checked mutation used by Step.
func (c Controller) Apply(ctx context.Context, proposal Proposal) error {
	snapshot, err := c.load(ctx)
	if err != nil {
		return err
	}
	return c.apply(ctx, snapshot, proposal)
}

func (c Controller) load(ctx context.Context) (Snapshot, error) {
	if c.Store == nil {
		return Snapshot{}, errors.New("factory controller requires a store")
	}
	snapshot, err := c.Store.Load(ctx)
	if err != nil {
		return Snapshot{}, fmt.Errorf("load factory snapshot: %w", err)
	}
	return snapshot, nil
}

func (c Controller) apply(ctx context.Context, snapshot Snapshot, proposal Proposal) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	transition, err := Validate(snapshot, proposal)
	if err != nil || proposal.Action == ActionNoAction {
		return err
	}
	applied, err := c.Store.CompareAndSwapBuild(ctx, transition)
	if err != nil {
		return fmt.Errorf("apply factory build pickup: %w", err)
	}
	if !applied {
		return ErrStale
	}
	return nil
}

// Validate checks a model proposal against a trusted snapshot. It returns the
// build transition to compare and swap, or a zero transition for no action.
func Validate(snapshot Snapshot, proposal Proposal) (BuildTransition, error) {
	if strings.TrimSpace(proposal.StateRevision) == "" {
		return BuildTransition{}, fmt.Errorf("%w: state revision is required", ErrInvalidProposal)
	}
	if strings.TrimSpace(snapshot.Revision) == "" {
		return BuildTransition{}, fmt.Errorf("%w: state revision is missing", ErrInvalidSnapshot)
	}
	if proposal.StateRevision != snapshot.Revision {
		return BuildTransition{}, ErrStale
	}
	if err := checkLaneOccupancy(snapshot); err != nil {
		return BuildTransition{}, err
	}
	switch proposal.Action {
	case ActionNoAction:
		if proposal.JobID != "" || proposal.JobRevision != "" {
			return BuildTransition{}, fmt.Errorf("%w: no action cannot target a job", ErrInvalidProposal)
		}
		return BuildTransition{}, nil
	case ActionPickupBuild:
		if strings.TrimSpace(proposal.JobID) == "" || strings.TrimSpace(proposal.JobRevision) == "" {
			return BuildTransition{}, fmt.Errorf("%w: build pickup requires a job ID and revision", ErrInvalidProposal)
		}
	default:
		return BuildTransition{}, fmt.Errorf("%w: unsupported action", ErrInvalidProposal)
	}
	job, err := findJob(snapshot.Jobs, proposal.JobID)
	if err != nil {
		return BuildTransition{}, err
	}
	if job.Revision != proposal.JobRevision {
		return BuildTransition{}, ErrStale
	}
	if job.Stage != StageReady {
		return BuildTransition{}, ErrWrongStage
	}
	if job.Owner != OwnerAuthorized {
		return BuildTransition{}, ErrOwnerAuthorization
	}
	if err := checkBuildPrerequisites(snapshot, job); err != nil {
		return BuildTransition{}, err
	}
	return BuildTransition{
		StateRevision: snapshot.Revision,
		JobID:         job.ID,
		JobRevision:   job.Revision,
		NextRound:     job.Round + 1,
	}, nil
}

func checkLaneOccupancy(snapshot Snapshot) error {
	if snapshot.BuildCapacity < 0 || snapshot.ActiveBuilds < 0 {
		return fmt.Errorf("%w: build lane capacity is unknown", ErrInvalidSnapshot)
	}
	var listedBuilds int
	for _, job := range snapshot.Jobs {
		if job.Stage.OccupiesBuildLane() {
			listedBuilds++
		}
	}
	if listedBuilds > snapshot.ActiveBuilds {
		return fmt.Errorf("%w: listed build lane occupants exceed active build count", ErrInvalidSnapshot)
	}
	return nil
}

func findJob(jobs []Job, id string) (Job, error) {
	var match Job
	for _, job := range jobs {
		if job.ID != id {
			continue
		}
		if match.ID != "" {
			return Job{}, fmt.Errorf("%w: job ID appears more than once", ErrInvalidSnapshot)
		}
		match = job
	}
	if match.ID == "" {
		return Job{}, fmt.Errorf("%w: target job is absent", ErrInvalidProposal)
	}
	return match, nil
}

func checkBuildPrerequisites(snapshot Snapshot, job Job) error {
	spec := job.Spec
	if strings.TrimSpace(spec.Digest) == "" || spec.JobID != job.ID || spec.JobRevision != job.Revision {
		return fmt.Errorf("%w: no verified spec for this job revision", ErrPrerequisites)
	}
	if strings.TrimSpace(snapshot.BaseRevision) == "" || spec.BaseRevision != snapshot.BaseRevision {
		return fmt.Errorf("%w: spec does not match the current base", ErrPrerequisites)
	}
	if !spec.QuestionsClear {
		return fmt.Errorf("%w: product questions are unresolved", ErrPrerequisites)
	}
	if job.Round < 0 || job.Round == math.MaxInt || !job.NextRoundAvailable {
		return fmt.Errorf("%w: next build round is unavailable", ErrPrerequisites)
	}
	if snapshot.ActiveBuilds >= snapshot.BuildCapacity {
		return ErrNoCapacity
	}
	return nil
}
