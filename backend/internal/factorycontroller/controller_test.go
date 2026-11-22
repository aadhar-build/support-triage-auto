package factorycontroller

import (
	"context"
	"errors"
	"slices"
	"sync"
	"testing"
)

func TestStepLetsProposerChooseFromSameQueue(t *testing.T) {
	for _, choice := range []string{"job-a", "job-b"} {
		t.Run(choice, func(t *testing.T) {
			store := &fixtureStore{snapshot: readyQueue()}
			controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
				for _, job := range view.Jobs {
					if job.ID == choice {
						return pickup(view, job), nil
					}
				}
				t.Fatal("selected job missing from the model view")
				return Proposal{}, nil
			})}
			proposal, err := controller.Step(t.Context())
			if err != nil || proposal.JobID != choice {
				t.Fatalf("Step() = %+v, %v", proposal, err)
			}
			for _, job := range store.snapshot.Jobs {
				if job.ID == choice && (job.Stage != StageBuild || job.Round != 1) {
					t.Errorf("selected job did not enter build round 1: %+v", job)
				}
				if job.ID != choice && (job.Stage != StageReady || job.Round != 0) {
					t.Errorf("unchosen job changed: %+v", job)
				}
			}
			if store.snapshot.ActiveBuilds != 1 || store.commits != 1 {
				t.Fatalf("got %d occupied slots and %d commits", store.snapshot.ActiveBuilds, store.commits)
			}
		})
	}
}

func TestStepPermitsNoAction(t *testing.T) {
	store := &fixtureStore{snapshot: readyQueue()}
	controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
		return Proposal{Action: ActionNoAction, StateRevision: view.Revision}, nil
	})}
	proposal, err := controller.Step(t.Context())
	if err != nil || proposal.Action != ActionNoAction || store.commits != 0 || store.attempts != 0 {
		t.Fatalf("Step() = %+v, %v with %d commits and %d attempts", proposal, err, store.commits, store.attempts)
	}
}

func TestValidateBindsNextRoundToBothRevisions(t *testing.T) {
	snapshot := readyQueue()
	snapshot.Jobs[0].Round = 2
	transition, err := Validate(snapshot, pickup(snapshot, snapshot.Jobs[0]))
	want := BuildTransition{StateRevision: "queue-1", JobID: "job-a", JobRevision: "a-1", NextRound: 3}
	if err != nil || transition != want {
		t.Fatalf("Validate() = %+v, %v, want %+v", transition, err, want)
	}
}

func TestValidateAllowsActiveBuildOutsideView(t *testing.T) {
	snapshot := readyQueue()
	snapshot.BuildCapacity = 2
	snapshot.ActiveBuilds = 1
	if _, err := Validate(snapshot, pickup(snapshot, snapshot.Jobs[0])); err != nil {
		t.Fatalf("Validate() refused a partial queue with a free slot: %v", err)
	}
}

func TestValidateRejectsUndercountedBuildLaneStages(t *testing.T) {
	for _, stage := range []Stage{"build", "gate", "review", "recovery", "approve", "merging"} {
		t.Run(string(stage), func(t *testing.T) {
			snapshot := readyQueue()
			snapshot.Jobs[1].Stage = stage
			snapshot.Jobs[1].Round = 1
			if _, err := Validate(snapshot, pickup(snapshot, snapshot.Jobs[0])); !errors.Is(err, ErrInvalidSnapshot) {
				t.Fatalf("Validate() returned %v for an uncounted %s job", err, stage)
			}
		})
	}
}

