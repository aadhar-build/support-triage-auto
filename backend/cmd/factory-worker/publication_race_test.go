package main

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The git shim orders a separate Python transition inside the last publication
// window, after builder validation and before the actual worktree fast-forward.
func TestBuilderPublicationAndStageTransitionAreMutuallyExclusive(t *testing.T) {
	root, job, wt := buildFixture(t)
	jobPath := filepath.Join(job, "job.json")
	write(t, jobPath, strings.Replace(read(t, jobPath), `"round":1`, `"round":1,"history":[]`, 1))
	input := gitTest(t, wt, "rev-parse", "HEAD")
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	gitBinary, err := exec.LookPath("git")
	if err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(root, "attempted-transition")
	fakeCLI(t, "git", `
case " $* " in
  *" merge --ff-only "*)
    if (cd "`+root+`" && python3 "`+jobCLI+`" advance 001-fixture gate) > "`+marker+`" 2>&1; then
      echo admitted >> "`+marker+`"
    fi
    ;;
esac
exec "`+gitBinary+`" "$@"
`)
	fakeCodexResponse(t, "echo candidate > approved.txt", validBuild)
	workerErr := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
	observed, err := os.ReadFile(marker)
	if err != nil {
		t.Fatalf("transition barrier was not exercised: %v; worker: %v", err, workerErr)
	}
	admitted := strings.Contains(string(observed), "\nadmitted\n")
	head := gitTest(t, wt, "rev-parse", "HEAD")
	if admitted && (workerErr == nil || head != input) {
		t.Fatalf("Python admitted build -> gate before fast-forward; worker published=%t, branch advanced=%t", workerErr == nil, head != input)
	}
	if !admitted && (workerErr != nil || head == input) {
		t.Fatalf("transition refused but publication failed: %v; transition output %s", workerErr, observed)
	}
}

// An interrupted commit leaves a journal bound to the exact job.json bytes.
// Even a scorecard update must be safely deferred until recovery has finished.
func TestInterruptedPublicationSurvivesConcurrentRecord(t *testing.T) {
	root, job, wt := buildFixture(t)
	artifact := filepath.Join(job, "build.md")
	if err := os.Mkdir(artifact, 0700); err != nil {
		t.Fatal(err)
	}
	fakeCodexResponse(t, "echo candidate > approved.txt", validBuild)
	firstErr := run(root, []string{"001-fixture", "builder", "--replace"}, &bytes.Buffer{})
	if firstErr == nil || !strings.Contains(firstErr.Error(), "publication is pending") {
		t.Fatalf("expected interrupted publication, got %v", firstErr)
	}
	candidate := gitTest(t, wt, "rev-parse", "HEAD")
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("python3", jobCLI, "record", "001-fixture", "owner_minutes", "9")
	cmd.Dir = root
	logged, recordErr := cmd.CombinedOutput()
	if err := os.Remove(artifact); err != nil {
		t.Fatal(err)
	}
	fakeCLI(t, "codex", "exit 99")
	recoverErr := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{})
	if recoverErr != nil {
		t.Fatalf("same-candidate recovery failed after record (err %v, output %q): %v", recordErr, logged, recoverErr)
	}
	if got := gitTest(t, wt, "rev-parse", "HEAD"); got != candidate {
		t.Fatal("recovery changed HEAD")
	}
	wantFile(t, artifact, validBuild)
	noFile(t, filepath.Join(job, "worker-round-1-builder.json.pending"))
}

// A Python process holds the real directory mutex (without creating a marker).
// The Go worker must contend on the same inode before invoking its fake model.
func TestPythonAndGoUseTheSameAdmissionMutex(t *testing.T) {
	root, job := fixture(t, `,"workers":{"roles":{"intake":{"harness":"codex","model":"gpt-explicit"}}}`)
	fakeCodexResponse(t, "", intakeArtifact)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "python3", "-u", "-c", `
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import factory as F
with F.job_mutex(Path(sys.argv[2])):
    print("locked", flush=True)
    sys.stdin.readline()
`, "../../../factory/lib", job)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	output := bufio.NewScanner(stdout)
	if !output.Scan() || output.Text() != "locked" {
		stdin.Close()
		t.Fatalf("Python mutex barrier: %v, %s", cmd.Wait(), stderr.String())
	}
	workerErr := run(root, []string{"001-fixture", "intake"}, &bytes.Buffer{})
	stdin.Close()
	if err := cmd.Wait(); err != nil {
		t.Fatalf("Python mutex holder: %v %s", err, stderr.String())
	}
	if workerErr == nil || !strings.Contains(workerErr.Error(), "busy") {
		t.Fatalf("Go ignored Python flock: %v", workerErr)
	}
	noFile(t, filepath.Join(job, "worker-intake.json"))
	if err := run(root, []string{"001-fixture", "intake"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	wantFile(t, filepath.Join(job, "intake.md"), intakeArtifact)
}

// Update the job scorecard during candidate preparation after the model returns, before
// the builder has created its durable pending journal.
func TestBuilderDropsPreparedCandidateAfterPythonStateUpdate(t *testing.T) {
	root, job, wt := buildFixture(t)
	jobPath := filepath.Join(job, "job.json")
	write(t, jobPath, strings.Replace(read(t, jobPath), `"round":1`, `"round":1,"history":[]`, 1))
	input := gitTest(t, wt, "rev-parse", "HEAD")
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	binary, err := exec.LookPath("git")
	if err != nil {
		t.Fatal(err)
	}
	fakeCLI(t, "git", `
case " $* " in *" fetch --quiet --no-tags --no-write-fetch-head -- "*)
    (cd "`+root+`" && python3 "`+jobCLI+`" record 001-fixture owner_minutes 9) >/dev/null
    ;;
esac
exec "`+binary+`" "$@"
`)
	fakeCodexResponse(t, "echo candidate > approved.txt", validBuild)
	err = run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "state changed") {
		t.Fatalf("prepared candidate survived a real Python state update: %v", err)
	}
	noFile(t, filepath.Join(job, "build.md"))
	noFile(t, filepath.Join(job, "worker-round-1-builder.json.pending"))
	if gitTest(t, wt, "rev-parse", "HEAD") != input {
		t.Fatal("stale publication advanced the branch")
	}
}
