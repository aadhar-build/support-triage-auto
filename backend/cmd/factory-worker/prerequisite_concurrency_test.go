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

// A replacement owns its role until the model returns and publication finishes.
// Downstream commands must not consume the artifact left by its previous run.
func TestPrerequisiteCommandsRefuseActiveReplacement(t *testing.T) {
	cases := []struct {
		role, stage, artifact, old, replacement string
		command                                 []string
	}{
		{"intake", "intake", "intake.md", intakeArtifact, strings.Replace(intakeArtifact, "Example", "Replacement", 1), []string{"intake", "001-fixture"}},
		{"spec-writer", "spec", "spec.md", "## Product questions\n- [ ] Old question?\n\n## Files\n- approved.txt\n\n## Acceptance criteria\n- Old.\n", "## Product questions\nNone.\n\n## Files\n- approved.txt\n\n## Acceptance criteria\n- Replacement.\n", []string{"clarify", "001-fixture"}},
		{"builder", "build", "build.md", strings.Replace(validBuild, "Changed", "Old", 1), validBuild, []string{"advance", "001-fixture", "gate"}},
	}
	binary := filepath.Join(t.TempDir(), "factory-worker")
	if out, err := exec.Command("go", "build", "-o", binary, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v: %s", err, out)
	}
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.role, func(t *testing.T) {
			root, job, wt := buildFixture(t)
			gitTest(t, root, "remote", "add", "origin", "https://github.com/fixture/repo.git")
			write(t, filepath.Join(root, "factory", "config.json"), `{"base_branch":"main","protected_paths":["factory/"],"labels":{"needs_info":"needs-info","queue":"queue"},"workers":{"roles":{"`+tc.role+`":{"harness":"codex","model":"gpt-explicit"}}}}`)
			if tc.role == "spec-writer" {
				write(t, filepath.Join(root, ".claude", "agents", "spec-writer.md"), "---\nname: spec-writer\n---\nFixture instructions.\n")
				gitTest(t, root, "add", ".claude/agents/spec-writer.md")
				gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "spec agent")
				gitTest(t, root, "update-ref", "refs/remotes/origin/main", "HEAD")
			}
			state := `{"id":"001-fixture","issue":1,"stage":"` + tc.stage + `","branch":"factory/001-fixture","worktree":".worktrees/001-fixture","round":1,"history":[],"reviewers":{"code":"sonnet"}}`
			write(t, filepath.Join(job, "job.json"), state)
			write(t, filepath.Join(job, tc.artifact), tc.old)
			barrier := filepath.Join(root, "entered")
			release := filepath.Join(root, "release")
			changes := ""
			if tc.role == "builder" {
				changes = "printf 'replacement\\n' > approved.txt\n"
			}
			fakeCodexResponse(t, `touch "`+barrier+`"
i=0
while [ ! -e "`+release+`" ]; do
 i=$((i+1)); [ "$i" -lt 300 ] || exit 90
 sleep 0.05
done
`+changes, tc.replacement)
			worker := exec.Command(binary, "--root", root, "001-fixture", tc.role, "--replace")
			output := new(bytes.Buffer)
			worker.Stdout = output
			worker.Stderr = output
			if err := worker.Start(); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if worker.ProcessState == nil {
					_ = worker.Process.Kill()
					_ = worker.Wait()
				}
			})
			deadline := time.Now().Add(8 * time.Second)
			for {
				if _, err := os.Stat(barrier); err == nil {
					break
				}
				if time.Now().After(deadline) {
					t.Fatalf("model never reached barrier: %s", output)
				}
				time.Sleep(25 * time.Millisecond)
			}
			head := gitTest(t, wt, "rev-parse", "HEAD")
			gitBinary, err := exec.LookPath("git")
			if err != nil {
				t.Fatal(err)
			}
			log := filepath.Join(root, "external-calls")
			fakeCLI(t, "gh", `echo gh >> "`+log+`"; exit 97`)
			fakeCLI(t, "git", `echo "$*" >> "`+log+`"; exec "`+gitBinary+`" "$@"`)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, "python3", append([]string{jobCLI}, tc.command...)...)
			cmd.Dir = root
			commandOut, commandErr := cmd.CombinedOutput()
			if ctx.Err() != nil || commandErr == nil || !strings.Contains(string(commandOut), "worker-") || !strings.Contains(string(commandOut), ".lock") {
				t.Errorf("expected prompt refusal naming role lock; error=%v output=%s", commandErr, commandOut)
			}
			wantFile(t, filepath.Join(job, "job.json"), state)
			wantFile(t, filepath.Join(job, tc.artifact), tc.old)
			if got := gitTest(t, wt, "rev-parse", "HEAD"); got != head {
				t.Errorf("worktree head changed: %s -> %s", head, got)
			}
			if b, err := os.ReadFile(log); err == nil {
				for _, line := range strings.Split(string(b), "\n") {
					if strings.HasPrefix(line, "gh") || strings.HasPrefix(line, "fetch ") || strings.HasPrefix(line, "merge ") {
						t.Errorf("command made external/mutating call: %s", line)
					}
				}
			} else if !os.IsNotExist(err) {
				t.Fatal(err)
			}
			write(t, release, "go")
			if err := worker.Wait(); err != nil {
				t.Fatalf("replacement worker failed: %v: %s", err, output)
			}
			wantFile(t, filepath.Join(job, tc.artifact), tc.replacement)
		})
	}
}