func TestApplyRefusesUnsafeProposals(t *testing.T) {
	tests := []struct {
		name   string
		change func(*Snapshot, *Proposal)
		want   error
	}{
		{"unknown action", func(_ *Snapshot, p *Proposal) { p.Action = "merge" }, ErrInvalidProposal},
		{"missing target revision", func(_ *Snapshot, p *Proposal) { p.JobRevision = "" }, ErrInvalidProposal},
		{"no action with a target", func(_ *Snapshot, p *Proposal) { p.Action = ActionNoAction }, ErrInvalidProposal},
		{"unknown target", func(_ *Snapshot, p *Proposal) { p.JobID = "job-missing" }, ErrInvalidProposal},
		{"ambiguous target", func(s *Snapshot, _ *Proposal) { s.Jobs = append(s.Jobs, s.Jobs[0]) }, ErrInvalidSnapshot},
		{"stale state", func(_ *Snapshot, p *Proposal) { p.StateRevision = "previous" }, ErrStale},
		{"stale target", func(_ *Snapshot, p *Proposal) { p.JobRevision = "previous" }, ErrStale},
		{"wrong stage", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Stage = "needs-input" }, ErrWrongStage},
		{"missing approval", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Owner = "" }, ErrOwnerAuthorization},
		{"owner decision pending", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Owner = "awaiting-decision" }, ErrOwnerAuthorization},
		{"missing spec", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Spec.Digest = "" }, ErrPrerequisites},
		{"spec belongs to a different job", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Spec.JobID = "job-b" }, ErrPrerequisites},
		{"spec bound to different job state", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Spec.JobRevision = "previous" }, ErrPrerequisites},
		{"base moved", func(s *Snapshot, _ *Proposal) { s.BaseRevision = "new-base" }, ErrPrerequisites},
		{"open product questions", func(s *Snapshot, _ *Proposal) { s.Jobs[0].Spec.QuestionsClear = false }, ErrPrerequisites},
		{"next round already exists", func(s *Snapshot, _ *Proposal) { s.Jobs[0].NextRoundAvailable = false }, ErrPrerequisites},
		{"full lane", func(s *Snapshot, _ *Proposal) { s.ActiveBuilds = 1 }, ErrNoCapacity},
		{"unknown lane capacity", func(s *Snapshot, _ *Proposal) { s.ActiveBuilds = -1 }, ErrInvalidSnapshot},
		{"undercounted lane occupancy", func(s *Snapshot, _ *Proposal) { s.Jobs[1].Stage = StageBuild }, ErrInvalidSnapshot},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			snapshot := readyQueue()
			proposal := pickup(snapshot, snapshot.Jobs[0])
			tt.change(&snapshot, &proposal)
			store := &fixtureStore{snapshot: snapshot}
			controller := Controller{Store: store}
			if err := controller.Apply(t.Context(), proposal); !errors.Is(err, tt.want) {
				t.Fatalf("Apply() error = %v, want %v", err, tt.want)
			}
			if store.attempts != 0 || store.commits != 0 {
				t.Fatalf("rejected proposal reached CAS: %d attempts, %d commits", store.attempts, store.commits)
			}
		})
	}
}

func TestStepProposerCannotChangeTrustedSnapshot(t *testing.T) {
	snapshot := readyQueue()
	snapshot.Jobs[0].Owner = "blocked"
	store := &fixtureStore{snapshot: snapshot}
	controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
		view.Jobs[0].Owner = OwnerAuthorized
		return pickup(view, view.Jobs[0]), nil
	})}
	if _, err := controller.Step(t.Context()); !errors.Is(err, ErrOwnerAuthorization) {
		t.Fatalf("Step() returned %v after proposer tried to supply its own authorization", err)
	}
	if store.commits != 0 {
		t.Fatal("proposer-supplied authorization committed a transition")
	}
}

func TestApplyRejectsDuplicateAfterCommit(t *testing.T) {
	snapshot := readyQueue()
	store := &fixtureStore{snapshot: snapshot}
	controller := Controller{Store: store}
	proposal := pickup(snapshot, snapshot.Jobs[0])
	if err := controller.Apply(t.Context(), proposal); err != nil {
		t.Fatal(err)
	}
	if err := controller.Apply(t.Context(), proposal); !errors.Is(err, ErrStale) {
		t.Fatalf("duplicate Apply() error = %v, want stale", err)
	}
	if store.attempts != 1 || store.commits != 1 {
		t.Fatalf("duplicate reached CAS: %d attempts, %d commits", store.attempts, store.commits)
	}
}

