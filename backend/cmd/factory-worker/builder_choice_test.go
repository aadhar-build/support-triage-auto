package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJobBuilderPinsSeparateNativeWorkersWithoutChangingConfig(t *testing.T) {
	root, codexJob, codexTree := buildFixture(t)
	configPath := filepath.Join(root, "factory", "config.json")
	configured := strings.Replace(read(t, configPath), `"workers":{"roles":`,
		`"workers":{"builder_models":{"claude":["opus"],"codex":["gpt-choice"]},"roles":`, 1)
	write(t, configPath, configured)
	state := read(t, filepath.Join(codexJob, "job.json"))
	write(t, filepath.Join(codexJob, "job.json"), strings.Replace(state, `"stage":`, `"builder":{"harness":"codex","model":"gpt-choice"},"stage":`, 1))
	claudeID := "002-fixture"
	claudeJob := filepath.Join(root, "factory", "jobs", claudeID)
	if err := os.Mkdir(claudeJob, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"issue.md", "intake.md", "spec.md"} {
		write(t, filepath.Join(claudeJob, name), read(t, filepath.Join(codexJob, name)))
	}
	claudeTree := filepath.Join(root, ".worktrees", claudeID)
	gitTest(t, root, "worktree", "add", "-qb", "factory/"+claudeID, claudeTree, "HEAD")
	write(t, filepath.Join(claudeJob, "job.json"), strings.ReplaceAll(strings.Replace(state, `"stage":`, `"builder":{"harness":"claude","model":"opus"},"stage":`, 1), "001-fixture", claudeID))
	fakeCodexResponse(t, `case " $* " in *" --model gpt-choice "*) ;; *) exit 12 ;; esac
printf 'codex fixture\n' > approved.txt`, validBuild)
	fakeClaudeResult(t, `case " $* " in *" --model opus "*) ;; *) exit 13 ;; esac
printf 'claude fixture\n' > approved.txt`, validBuild, `,"permission_denials":[]`)

	builders := []struct{ id, job, tree, harness, model string }{
		{"001-fixture", codexJob, codexTree, "codex", "gpt-choice"},
		{claudeID, claudeJob, claudeTree, "claude", "opus"},
	}
	states := make(map[string]string, len(builders))
	results := make(chan error, len(builders))
	for _, builder := range builders {
		before := read(t, filepath.Join(builder.job, "job.json"))
		states[builder.id] = before
		go func() {
			results <- run(root, []string{builder.id, "builder"}, &bytes.Buffer{})
		}()
	}
	for range builders {
		if err := <-results; err != nil {
			t.Error(err)
		}
	}
	if t.Failed() {
		return
	}
	for _, builder := range builders {
		wantFile(t, filepath.Join(builder.job, "job.json"), states[builder.id])
		wantFile(t, filepath.Join(builder.tree, "approved.txt"), builder.harness+" fixture\n")
		var audit struct {
			selection
			Candidate string `json:"candidate_sha"`
		}
		if err := readJSON(filepath.Join(builder.job, "worker-round-1-builder.json"), &audit); err != nil {
			t.Fatal(err)
		}
		if audit.selection != (selection{Harness: builder.harness, Model: builder.model}) ||
			audit.Candidate != gitTest(t, builder.tree, "rev-parse", "HEAD") {
			t.Fatalf("builder audit did not record the pinned provider, model and candidate: %+v", audit)
		}
	}
	wantFile(t, configPath, configured)
}

func TestJobBuilderRefusesMalformedOrUnlistedSelectionBeforeDispatch(t *testing.T) {
	root, job, _ := buildFixture(t)
	configPath := filepath.Join(root, "factory", "config.json")
	write(t, configPath, strings.Replace(read(t, configPath), `"workers":{"roles":`,
		`"workers":{"builder_models":{"codex":["gpt-choice"]},"roles":`, 1))
	state := read(t, filepath.Join(job, "job.json"))
	marker := filepath.Join(t.TempDir(), "invoked")
	fakeCLI(t, "codex", "touch '"+marker+"'\nexit 13\n")
	for _, choice := range []string{`null`, `{}`, `{"harness":"claude","model":"opus"}`,
		`{"harness":"codex","model":"other"}`, `{"harness":"codex","model":"gpt-choice","effort":"high"}`} {
		t.Run(choice, func(t *testing.T) {
			write(t, filepath.Join(job, "job.json"), strings.Replace(state, `"stage":`, `"builder":`+choice+`,"stage":`, 1))
			if err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "job builder") {
				t.Fatalf("expected a job builder refusal, got %v", err)
			}
			noFile(t, marker)
			noFile(t, filepath.Join(job, "worker-round-1-builder.json"))
		})
	}
}

func TestJobBuilderRecoversRecordedSelectionAfterAllowlistChange(t *testing.T) {
	root, job, _ := buildFixture(t)
	configPath := filepath.Join(root, "factory", "config.json")
	defaultConfig := read(t, configPath)
	write(t, configPath, strings.Replace(defaultConfig, `"workers":{"roles":`,
		`"workers":{"builder_models":{"codex":["gpt-choice"]},"roles":`, 1))
	statePath := filepath.Join(job, "job.json")
	write(t, statePath, strings.Replace(read(t, statePath), `"stage":`,
		`"builder":{"harness":"codex","model":"gpt-choice"},"stage":`, 1))
	artifact := filepath.Join(job, "build.md")
	if err := os.Mkdir(artifact, 0700); err != nil {
		t.Fatal(err)
	}
	fakeCodexResponse(t, "printf 'after\\n' > approved.txt", validBuild)
	if err := run(root, []string{"001-fixture", "builder", "--replace"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "publication is pending") {
		t.Fatalf("expected interrupted publication, got %v", err)
	}
	if err := os.Remove(artifact); err != nil {
		t.Fatal(err)
	}
	pending := filepath.Join(job, "worker-round-1-builder.json.pending")
	journal := read(t, pending)
	var changed publication
	if err := json.Unmarshal([]byte(journal), &changed); err != nil {
		t.Fatal(err)
	}
	changed.Audit = bytes.ReplaceAll(changed.Audit, []byte("gpt-choice"), []byte("another-model"))
	tampered, err := json.Marshal(changed)
	if err != nil {
		t.Fatal(err)
	}
	write(t, pending, string(tampered))
	write(t, configPath, defaultConfig)
	fakeCLI(t, "codex", "exit 12")
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "audit does not match the pinned job builder") {
		t.Fatalf("expected recovery to refuse the mismatched audit, got %v", err)
	}
	wantFile(t, pending, string(tampered))
	noFile(t, artifact)

	write(t, pending, journal)
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	wantFile(t, artifact, validBuild)
	noFile(t, pending)
}
