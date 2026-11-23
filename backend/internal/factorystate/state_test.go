package factorystate

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
)

const clearSpec = "## Product questions\nNone.\n\n## Files\n- fixture.txt\n\n## Acceptance criteria\n- Demonstrate.\n"

func TestObserveReportsNativeRecordsWithoutGrantingAuthority(t *testing.T) {
	checkout, base := newCheckout(t)
	ready := addJob(t, checkout, "001-ready", "ready", 0, base)
	writeFixture(t, ready, "spec.md", clearSpec+"private evidence body\n")
	writeFixture(t, ready, "worker-spec-writer.json", specAudit(base))
	addJob(t, checkout, "002-building", "build", 1, "")
	addJob(t, checkout, "003-review", "review", 1, "")
	addJob(t, checkout, "004-waiting", "needs-human", 2, "")
	addJob(t, checkout, "005-other-ready", "ready", 0, "")
	addJob(t, checkout, "006-merged", "merged", 1, "")
	addJob(t, checkout, "007-spec", "spec", 0, "")
	addJob(t, checkout, "008-gate", "gate", 1, "")
	addJob(t, checkout, "009-recovery", "recovery", 1, "")
	addJob(t, checkout, "010-approve", "approve", 1, "")
	addJob(t, checkout, "011-merging", "merging", 1, "")

	observed, err := Observe(t.Context(), checkout)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := observed.Snapshot
	if snapshot.Revision == "" || snapshot.BuildCapacity != 3 || snapshot.ActiveBuilds != 6 ||
		snapshot.BaseRevision != base || len(snapshot.Jobs) != 11 {
		t.Fatalf("incorrect snapshot: %+v", snapshot)
	}
	target := snapshot.Jobs[0]
	if target.ID != "001-ready" || target.Revision != "7" || target.Stage != factorycontroller.StageReady ||
		target.Owner != "" || target.Spec != (factorycontroller.SpecEvidence{}) || !target.NextRoundAvailable {
		t.Fatalf("incorrect ready job: %+v", target)
	}
	if len(observed.ReadyJobs) != 2 || observed.ReadyJobs[0] != (ReadyJobObservation{
		JobID: "001-ready", Spec: SpecUnbound, Questions: QuestionsClear,
	}) || observed.ReadyJobs[1] != (ReadyJobObservation{JobID: "005-other-ready", Spec: SpecMissing, Questions: QuestionsUnknown}) {
		t.Fatalf("incorrect ready diagnostics: %+v", observed.ReadyJobs)
	}
	if strings.Contains(fmt.Sprintf("%+v", observed), "private evidence body") {
		t.Fatal("observed state contains private evidence text")
	}
	proposal := factorycontroller.Proposal{
		Action: factorycontroller.ActionPickupBuild, StateRevision: snapshot.Revision, JobID: target.ID, JobRevision: target.Revision,
	}
	if _, err := factorycontroller.Validate(snapshot, proposal); !errors.Is(err, factorycontroller.ErrOwnerAuthorization) {
		t.Fatalf("native state authorized a pickup: %v", err)
	}
	second, err := Load(t.Context(), checkout)
	if err != nil || second.Revision == snapshot.Revision {
		t.Fatalf("expected a distinct observation ID, got %q and %v", second.Revision, err)
	}
	if got, err := os.ReadFile(filepath.Join(ready, "spec.md")); err != nil || string(got) != clearSpec+"private evidence body\n" {
		t.Fatalf("read changed evidence: %q, %v", got, err)
	}
}

