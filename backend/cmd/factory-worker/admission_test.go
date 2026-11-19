package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestWorkerAdmissionBlocksActiveOrInterruptedMutation(t *testing.T) {
	for _, blocker := range []string{"directory lock", ".factory-operation.json", ".gate-publication.pending", "worker-round-0-builder.json.pending"} {
		t.Run(blocker, func(t *testing.T) {
			root, job := fixture(t, `,"workers":{"roles":{"intake":{"harness":"codex","model":"gpt-explicit"}}}`)
			fakeCodexResponse(t, "", intakeArtifact)
			if blocker == "directory lock" {
				dir, err := os.Open(job)
				if err != nil {
					t.Fatal(err)
				}
				defer dir.Close()
				if err := syscall.Flock(int(dir.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
					t.Fatal(err)
				}
			} else {
				write(t, filepath.Join(job, blocker), "{}")
			}
			reason := blocker
			if blocker == "directory lock" {
				reason = "busy"
			}
			if err := run(root, []string{"001-fixture", "intake"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), reason) {
				t.Fatalf("wanted a refusal for %s, got %v", blocker, err)
			}
			noFile(t, filepath.Join(job, "intake.md"))
			noFile(t, filepath.Join(job, "worker-intake.json"))
			noFile(t, filepath.Join(job, "worker-intake.json.lock"))
		})
	}
}

func TestWorkerPublicationBlocksMutationAdmittedDuringModel(t *testing.T) {
	root, job := fixture(t, `,"workers":{"roles":{"intake":{"harness":"codex","model":"gpt-explicit"}}}`)
	marker := filepath.Join(job, ".factory-operation.json")
	fakeCodexResponse(t, `printf '{}'> "`+marker+`"`, intakeArtifact)
	if err := run(root, []string{"001-fixture", "intake"}, &bytes.Buffer{}); err == nil {
		t.Fatal("worker published during a job mutation")
	}
	wantFile(t, marker, "{}")
	noFile(t, filepath.Join(job, "intake.md"))
	noFile(t, filepath.Join(job, "worker-intake.json"))
}

func TestBuilderCannotPublishAfterSpecChangesDuringModel(t *testing.T) {
	root, job, wt := buildFixture(t)
	spec := filepath.Join(job, "spec.md")
	head := gitTest(t, wt, "rev-parse", "HEAD")
	fakeCodexResponse(t, `echo candidate > approved.txt; printf '\nNew requirement\n' >> "`+spec+`"`, validBuild)
	err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "spec.md") {
		t.Fatalf("wanted stale spec refusal, got %v", err)
	}
	if gitTest(t, wt, "rev-parse", "HEAD") != head {
		t.Fatal("worktree advanced after the spec changed")
	}
	noFile(t, filepath.Join(job, "build.md"))
	noFile(t, filepath.Join(job, "worker-round-1-builder.json.pending"))
}

func TestBuilderNoChangeSidecarBlocksMutationAdmittedDuringModel(t *testing.T) {
	root, job, _ := buildFixture(t)
	marker := filepath.Join(job, ".gate-publication.pending")
	fakeCodexResponse(t, `printf '{}'> "`+marker+`"`, validBuild)
	err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), filepath.Base(marker)) {
		t.Fatalf("wanted pending gate refusal, got %v", err)
	}
	noFile(t, filepath.Join(job, "worker-round-1-builder.json.failure"))
}

