package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestCodexBuilderKeepsOperatorToolchainPath(t *testing.T) {
	for _, tc := range []struct {
		name, toolVersion, wantError string
		module, installed, home      bool
		repositoryInstall            bool
	}{
		{name: "Go 1.26", module: true, installed: true, toolVersion: "go1.26.0"},
		{name: "newer Go", module: true, installed: true, toolVersion: "go1.27.1"},
		{name: "older Go", module: true, installed: true, toolVersion: "go1.25.7", wantError: "Go 1.26 or later"},
		{name: "standalone wrapper", module: true, toolVersion: "go1.26.8", wantError: "standalone Go installation"},
		{name: "home directory", module: true, installed: true, home: true, toolVersion: "go1.26.8", wantError: "standalone Go installation"},
		{name: "repository directory", module: true, installed: true, repositoryInstall: true, toolVersion: "go1.26.8", wantError: "standalone Go installation"},
		{name: "checkout without a Go module", toolVersion: "go1.25.7"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root, _, tree := buildFixture(t)
			if tc.module {
				backend := filepath.Join(tree, "backend")
				if err := os.Mkdir(backend, 0700); err != nil {
					t.Fatal(err)
				}
				write(t, filepath.Join(backend, "go.mod"), "module fixture.invalid/sample\n\ngo 1.24\n")
				gitTest(t, tree, "add", "backend/go.mod")
				gitTest(t, tree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "add Go module")
				gitTest(t, root, "update-ref", "refs/remotes/origin/main", gitTest(t, tree, "rev-parse", "HEAD"))
			}
			goRoot, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			if tc.repositoryInstall {
				goRoot = filepath.Join(tree, "go-install")
				write(t, filepath.Join(root, ".git", "info", "exclude"), "/go-install/\n")
			}
			goBin := filepath.Join(goRoot, "bin")
			if err := os.MkdirAll(goBin, 0700); err != nil {
				t.Fatal(err)
			}
			versionOutput := "go version " + tc.toolVersion + " " + runtime.GOOS + "/" + runtime.GOARCH
			goPath := filepath.Join(goBin, "go")
			if tc.installed {
				program := filepath.Join(t.TempDir(), "go_fixture.go")
				write(t, program, `package main
import "fmt"
func main() { fmt.Println(`+strconv.Quote(versionOutput)+`) }
`)
				cmd := exec.Command("go", "build", "-ldflags", "-X runtime.buildVersion="+tc.toolVersion, "-o", goPath, program)
				if output, err := cmd.CombinedOutput(); err != nil {
					t.Fatalf("build fixture Go executable: %v: %s", err, output)
				}
				toolDir := filepath.Join(goRoot, "pkg", "tool", runtime.GOOS+"_"+runtime.GOARCH)
				if err := os.MkdirAll(toolDir, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(toolDir, "compile"), nil, 0700); err != nil {
					t.Fatal(err)
				}
				write(t, filepath.Join(goRoot, "VERSION"), "go1.26.8\n")
			} else if err := os.WriteFile(goPath, []byte("#!/bin/sh\nprintf '%s\\n' '"+versionOutput+"'\n"), 0700); err != nil {
				t.Fatal(err)
			}
			if tc.home {
				t.Setenv("HOME", goRoot)
			}
			launcherDir := t.TempDir()
			if err := os.Symlink(goPath, filepath.Join(launcherDir, "go")); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", launcherDir+string(os.PathListSeparator)+os.Getenv("PATH"))
			grant := strconv.Quote(goRoot) + `="read"`
			checkGrant := `case "$*" in *'` + grant + `'*) exit 14 ;; esac`
			if tc.module && tc.wantError == "" {
				checkGrant = `case "$*" in *'` + grant + `'*) ;; *) exit 14 ;; esac
test "$(command -v go)" = '` + goPath + `' || exit 15
test "${GOTOOLCHAIN-}" = local || exit 16`
			}
			fakeCodexResponse(t, `
case " $* " in *" -c allow_login_shell=false "*) ;; *) exit 12 ;; esac
case " $* " in *" -c features.shell_snapshot=false "*) ;; *) exit 13 ;; esac
`+checkGrant+`
go version > approved.txt`, validBuild)

			err = run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
			if tc.wantError != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantError) {
					t.Fatalf("builder error = %v, want %q", err, tc.wantError)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			wantFile(t, filepath.Join(tree, "approved.txt"), versionOutput+"\n")
		})
	}
}

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