func TestObserveIncludesLegacyRecordsWithoutGrantingPickup(t *testing.T) {
	checkout, base := newCheckout(t)
	ready := filepath.Join(checkout, "factory/jobs/001-ready")
	readyRecord := fmt.Sprintf("{\n  \"id\": \"001-ready\", \"stage\": \"ready\", \"round\": 0, \"spec_base\": %q\n}\n", base)
	writeFixture(t, ready, "job.json", readyRecord)
	writeFixture(t, ready, "spec.md", clearSpec)
	writeFixture(t, ready, "worker-spec-writer.json", specAudit(base))
	writeFixture(t, checkout, "factory/jobs/002-merged/job.json", `{"id":"002-merged","stage":"merged","round":1}`)
	writeFixture(t, checkout, "factory/jobs/003-spec/job.json", `{"id":"003-spec","stage":"spec","round":0}`)
	writeFixture(t, checkout, "factory/jobs/004-review/job.json", `{"id":"004-review","stage":"review","round":2}`)
	addJob(t, checkout, "005-build", "build", 1, "")

	observed, err := Observe(t.Context(), checkout)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := observed.Snapshot
	if len(snapshot.Jobs) != 5 || snapshot.ActiveBuilds != 2 {
		t.Fatalf("incorrect mixed inventory: %+v", snapshot)
	}
	wantStages := []factorycontroller.Stage{"ready", "merged", "spec", "review", "build"}
	for i, job := range snapshot.Jobs {
		if job.Stage != wantStages[i] {
			t.Fatalf("job %s stage = %q, want %q", job.ID, job.Stage, wantStages[i])
		}
	}
	target := snapshot.Jobs[0]
	wantRevision := fmt.Sprintf("legacy-observation-sha256-%x", sha256.Sum256([]byte(readyRecord)))
	if target.Revision != wantRevision || snapshot.Jobs[4].Revision != "7" || target.Owner != "" ||
		target.Spec != (factorycontroller.SpecEvidence{}) || target.NextRoundAvailable {
		t.Fatalf("incorrect observed revisions or ready job evidence: %+v", snapshot.Jobs)
	}
	if len(observed.ReadyJobs) != 1 || observed.ReadyJobs[0] != (ReadyJobObservation{
		JobID: "001-ready", Spec: SpecUnbound, Questions: QuestionsClear,
	}) {
		t.Fatalf("incorrect legacy ready diagnostics: %+v", observed.ReadyJobs)
	}
	proposal := factorycontroller.Proposal{
		Action: factorycontroller.ActionPickupBuild, StateRevision: snapshot.Revision, JobID: target.ID, JobRevision: target.Revision,
	}
	if _, err := factorycontroller.Validate(snapshot, proposal); !errors.Is(err, factorycontroller.ErrOwnerAuthorization) {
		t.Fatalf("legacy record authorized a pickup: %v", err)
	}
	second, err := Load(t.Context(), checkout)
	if err != nil || second.Revision == snapshot.Revision || second.Jobs[0].Revision != target.Revision {
		t.Fatalf("unchanged record did not retain its revision: %+v, %v", second, err)
	}
	if got, err := os.ReadFile(filepath.Join(ready, "job.json")); err != nil || string(got) != readyRecord {
		t.Fatalf("observation changed the legacy record: %q, %v", got, err)
	}
	writeFixture(t, ready, "job.json", readyRecord+"\n")
	third, err := Load(t.Context(), checkout)
	if err != nil || third.Jobs[0].Revision == target.Revision {
		t.Fatalf("changed record retained its revision: %+v, %v", third, err)
	}
}

func TestObserveRefusesInvalidPresentJobRevisions(t *testing.T) {
	for _, revision := range []string{`null`, `0`, `-1`, `true`, `"1"`, `1.5`, `1e0`, `18446744073709551616`, `1,"revision":2`} {
		t.Run(revision, func(t *testing.T) {
			checkout, _ := newCheckout(t)
			writeFixture(t, checkout, "factory/jobs/001-job/job.json",
				`{"id":"001-job","stage":"ready","round":0,"revision":`+revision+`}`)
			observed, err := Observe(t.Context(), checkout)
			if err == nil || observed.Snapshot.Revision != "" {
				t.Fatalf("invalid revision returned a snapshot: %+v, %v", observed, err)
			}
		})
	}
}

