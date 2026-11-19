package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestStageEvidenceRequiresGateBoundToBaseAndExactSpecBytes(t *testing.T) {
	const spec = "## Files\r\n- approved.txt\r\n"
	const specSHA = "23644c57bda8e4ebaa3101a42f625f34185c870c20fa3c1f53f1bba6099fef02"
	head := strings.Repeat("a", 40)
	base := strings.Repeat("b", 40)
	gate := "GATE: PASS\nCANDIDATE: " + head + "\n"
	baseLine := "BASE: " + base + "\n"
	specLine := "SPEC-DIGEST: " + specSHA + "\n"
	fullGate := gate + baseLine + specLine + "\n## Summary\n"
	for _, test := range []struct {
		name, receipt, currentBase, currentSpec string
		wantPass                                bool
	}{
		{name: "current gate", receipt: fullGate, currentBase: base, currentSpec: spec, wantPass: true},
		{name: "legacy gate", receipt: gate, currentBase: base, currentSpec: spec},
		{name: "missing base", receipt: gate + specLine, currentBase: base, currentSpec: spec},
		{name: "missing spec digest", receipt: gate + baseLine, currentBase: base, currentSpec: spec},
		{name: "base moved", receipt: fullGate, currentBase: strings.Repeat("c", 40), currentSpec: spec},
		{name: "spec line endings changed", receipt: fullGate, currentBase: base, currentSpec: strings.ReplaceAll(spec, "\r\n", "\n")},
		{name: "spec disappeared", receipt: fullGate, currentBase: base},
		{name: "headers in body", receipt: gate + "\n" + baseLine + specLine, currentBase: base, currentSpec: spec},
		{name: "conflicting base header", receipt: gate + baseLine + specLine + "BASE: " + strings.Repeat("c", 40) + "\n", currentBase: base, currentSpec: spec},
	} {
		t.Run(test.name, func(t *testing.T) {
			job := t.TempDir()
			round := filepath.Join(job, "round-1")
			if err := os.Mkdir(round, 0700); err != nil {
				t.Fatal(err)
			}
			write(t, filepath.Join(round, "checks.md"), test.receipt)
			if test.currentSpec != "" {
				write(t, filepath.Join(job, "spec.md"), test.currentSpec)
			}
			state := jobState{ID: "001-fixture", Stage: "review", Round: 1}
			err := validateStageEvidence(context.Background(), job, state, head, test.currentBase)
			if test.wantPass {
				if err != nil {
					t.Fatalf("rejected the current gate: %v", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), "job re-review 001-fixture") {
				t.Fatalf("expected an invalid gate with recovery instructions, got %v", err)
			}
		})
	}
}

func TestStageEvidenceRejectsMissingGateCandidateAndReviewAudit(t *testing.T) {
	job := t.TempDir()
	round := filepath.Join(job, "round-1")
	if err := os.Mkdir(round, 0700); err != nil {
		t.Fatal(err)
	}
	head := strings.Repeat("a", 40)
	state := jobState{Stage: "review", Round: 1, Reviewers: map[string]string{"code": "sonnet"}}
	write(t, filepath.Join(round, "checks.md"), "GATE: PASS\n")
	if err := validateStageEvidence(context.Background(), job, state, head, head); err == nil {
		t.Fatal("accepted gate without candidate SHA")
	}
	spec := "## Files\n- approved.txt\n"
	write(t, filepath.Join(job, "spec.md"), spec)
	write(t, filepath.Join(round, "checks.md"), fmt.Sprintf("GATE: PASS\nCANDIDATE: %s\nBASE: %s\nSPEC-DIGEST: %x\n", head, head, sha256.Sum256([]byte(spec))))
	write(t, filepath.Join(round, "review-code.md"), "VERDICT: PASS\n")
	state.Stage = "approve"
	if err := validateStageEvidence(context.Background(), job, state, head, head); err == nil || !strings.Contains(err.Error(), "audit does not match") {
		t.Fatalf("expected to reject a passing review with no audit, got %v", err)
	}
}

func TestApprovalRejectsReviewModifiedAfterPublication(t *testing.T) {
	root, job, wt := buildFixture(t)
	fakeCodexResponse(t, "printf 'after\\n' > approved.txt", validBuild)
	if err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	head := gitTest(t, wt, "rev-parse", "HEAD")
	round := filepath.Join(job, "round-1")
	if err := os.Mkdir(round, 0700); err != nil {
		t.Fatal(err)
	}
	writeFixtureGate(t, root, job, head)
	state := read(t, filepath.Join(job, "job.json"))
	write(t, filepath.Join(job, "job.json"), strings.Replace(state, `"stage":"build"`, `"stage":"review"`, 1))
	seedFixtureReview(t, root, job, head)
	write(t, filepath.Join(round, "review-code.md"), "VERDICT: PASS\n\n## Summary\nTampered review.\n")
	write(t, filepath.Join(job, "job.json"), strings.Replace(state, `"stage":"build"`, `"stage":"approve"`, 1))
	fakeCodexResponse(t, "", "ESCALATE\n\n## Summary\nNeeds review.\n\n## For the human\nCheck the candidate.\n")
	if err := run(root, []string{"001-fixture", "approver"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "audit does not match") {
		t.Fatalf("expected to reject the changed review digest, got %v", err)
	}
	noFile(t, filepath.Join(job, "decision.md"))
}

func TestJobIdentityDigestMatchesJobCommand(t *testing.T) {
	state := jobState{
		ID: "001-fixture", Branch: "factory/001-fixture", Worktree: ".worktrees/001-fixture",
		Round: 1, PR: 42, StackedOnPR: 17, Risk: "high", Surfaces: []string{"security", "eval"},
		Reviewers: map[string]string{"eval": "opus", "code": "sonnet"},
	}
	if got := jobIdentityDigest(state); got != "ce867e0bca1ffc227424063bb6babff56b71e9b3cb246112843d1b67cbdb933f" {
		t.Fatalf("unexpected job identity digest %s", got)
	}
}

func TestFailedReviewReplacementLeavesOldPassBlocked(t *testing.T) {
	root, round, head := claudeReviewFixture(t)
	artifact := filepath.Join(round, "review-code.md")
	audit := filepath.Join(round, "worker-reviewer-code.json")
	old := seedFixtureReview(t, root, filepath.Dir(round), head)
	fakeClaudeResult(t, "", old, `,"permission_denials":[{"tool_name":"Read"}]`)
	if err := run(root, []string{"001-fixture", "reviewer-code", "--replace"}, &bytes.Buffer{}); err == nil {
		t.Fatal("replacement succeeded despite a permission denial")
	}
	wantFile(t, artifact, old)
	if _, err := os.Stat(audit + ".pending"); err != nil {
		t.Fatalf("old PASS remains eligible for tally after a failed replacement: %v", err)
	}
	cmd := exec.Command("python3", "-B", "-c", `
import runpy, sys
from pathlib import Path
job = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
try:
    job["cmd_tally"](root, job["F"].load_config(root), ["001-fixture"])
except job["F"].FactoryError as exc:
    if "interrupted worker publication" in str(exc):
        sys.exit(0)
    raise
sys.exit("tally did not refuse replacement")
`, "../../../factory/bin/job", root)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("tally did not block the failed replacement: %v\n%s", err, out)
	}
	replacement := "VERDICT: CHANGES\n\n## Summary\nNeeds work.\n\n## Problems\n- Update the fixture.\n"
	fakeClaudeResult(t, "", replacement, `,"permission_denials":[]`)
	if err := run(root, []string{"001-fixture", "reviewer-code", "--replace"}, &bytes.Buffer{}); err != nil {
		t.Fatalf("could not retry replacement: %v", err)
	}
	wantFile(t, artifact, replacement)
	noFile(t, audit+".pending")
}

func TestManifestRejectsNewOwnerFeedback(t *testing.T) {
	root, round, _ := claudeReviewFixture(t)
	job, err := filepath.EvalSymlinks(filepath.Dir(round))
	if err != nil {
		t.Fatal(err)
	}
	var state jobState
	if err := readJSON(filepath.Join(job, "job.json"), &state); err != nil {
		t.Fatal(err)
	}
	inputs, err := evidenceInputs(context.Background(), job, state, roles["reviewer-code"])
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyEvidenceInputs(context.Background(), job, state, roles["reviewer-code"], inputs); err != nil {
		t.Fatalf("unchanged manifest refused: %v", err)
	}
	write(t, filepath.Join(job, "feedback-owner.md"), "New requirements.\n")
	if err := verifyEvidenceInputs(context.Background(), job, state, roles["reviewer-code"], inputs); err == nil {
		t.Fatalf("owner feedback added after snapshot went undetected in %s", filepath.Base(root))
	}
}

func TestStageEvidenceRejectsGateThroughSymlinkedRound(t *testing.T) {
	job := t.TempDir()
	target := t.TempDir()
	head := strings.Repeat("a", 40)
	spec := "## Files\n- approved.txt\n"
	write(t, filepath.Join(job, "spec.md"), spec)
	write(t, filepath.Join(target, "checks.md"), fmt.Sprintf("GATE: PASS\nCANDIDATE: %s\nBASE: %s\nSPEC-DIGEST: %x\n", head, head, sha256.Sum256([]byte(spec))))
	if err := os.Symlink(target, filepath.Join(job, "round-1")); err != nil {
		t.Fatal(err)
	}
	if err := validateStageEvidence(context.Background(), job, jobState{Stage: "review", Round: 1}, head, head); err == nil {
		t.Fatal("live validation accepted a gate reached through a symlinked round")
	}
}

func TestLiveEvidenceRejectsFIFO(t *testing.T) {
	job := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(job, "job.json"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readLiveEvidence(job, "job.json", maxJobInputBytes); err == nil {
		t.Fatal("accepted a FIFO as job evidence")
	}
}

func writeFixtureGate(t *testing.T, root, job, candidate string) {
	t.Helper()
	base := gitTest(t, root, "rev-parse", "refs/remotes/origin/main")
	spec := read(t, filepath.Join(job, "spec.md"))
	receipt := fmt.Sprintf("GATE: PASS\nCANDIDATE: %s\nBASE: %s\nSPEC-DIGEST: %x\n\n## Summary\n", candidate, base, sha256.Sum256([]byte(spec)))
	write(t, filepath.Join(job, "round-1", "checks.md"), receipt)
}

func seedFixtureReview(t *testing.T, root, job, head string) string {
	t.Helper()
	var state jobState
	if err := readJSON(filepath.Join(job, "job.json"), &state); err != nil {
		t.Fatal(err)
	}
	inputs, err := evidenceInputs(context.Background(), job, state, roles["reviewer-code"])
	if err != nil {
		t.Fatal(err)
	}
	result := "VERDICT: PASS\n\n## Summary\n- Local fixture review.\n"
	doc := evidenceRecord{EvidenceVersion: reviewEvidenceVersion, Role: "reviewer-code", CandidateSHA: head,
		BaseSHA: gitTest(t, root, "rev-parse", "refs/remotes/origin/main"), Inputs: inputs, ArtifactSHA256: contentDigest([]byte(result))}
	data, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	round := filepath.Join(job, "round-1")
	write(t, filepath.Join(round, "review-code.md"), result)
	write(t, filepath.Join(round, "worker-reviewer-code.json"), string(data))
	return result
}