func TestStepConcurrentProposalsCommitOneSlot(t *testing.T) {
	for _, tt := range []struct {
		name string
		jobs [2]int
	}{
		{"same job", [2]int{0, 0}},
		{"different jobs", [2]int{0, 1}},
	} {
		t.Run(tt.name, func(t *testing.T) {
			store := &fixtureStore{snapshot: readyQueue()}
			ready := make(chan struct{}, 2)
			release := make(chan struct{})
			results := make(chan error, 2)
			for _, choice := range tt.jobs {
				controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
					ready <- struct{}{}
					<-release
					return pickup(view, view.Jobs[choice]), nil
				})}
				go func() {
					_, err := controller.Step(t.Context())
					results <- err
				}()
			}
			<-ready
			<-ready
			close(release)
			first, second := <-results, <-results
			if first != nil {
				first, second = second, first
			}
			if first != nil || !errors.Is(second, ErrStale) {
				t.Fatalf("got %v and %v, want one commit and one stale decision", first, second)
			}
			if store.attempts != 2 || store.commits != 1 || store.snapshot.ActiveBuilds != 1 {
				t.Fatalf("got %d attempts, %d commits and %d occupied slots", store.attempts, store.commits, store.snapshot.ActiveBuilds)
			}
		})
	}
}

func TestStepRefusesProposalAfterAuthorityChanges(t *testing.T) {
	store := &fixtureStore{snapshot: readyQueue()}
	controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
		store.snapshot.Jobs[0].Owner = "blocked"
		store.snapshot.Revision = "revoked"
		return pickup(view, view.Jobs[0]), nil
	})}
	if _, err := controller.Step(t.Context()); !errors.Is(err, ErrStale) {
		t.Fatalf("Step() returned %v after owner revoked authority", err)
	}
	if store.commits != 0 {
		t.Fatal("proposal committed after authority was revoked")
	}
}

func TestStepDoesNotApplyAProposalReturnedWithAnError(t *testing.T) {
	store := &fixtureStore{snapshot: readyQueue()}
	want := errors.New("proposer failed")
	controller := Controller{Store: store, Proposer: proposerFunc(func(_ context.Context, view Snapshot) (Proposal, error) {
		return pickup(view, view.Jobs[0]), want
	})}
	if _, err := controller.Step(t.Context()); !errors.Is(err, want) || store.attempts != 0 {
		t.Fatalf("Step() returned %v and attempted %d transitions", err, store.attempts)
	}
}

type proposerFunc func(context.Context, Snapshot) (Proposal, error)

func (f proposerFunc) Propose(ctx context.Context, snapshot Snapshot) (Proposal, error) {
	return f(ctx, snapshot)
}

func readyQueue() Snapshot {
	return Snapshot{
		Revision:      "queue-1",
		BaseRevision:  "base-current",
		BuildCapacity: 1,
		Jobs: []Job{
			{
				ID: "job-a", Revision: "a-1", Stage: StageReady, Owner: OwnerAuthorized, NextRoundAvailable: true,
				Spec: SpecEvidence{JobID: "job-a", JobRevision: "a-1", BaseRevision: "base-current", Digest: "spec-a", QuestionsClear: true},
			},
			{
				ID: "job-b", Revision: "b-1", Stage: StageReady, Owner: OwnerAuthorized, NextRoundAvailable: true,
				Spec: SpecEvidence{JobID: "job-b", JobRevision: "b-1", BaseRevision: "base-current", Digest: "spec-b", QuestionsClear: true},
			},
		},
	}
}

func pickup(snapshot Snapshot, job Job) Proposal {
	return Proposal{Action: ActionPickupBuild, StateRevision: snapshot.Revision, JobID: job.ID, JobRevision: job.Revision}
}

// fixtureStore models an atomic state boundary without touching the factory.
type fixtureStore struct {
	mu       sync.Mutex
	snapshot Snapshot
	attempts int
	commits  int
}

func (s *fixtureStore) Load(context.Context) (Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	snapshot := s.snapshot
	snapshot.Jobs = slices.Clone(snapshot.Jobs)
	return snapshot, nil
}

func (s *fixtureStore) CompareAndSwapBuild(_ context.Context, transition BuildTransition) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.attempts++
	if transition.StateRevision != s.snapshot.Revision {
		return false, nil
	}
	for index, job := range s.snapshot.Jobs {
		if transition.JobID == job.ID && transition.JobRevision == job.Revision {
			job.Stage = StageBuild
			job.Round = transition.NextRound
			job.Revision += "-applied"
			s.snapshot.Jobs[index] = job
			s.snapshot.Revision += "-applied"
			s.snapshot.ActiveBuilds++
			s.commits++
			return true, nil
		}
	}
	return false, nil
}