func TestObserveRefusesIncompleteOrAmbiguousInventory(t *testing.T) {
	tests := []struct {
		name   string
		change func(t *testing.T, checkout, job string)
	}{
		{"unknown stage", func(t *testing.T, _, job string) {
			writeFixture(t, job, "job.json", `{"id":"001-job","stage":"unknown","round":0,"revision":1}`)
		}},
		{"ambiguous stage", func(t *testing.T, _, job string) {
			writeFixture(t, job, "job.json", `{"id":"001-job","stage":"ready","stage":"build","round":1,"revision":1}`)
		}},
		{"missing round", func(t *testing.T, _, job string) {
			writeFixture(t, job, "job.json", `{"id":"001-job","stage":"build","revision":1}`)
		}},
		{"active job without a round", func(t *testing.T, _, job string) {
			writeFixture(t, job, "job.json", `{"id":"001-job","stage":"merging","round":0,"revision":1}`)
		}},
		{"unknown capacity", func(t *testing.T, checkout, _ string) {
			writeFixture(t, checkout, "factory/config.json", `{"base_branch":"main","lanes":{"clarify":1}}`)
		}},
		{"ambiguous capacity", func(t *testing.T, checkout, _ string) {
			writeFixture(t, checkout, "factory/config.json", `{"base_branch":"main","lanes":{"build":1,"build":2}}`)
		}},
		{"missing base", func(t *testing.T, checkout, _ string) {
			gitFixture(t, checkout, "update-ref", "-d", "refs/remotes/origin/main")
		}},
		{"aliased job", func(t *testing.T, checkout, job string) {
			if err := os.Symlink(job, filepath.Join(checkout, "factory/jobs/002-alias")); err != nil {
				t.Fatal(err)
			}
		}},
		{"aliased state", func(t *testing.T, _, job string) {
			if err := os.Rename(filepath.Join(job, "job.json"), filepath.Join(job, "detached.json")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink("detached.json", filepath.Join(job, "job.json")); err != nil {
				t.Fatal(err)
			}
		}},
		{"special file", func(t *testing.T, _, job string) {
			path := filepath.Join(job, "job.json")
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			if err := syscall.Mkfifo(path, 0600); err != nil {
				t.Fatal(err)
			}
		}},
		{"incomplete admission", func(t *testing.T, checkout, _ string) {
			if err := os.Mkdir(filepath.Join(checkout, "factory/jobs/002-incomplete"), 0700); err != nil {
				t.Fatal(err)
			}
		}},
		{"duplicate issue admission", func(t *testing.T, checkout, _ string) {
			addJob(t, checkout, "1-other-slug", "ready", 0, "")
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			checkout, base := newCheckout(t)
			job := addJob(t, checkout, "001-job", "build", 1, base)
			test.change(t, checkout, job)
			observed, err := Observe(t.Context(), checkout)
			if err == nil || observed.Snapshot.Revision != "" {
				t.Fatalf("ambiguous read returned a snapshot: %+v, %v", observed, err)
			}
		})
	}
}

func TestObserveHonorsFactoryLocksAndInterruptedOperations(t *testing.T) {
	checkout, base := newCheckout(t)
	job := addJob(t, checkout, "001-job", "ready", 0, base)
	for _, directory := range []string{job, filepath.Dir(job)} {
		fd, err := os.Open(directory)
		if err != nil {
			t.Fatal(err)
		}
		if err := syscall.Flock(int(fd.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
			fd.Close()
			t.Fatal(err)
		}
		_, err = Observe(t.Context(), checkout)
		fd.Close()
		if !errors.Is(err, ErrBusy) {
			t.Fatalf("accepted a busy factory directory: %v", err)
		}
	}
	writeFixture(t, job, ".factory-operation.json", "{}")
	if _, err := Observe(t.Context(), checkout); !errors.Is(err, ErrBusy) {
		t.Fatalf("accepted an interrupted write: %v", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := Observe(ctx, checkout); !errors.Is(err, context.Canceled) {
		t.Fatalf("did not honor cancellation: %v", err)
	}
}

func TestObserveReportsStaleSpecAndConflictingRound(t *testing.T) {
	checkout, base := newCheckout(t)
	job := addJob(t, checkout, "001-job", "ready", 0, base)
	writeFixture(t, job, "spec.md", strings.Replace(clearSpec, "None.", "\t*\t[\t] Open question", 1))
	writeFixture(t, job, "worker-spec-writer.json", specAudit(base))
	if err := os.Symlink("missing", filepath.Join(job, "round-1")); err != nil {
		t.Fatal(err)
	}
	gitFixture(t, checkout, "commit", "--allow-empty", "-qm", "second")
	gitFixture(t, checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
	got, err := Observe(t.Context(), checkout)
	if err != nil {
		t.Fatal(err)
	}
	if got.Snapshot.BaseRevision == base || got.Snapshot.Jobs[0].NextRoundAvailable ||
		got.ReadyJobs[0].Spec != SpecBaseMismatch || got.ReadyJobs[0].Questions != QuestionsOpen {
		t.Fatalf("stale evidence was not reported correctly: %+v", got)
	}
}

func TestObserveRefusesStaleCheckout(t *testing.T) {
	checkout, base := newCheckout(t)
	addJob(t, checkout, "001-job", "ready", 0, base)
	gitFixture(t, checkout, "commit", "--allow-empty", "-qm", "different checkout")
	got, err := Observe(t.Context(), checkout)
	if !errors.Is(err, ErrStaleCheckout) || got.Snapshot.Revision != "" {
		t.Fatalf("observed a stale checkout: %+v, %v", got, err)
	}
}

func TestProductQuestionMarkers(t *testing.T) {
	tests := []struct {
		content string
		open    bool
	}{
		{"## Product questions\n- [ ] Open\n", true},
		{"## Product questions\n-[ ] Open\n", true},
		{"## Product questions\r\n*\t[ ] Open\r\n", true},
		{"## Product questions\u2028- [ ] Open", true},
		{"## Product questions\n- [x] Answered", false},
		{"## Product questions\n- [ ]", false},
		{"## Product questions\n- [ ] ", true},
		{"## Owner decisions\nPRODUCT DECISION is recorded\n", false},
		{"## Owner decİsions\nPRODUCT DECISION remains\n", true},
		{"## Owner decisions\nPRODUCT DECISION is recorded\n## Other\nPRODUCT DECISION remains", true},
		{"## Files\n- [ ] Complete implementation", false},
		{"PRODUCT DECISION in preface", true},
	}
	for _, test := range tests {
		if got := hasOpenQuestions(test.content); got != test.open {
			t.Errorf("question status for %q = %v, want %v", test.content, got, test.open)
		}
	}
}

func newCheckout(t *testing.T) (string, string) {
	t.Helper()
	checkout := t.TempDir()
	gitFixture(t, checkout, "init", "--quiet", "--template=", "--initial-branch=main")
	gitFixture(t, checkout, "commit", "--allow-empty", "-qm", "fixture")
	gitFixture(t, checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
	writeFixture(t, checkout, "factory/config.json", `{"base_branch":"main","lanes":{"build":3},"max_rounds":2}`)
	if err := os.Mkdir(filepath.Join(checkout, "factory/jobs"), 0700); err != nil {
		t.Fatal(err)
	}
	return checkout, gitFixture(t, checkout, "rev-parse", "HEAD")
}

func gitFixture(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.CommandContext(t.Context(), "git", append([]string{"-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false"}, args...)...)
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v: %s", args, err, out)
	} else {
		return strings.TrimSpace(string(out))
	}
	return ""
}

func addJob(t *testing.T, checkout, id, stage string, round int, base string) string {
	t.Helper()
	dir := filepath.Join(checkout, "factory/jobs", id)
	writeFixture(t, dir, "job.json", fmt.Sprintf(`{"id":%q,"stage":%q,"round":%d,"revision":7,"spec_base":%q}`, id, stage, round, base))
	return dir
}

func specAudit(base string) string {
	return fmt.Sprintf(`{"role":"spec-writer","input_sha":%q,"base_sha":%q,"candidate_sha":%q,"harness":"claude","model":"opus","at":"2026-11-19T11:00:00Z"}`, base, base, base)
}

func writeFixture(t *testing.T, dir, name, content string) {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}