// A failed replacement must not make its old artifact consumable. Retrying the
// replacement clears the refusal only after the new artifact is published.
func TestPrerequisiteCommandsRequireSuccessfulReplacementAfterFailure(t *testing.T) {
	cases := []struct {
		role, stage, artifact, old, replacement string
		command                                 []string
	}{
		{"intake", "intake", "intake.md", intakeArtifact, strings.Replace(intakeArtifact, "Example", "Replacement", 1), []string{"intake", "001-fixture"}},
		{"spec-writer", "spec", "spec.md", "## Product questions\n- [ ] Old question?\n\n## Files\n- approved.txt\n\n## Acceptance criteria\n- Old.\n", "## Product questions\nNone.\n\n## Files\n- approved.txt\n\n## Acceptance criteria\n- Replacement.\n", []string{"clarify", "001-fixture"}},
		{"builder", "build", "build.md", strings.Replace(validBuild, "Changed", "Old", 1), validBuild, []string{"advance", "001-fixture", "gate"}},
	}
	binary := filepath.Join(t.TempDir(), "factory-worker")
	if out, err := exec.Command("go", "build", "-o", binary, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v: %s", err, out)
	}
	jobCLI, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.role, func(t *testing.T) {
			root, job, _ := buildFixture(t)
			gitTest(t, root, "remote", "add", "origin", "https://github.com/fixture/repo.git")
			write(t, filepath.Join(root, "factory", "config.json"), `{"base_branch":"main","protected_paths":["factory/"],"labels":{"needs_info":"needs-info","queue":"queue"},"workers":{"roles":{"`+tc.role+`":{"harness":"codex","model":"gpt-explicit"}}}}`)
			if tc.role == "spec-writer" {
				write(t, filepath.Join(root, ".claude", "agents", "spec-writer.md"), "---\nname: spec-writer\n---\nFixture instructions.\n")
				gitTest(t, root, "add", ".claude/agents/spec-writer.md")
				gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "spec agent")
				gitTest(t, root, "update-ref", "refs/remotes/origin/main", "HEAD")
			}
			state := `{"id":"001-fixture","issue":1,"stage":"` + tc.stage + `","branch":"factory/001-fixture","worktree":".worktrees/001-fixture","round":1,"history":[],"reviewers":{"code":"sonnet"}}`
			write(t, filepath.Join(job, "job.json"), state)
			write(t, filepath.Join(job, tc.artifact), tc.old)
			fakeCodexResponse(t, "exit 51", tc.replacement)
			ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
			defer cancel()
			worker := exec.CommandContext(ctx, binary, "--root", root, "001-fixture", tc.role, "--replace")
			if out, err := worker.CombinedOutput(); err == nil || ctx.Err() != nil {
				t.Fatalf("replacement did not fail as injected: %v %s", err, out)
			}
			wantFile(t, filepath.Join(job, tc.artifact), tc.old)
			gitBinary, err := exec.LookPath("git")
			if err != nil {
				t.Fatal(err)
			}
			calls := filepath.Join(root, "external-calls")
			fakeCLI(t, "gh", `echo gh >> "`+calls+`"; echo '{}'`)
			fakeCLI(t, "git", `case "$*" in "fetch --quiet origin "*) echo fetch >> "`+calls+`"; exit 0 ;; esac
exec "`+gitBinary+`" "$@"`)
			command := func() ([]byte, error) {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				cmd := exec.CommandContext(ctx, "python3", append([]string{jobCLI}, tc.command...)...)
				cmd.Dir = root
				out, err := cmd.CombinedOutput()
				if ctx.Err() != nil {
					t.Fatalf("command timed out: %s", out)
				}
				return out, err
			}
			out, err := command()
			if err == nil || !strings.Contains(string(out), "worker-") || !strings.Contains(string(out), ".replacing") {
				t.Fatalf("failed replacement did not block consumer: %v %s", err, out)
			}
			if _, err := os.Stat(calls); !os.IsNotExist(err) {
				t.Fatalf("consumer made external call after failure")
			}
			wantFile(t, filepath.Join(job, "job.json"), state)
			changes := ""
			if tc.role == "builder" {
				changes = "printf 'replacement\\n' > approved.txt\n"
			}
			fakeCodexResponse(t, changes, tc.replacement)
			retryCtx, retryCancel := context.WithTimeout(context.Background(), 12*time.Second)
			defer retryCancel()
			retry := exec.CommandContext(retryCtx, binary, "--root", root, "001-fixture", tc.role, "--replace")
			if out, err := retry.CombinedOutput(); err != nil {
				t.Fatalf("replacement retry failed: %v %s", err, out)
			}
			wantFile(t, filepath.Join(job, tc.artifact), tc.replacement)
			if out, err := command(); err != nil {
				t.Fatalf("consumer remained blocked after successful replacement: %v %s", err, out)
			}
			if got := read(t, filepath.Join(job, "job.json")); got == state {
				t.Fatal("consumer did not progress the job")
			}
		})
	}
}