func TestBuilderRecoveryRefusesConcurrentMutationOrUnboundSpec(t *testing.T) {
	root, job, wt := buildFixture(t)
	artifact := filepath.Join(job, "build.md")
	if err := os.Mkdir(artifact, 0700); err != nil {
		t.Fatal(err)
	}
	fakeCodexResponse(t, "echo candidate > approved.txt", validBuild)
	if err := run(root, []string{"001-fixture", "builder", "--replace"}, &bytes.Buffer{}); err == nil {
		t.Fatal("expected the artifact directory to interrupt publication")
	}
	if err := os.Remove(artifact); err != nil {
		t.Fatal(err)
	}
	candidate := gitTest(t, wt, "rev-parse", "HEAD")
	pending := filepath.Join(job, "worker-round-1-builder.json.pending")
	journal := read(t, pending)
	replacement := filepath.Join(job, "worker-round-1-builder.json.replacing")
	intent := read(t, replacement)
	marker := filepath.Join(job, ".factory-operation.json")
	write(t, marker, "{}")

	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), filepath.Base(marker)) {
		t.Fatalf("wanted pending operation refusal, got %v", err)
	}
	wantFile(t, pending, journal)
	noFile(t, artifact)
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}

	specPath := filepath.Join(job, "spec.md")
	spec := read(t, specPath)
	write(t, specPath, spec+"\nUpdated acceptance criteria.\n")
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "spec.md") {
		t.Fatalf("wanted stale spec refusal, got %v", err)
	}
	wantFile(t, pending, journal)
	noFile(t, artifact)
	write(t, specPath, spec)

	var legacyFields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(journal), &legacyFields); err != nil {
		t.Fatal(err)
	}
	delete(legacyFields, "SpecSHA256")
	encoded, err := json.Marshal(legacyFields)
	if err != nil {
		t.Fatal(err)
	}
	legacy := string(encoded)
	write(t, pending, legacy)
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "legacy builder publication") {
		t.Fatalf("wanted legacy journal refusal, got %v", err)
	}
	wantFile(t, pending, legacy)
	wantFile(t, replacement, intent)
	noFile(t, artifact)

	write(t, pending, journal)
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	wantFile(t, artifact, validBuild)
	noFile(t, pending)
	noFile(t, replacement)
	if gitTest(t, wt, "rev-parse", "HEAD") != candidate {
		t.Fatal("recovery changed the candidate")
	}
}

func TestFailedReplacementRequiresExplicitRetry(t *testing.T) {
	const validSpec = "## Product questions\nNone.\n\n## Files\n- approved.txt\n\n## Acceptance criteria\n- Example.\n"
	for _, tc := range []struct {
		role, stage, artifact, audit, result, changes string
	}{
		{"intake", "intake", "intake.md", "worker-intake.json", intakeArtifact, ""},
		{"spec-writer", "spec", "spec.md", "worker-spec-writer.json", validSpec, ""},
		{"builder", "build", "build.md", "worker-round-1-builder.json", validBuild, "echo candidate > approved.txt"},
	} {
		t.Run(tc.role, func(t *testing.T) {
			root, job, _ := buildFixture(t)
			state := filepath.Join(job, "job.json")
			write(t, state, strings.Replace(read(t, state), `"stage":"build"`, `"stage":"`+tc.stage+`"`, 1))
			write(t, filepath.Join(root, ".claude", "agents", tc.role+".md"), "---\nname: "+tc.role+"\n---\nFixture instructions.\n")
			write(t, filepath.Join(root, "factory", "config.json"), `{"base_branch":"main","protected_paths":["factory/"],"workers":{"roles":{"`+tc.role+`":{"harness":"codex","model":"gpt-explicit"}}}}`)
			artifact := filepath.Join(job, tc.artifact)
			audit := filepath.Join(job, tc.audit)
			write(t, artifact, "previous artifact")
			write(t, audit, "{}")
			fakeCodexResponse(t, "exit 99", tc.result)
			if err := run(root, []string{"001-fixture", tc.role, "--replace"}, &bytes.Buffer{}); err == nil {
				t.Fatal("expected the replacement model to fail")
			}
			marker := audit + ".replacing"
			if _, err := os.Stat(marker); err != nil {
				t.Fatalf("failed replacement lost its intent: %v", err)
			}
			wantFile(t, artifact, "previous artifact")
			wantFile(t, audit, "{}")
			noFile(t, audit+".lock")
			noFile(t, audit+".pending")

			fakeCodexResponse(t, tc.changes, tc.result)
			err := run(root, []string{"001-fixture", tc.role}, &bytes.Buffer{})
			if err == nil || !strings.Contains(err.Error(), "replacement is pending") {
				t.Fatalf("wanted an explicit replacement retry, got %v", err)
			}
			if err := run(root, []string{"001-fixture", tc.role, "--replace"}, &bytes.Buffer{}); err != nil {
				t.Fatal(err)
			}
			wantFile(t, artifact, tc.result)
			noFile(t, marker)
		})
	}
}
