package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestClaudeReviewAndApprovalRequireEmptyDenials(t *testing.T) {
	for _, stage := range []string{"review", "approve"} {
		for _, tc := range []struct {
			name, field string
			accepted    bool
		}{
			{"missing", "", false},
			{"null", `,"permission_denials":null`, false},
			{"object", `,"permission_denials":{}`, false},
			{"denied", `,"permission_denials":[{"tool_name":"Read"}]`, false},
			{"empty", `,"permission_denials":[]`, true},
		} {
			t.Run(stage+"/"+tc.name, func(t *testing.T) {
				tmp := t.TempDir()
				checkout := filepath.Join(tmp, "checkout")
				if err := os.Mkdir(checkout, 0700); err != nil {
					t.Fatal(err)
				}
				response := "VERDICT: CHANGES\n\n## Summary\nFixture.\n\n## Problems\nFixture.\n"
				if stage == "approve" {
					response = "ESCALATE\n\n## Summary\nFixture.\n\n## For the human\nFixture.\n"
				}
				fakeClaudeResult(t, "", response, tc.field)
				bin, err := filepath.EvalSymlinks(strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0] + "/claude")
				if err != nil {
					t.Fatal(err)
				}
				_, err = execute(context.Background(), bin, selection{Harness: "claude", Model: "fixture"}, snapshot{temp: tmp, checkout: checkout}, "fixture", role{stage: stage})
				if (err == nil) != tc.accepted {
					t.Fatalf("accepted=%v, err=%v", tc.accepted, err)
				}
			})
		}
	}
}

func TestNativeHarnessRefusesPositiveReviewWithoutObservedReads(t *testing.T) {
	for _, harness := range []string{"claude", "codex"} {
		for _, tc := range []struct{ name, stage, result, script string }{
			{"no reads", "review", "VERDICT: PASS\n\n## Summary\n- Skipped input.\n", ""},
			{"failed read", "review", "VERDICT: PASS\n\n## Summary\n- Could not read an input.\n", "cat missing.md 2>/dev/null || :\n"},
			{"truncated final read", "approve", "APPROVE\n\n## Summary\n- Saw some of the evidence.\n", "head -n 1 evidence.md >&2\n"},
		} {
			t.Run(harness+"/"+tc.name, func(t *testing.T) {
				checkout := t.TempDir()
				write(t, filepath.Join(checkout, "evidence.md"), "prefix\nend of evidence\n")
				if harness == "claude" {
					fakeClaudeResult(t, tc.script, tc.result, `,"permission_denials":[]`)
				} else {
					fakeCodexResponse(t, tc.script, tc.result)
				}
				bin, err := exec.LookPath(harness)
				if err != nil {
					t.Fatal(err)
				}
				_, err = execute(context.Background(), bin, selection{Harness: harness, Model: "fixture"},
					snapshot{temp: t.TempDir(), checkout: checkout}, "fixture", role{stage: tc.stage})
				if err == nil {
					t.Fatal("accepted positive review without a complete native read stream")
				}
			})
		}
	}
}

func TestHarnessesReceiveQuotedOwnerAnswerOnlyInEvidenceFile(t *testing.T) {
	for _, harness := range []string{"claude", "codex"} {
		t.Run(harness, func(t *testing.T) {
			s := reviewSnapshotFixture(t, "candidate edit\n")
			s.temp = t.TempDir()
			write(t, filepath.Join(s.job, "spec.md"), "## Owner decisions\n> OWNER ANSWER FIXTURE\n")
			if err := s.writeReviewInputs(context.Background()); err != nil {
				t.Fatal(err)
			}
			const check = `
case "$prompt" in *"OWNER ANSWER FIXTURE"*) exit 21 ;; esac
job=$(printf '%s\n' "$prompt" | sed -n 's/^Job folder: //p')
grep -Fxq '> OWNER ANSWER FIXTURE' "$job/spec.md"
`
			const result = "VERDICT: CHANGES\n\n## Summary\n- Human review needed.\n\n## Problems\n- Missing confirmation.\n"
			if harness == "claude" {
				fakeClaudeResult(t, check, result, `,"permission_denials":[]`)
			} else {
				fakeCodexResponse(t, "prompt=$(cat)\n"+check, result)
			}
			bin, err := exec.LookPath(harness)
			if err != nil {
				t.Fatal(err)
			}
			r := roles["reviewer-code"]
			prompt := s.prompt(jobState{}, config{}, r, "Review fixture.")
			content, err := execute(context.Background(), bin, selection{Harness: harness, Model: "fixture"}, s, prompt, r)
			if err != nil || string(content) != result {
				t.Fatalf("wanted the complete negative review, got %q and %v", content, err)
			}
		})
	}
}

func TestClaudeBuilderCanRecoverFromOptionalToolDenial(t *testing.T) {
	checkout := t.TempDir()
	fakeClaudeResult(t, "printf 'completed\\n' > candidate.txt", validBuild, `,"permission_denials":[{"tool_name":"Read"}]`)
	bin, err := exec.LookPath("claude")
	if err != nil {
		t.Fatal(err)
	}
	content, err := execute(context.Background(), bin, selection{Harness: "claude", Model: "fixture"},
		snapshot{temp: t.TempDir(), checkout: checkout}, "fixture", roles["builder"])
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != validBuild {
		t.Fatalf("unexpected artifact %q", content)
	}
	wantFile(t, filepath.Join(checkout, "candidate.txt"), "completed\n")
}
