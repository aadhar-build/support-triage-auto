package main

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Independent reviewer processes must both enter the model before either exits.
func TestIndependentReviewProcessesOverlapAndBlockCompetingOperations(t *testing.T) {
	root, round, _ := claudeReviewFixture(t)
	job := filepath.Dir(round)
	state := read(t, filepath.Join(job, "job.json"))
	write(t, filepath.Join(job, "job.json"), strings.Replace(state, `"code":"sonnet"`, `"code":"sonnet","eval":"sonnet"`, 1))
	write(t, filepath.Join(root, ".claude", "agents", "reviewer-eval.md"), "---\nname: reviewer-eval\n---\nFixture instructions.\n")
	barrier := filepath.Join(root, "barrier")
	if err := os.Mkdir(barrier, 0700); err != nil {
		t.Fatal(err)
	}
	fakeClaudeResult(t, `touch "`+barrier+`/entered-$$"
i=0
while [ ! -e "`+barrier+`/release" ]; do
 i=$((i+1)); [ "$i" -lt 200 ] || exit 90
 sleep 0.05
done`, "VERDICT: CHANGES\n\n## Summary\n- Fixture.\n\n## Problems\n- Needs work.\n", `,"permission_denials":[]`)
	binary := filepath.Join(t.TempDir(), "factory-worker")
	build := exec.Command("go", "build", "-o", binary, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v: %s", err, out)
	}
	start := func(role string) (*exec.Cmd, *bytes.Buffer) {
		cmd := exec.Command(binary, "--root", root, "001-fixture", role)
		out := new(bytes.Buffer)
		cmd.Stdout = out
		cmd.Stderr = out
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if cmd.ProcessState == nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			}
		})
		return cmd, out
	}
	code, codeOut := start("reviewer-code")
	eval, evalOut := start("reviewer-eval")
	deadline := time.Now().Add(8 * time.Second)
	for {
		matches, _ := filepath.Glob(filepath.Join(barrier, "entered-*"))
		if len(matches) == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("models did not overlap: %d entered; code=%s eval=%s", len(matches), codeOut, evalOut)
		}
		time.Sleep(25 * time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	replacement := exec.CommandContext(ctx, binary, "--root", root, "001-fixture", "reviewer-code", "--replace")
	if out, err := replacement.CombinedOutput(); err == nil || ctx.Err() != nil {
		t.Fatalf("replacement did not fail promptly: %v: %s", err, out)
	}
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	tallyCtx, tallyCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer tallyCancel()
	tally := exec.CommandContext(tallyCtx, "python3", jobCLI, "tally", "001-fixture")
	tally.Dir = root
	if out, err := tally.CombinedOutput(); err == nil || tallyCtx.Err() != nil {
		t.Fatalf("tally did not fail promptly: %v: %s", err, out)
	}
	write(t, filepath.Join(barrier, "release"), "go")
	for _, p := range []struct {
		cmd *exec.Cmd
		out *bytes.Buffer
	}{{code, codeOut}, {eval, evalOut}} {
		if err := p.cmd.Wait(); err != nil {
			t.Errorf("review failed: %v: %s", err, p.out)
		}
	}
	for _, role := range []string{"code", "eval"} {
		if _, err := os.Stat(filepath.Join(round, "review-"+role+".md")); err != nil {
			t.Errorf("missing %s review: %v", role, err)
		}
	}
}
