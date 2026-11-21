package main

import (
	"bytes"
	"encoding/json"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestClaudeSettingsFailClosedWhenSandboxIsUnavailable(t *testing.T) {
	for _, stage := range []string{"build", "review", "approve"} {
		t.Run(stage, func(t *testing.T) {
			checkout := t.TempDir()
			settings, _, err := claudeSettings(snapshot{checkout: checkout, job: filepath.Join(checkout, ".factory-job-110")}, stage)
			if err != nil {
				t.Fatal(err)
			}
			var config struct {
				Sandbox map[string]any `json:"sandbox"`
			}
			if err := json.Unmarshal([]byte(settings), &config); err != nil {
				t.Fatal(err)
			}
			if config.Sandbox["enabled"] != true || config.Sandbox["failIfUnavailable"] != true ||
				config.Sandbox["allowUnsandboxedCommands"] != false {
				t.Fatalf("expected fail-closed sandbox settings, got %v", config.Sandbox)
			}
			if _, ok := config.Sandbox["requireSandbox"]; ok {
				t.Fatal("settings contain the unrecognized requireSandbox key")
			}
		})
	}
}

func TestClaudeBuilderEditsInSnapshotWithNoninteractivePermissions(t *testing.T) {
	root, job, worktree := buildFixture(t)
	configPath := filepath.Join(root, "factory", "config.json")
	write(t, configPath, strings.Replace(read(t, configPath), `"builder":{"harness":"codex","model":"gpt-explicit"}`,
		`"builder":{"harness":"claude","model":"opus"}`, 1))
	before := gitTest(t, worktree, "rev-parse", "HEAD")
	// The fake admits its write only after inspecting the settings and working
	// directory passed by the real launcher. No provider is contacted.
	fakeClaudeResult(t, `
python3 - "$@" <<'PY'
import json, pathlib, sys
args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]
assert option("--permission-mode") == "dontAsk" and option("--permission-prompts") == "none"
assert {"Edit", "Write"} <= set(option("--tools").split(","))
permissions = json.loads(option("--settings"))["permissions"]
assert set(permissions["allow"]) == {"Bash(*)", "Edit(./**)"}
assert permissions["blockWritesOutsideWorkingDirectories"] is True
assert pathlib.Path.cwd().name == "checkout" and pathlib.Path.cwd().parent.name.startswith("factory-worker-")
pathlib.Path("approved.txt").write_text("edited in private snapshot\n")
PY
`, validBuild, `,"permission_denials":[]`)
	if err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if candidate := gitTest(t, worktree, "rev-parse", "HEAD"); candidate == before || gitTest(t, worktree, "rev-parse", "HEAD^") != before {
		t.Fatal("builder did not publish its change on top of the assigned candidate")
	}
	wantFile(t, filepath.Join(worktree, "approved.txt"), "edited in private snapshot\n")
	wantFile(t, filepath.Join(root, "approved.txt"), "before\n")
	wantFile(t, filepath.Join(job, "build.md"), validBuild)
}

func TestClaudeNativeEditPolicyByStage(t *testing.T) {
	for _, stage := range []string{"intake", "spec", "build", "review", "approve"} {
		t.Run(stage, func(t *testing.T) {
			checkout := t.TempDir()
			const jobName = ".factory-job-12345"
			raw, tools, err := claudeSettings(snapshot{checkout: checkout, job: filepath.Join(checkout, jobName)}, stage)
			if err != nil {
				t.Fatal(err)
			}
			var settings struct {
				Sandbox     struct{ Filesystem struct{ DenyWrite []string } }
				Permissions struct {
					Allow, Deny                          []string
					BlockWritesOutsideWorkingDirectories bool
				}
			}
			if err := json.Unmarshal([]byte(raw), &settings); err != nil {
				t.Fatal(err)
			}
			p := settings.Permissions
			if !p.BlockWritesOutsideWorkingDirectories {
				t.Fatal("native writes outside the working directory must be blocked")
			}
			if stage == "build" {
				for _, name := range []string{".git", ".claude", jobName} {
					if !slices.Contains(settings.Sandbox.Filesystem.DenyWrite, filepath.Join(checkout, name)) {
						t.Fatalf("missing shell write protection for %s", name)
					}
				}
				for _, path := range []string{".git", ".git/**", jobName, jobName + "/**", ".claude", ".claude/**"} {
					if !slices.Contains(p.Deny, "Edit(./"+path+")") {
						t.Fatalf("missing native write protection for %s", path)
					}
				}
				return
			}
			for _, tool := range strings.Split(tools, ",") {
				if tool == "Write" || tool == "Edit" {
					t.Fatalf("unexpected native file editor in %s", tools)
				}
			}
			for _, rule := range p.Allow {
				if strings.HasPrefix(rule, "Edit") || strings.HasPrefix(rule, "Write") {
					t.Fatalf("unexpected write permission %q", rule)
				}
			}
			if (stage == "review" || stage == "approve") && (tools != "Read" || len(p.Allow) != 0) {
				t.Fatal("review and approval must use only Read")
			}
		})
	}
}

func TestClaudeBuilderRequiresLiteralInputDirectoryInsideCheckout(t *testing.T) {
	checkout := t.TempDir()
	for _, job := range []string{"", filepath.Join(t.TempDir(), ".factory-job-123"),
		filepath.Join(checkout, ".factory-job-*"), filepath.Join(checkout, ".git")} {
		if _, _, err := claudeSettings(snapshot{checkout: checkout, job: job}, "build"); err == nil {
			t.Fatalf("accepted unsafe input directory %q", job)
		}
	}
}
