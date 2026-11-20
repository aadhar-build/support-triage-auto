package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

// Existing harness tests exercise the content-only contract.
func execute(ctx context.Context, bin string, chosen selection, s snapshot, prompt string, r role) ([]byte, error) {
	result, err := executeWorker(ctx, bin, chosen, s, prompt, r)
	if err != nil {
		return nil, err
	}
	return result.artifact, nil
}

func TestClaudeNativeDenialSummary(t *testing.T) {
	row := `{"tool_name":"Bash"}`
	valid := `{"permission_denials":[` + row + `]}`
	many := strings.Repeat(row+",", maxDenialEntries-1) + row
	for _, tc := range []struct{ name, terminal, expected string }{
		{"empty", `{"permission_denials":[]}`, `{"status":"reported","count":0}`},
		{"one", valid, `{"status":"reported","count":1,"by_category":{"shell":1}}`},
		{"categories", `{"permission_denials":[{"tool_name":"Edit"},{"tool_name":"Write"},{"tool_name":"Read"},{"tool_name":"Glob"},{"tool_name":"Grep"},{"tool_name":"custom-private-name"}]}`,
			`{"status":"reported","count":6,"by_category":{"edit":1,"other":1,"read":3,"write":1}}`},
		{"limit", `{"permission_denials":[` + many + `]}`, `{"status":"reported","count":256,"by_category":{"shell":256}}`},
		{"missing", `{"result":"permission_denials: [Write]"}`, ""},
		{"null", `{"permission_denials":null}`, ""},
		{"not array", `{"permission_denials":{}}`, ""},
		{"malformed row after good one", `{"permission_denials":[` + row + `,{}]}`, ""},
		{"missing name", `{"permission_denials":[{"tool_input":{"tool_name":"Write"}}]}`, ""},
		{"blank", `{"permission_denials":[{"tool_name":" \t"}]}`, ""},
		{"wrong type", `{"permission_denials":[{"tool_name":false}]}`, ""},
		{"scalar row", `{"permission_denials":[true]}`, ""},
		{"case alias", `{"PERMISSION_DENIALS":[]}`, ""},
		{"duplicate field", `{"permission_denials":null,"permission_denials":[]}`, ""},
		{"duplicate row name", `{"permission_denials":[{"tool_name":"Read","tool_name":"Write"}]}`, ""},
		{"over count", `{"permission_denials":[` + many + "," + row + `]}`, ""},
		{"over field", `{"permission_denials":[{"tool_name":"Write","tool_input":"` + strings.Repeat("z", maxDenialField) + `"}]}`, ""},
		{"over envelope", `{"permission_denials":[],"result":"` + strings.Repeat("z", maxDenialEnvelope) + `"}`, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			want := tc.expected
			if want == "" {
				want = `{"status":"unavailable"}`
			}
			actual, err := json.Marshal(claudeNativeDenials([]byte(tc.terminal)))
			if err != nil || string(actual) != want {
				t.Fatalf("summary = %s (%v), want %s", actual, err, want)
			}
		})
	}
}

func TestClaudeNoChangeFailureSeparatesModelReasonFromNativeCounts(t *testing.T) {
	root, job, worktree := buildFixture(t)
	configPath := filepath.Join(root, "factory", "config.json")
	write(t, configPath, strings.Replace(read(t, configPath), `"builder":{"harness":"codex","model":"gpt-explicit"}`,
		`"builder":{"harness":"claude","model":"opus"}`, 1))
	head := gitTest(t, worktree, "rev-parse", "HEAD")
	state := read(t, filepath.Join(job, "job.json"))
	audit := filepath.Join(job, "worker-round-1-builder.json")
	failure := audit + ".failure"
	model := validBuild + "NO-CHANGE REASON: already-satisfied\nprivate-model-text\n"
	fields := `,"permission_denials":[{"tool_name":"Write","tool_input":{"file_path":"/private-text/customer.txt"},"tool_use_id":"private-id"},{"tool_name":"private-custom-tool"}]`
	fakeClaudeResult(t, "echo private-stderr >&2", model, fields)
	var stdout bytes.Buffer
	err := run(root, []string{"001-fixture", "builder"}, &stdout)
	if !errors.Is(err, errNoCandidate) || stdout.Len() != 0 {
		t.Fatalf("expected a no-change failure, got %v", err)
	}
	data := read(t, failure)
	if len(data) > 512 || strings.Contains(data+err.Error()+stdout.String(), "private-") {
		t.Fatal("diagnostic included private output or exceeded the sidecar size bound")
	}
	var record struct {
		Reported string          `json:"reported_reason"`
		Native   json.RawMessage `json:"native_permission_denials"`
	}
	if err := json.Unmarshal([]byte(data), &record); err != nil {
		t.Fatal(err)
	}
	if record.Reported != "already-satisfied" || string(record.Native) != `{"status":"reported","count":2,"by_category":{"other":1,"write":1}}` {
		t.Fatalf("missing independent observations: %+v", record)
	}
	wantFile(t, filepath.Join(job, "job.json"), state)
	if gitTest(t, worktree, "rev-parse", "HEAD") != head || gitTest(t, worktree, "status", "--porcelain") != "" {
		t.Fatal("no-change diagnostics modified the candidate")
	}
	noFile(t, filepath.Join(job, "build.md"))
	noFile(t, audit)
	noFile(t, audit+".pending")

	// A successful retry can encounter an optional native denial. It neither
	// changes the previous failure record nor adds denial data to the success audit.
	fakeClaudeResult(t, "printf 'retry succeeded\\n' > approved.txt", validBuild, fields)
	if err := run(root, []string{"001-fixture", "builder"}, &stdout); err != nil {
		t.Fatal(err)
	}
	wantFile(t, failure, data)
	wantFile(t, filepath.Join(job, "build.md"), validBuild)
	wantFile(t, filepath.Join(worktree, "approved.txt"), "retry succeeded\n")
	if strings.Contains(read(t, audit), "permission_denials") {
		t.Fatal("success audit included native failure diagnostics")
	}
}

func TestNoChangeReceiptDoesNotInventNativeCounts(t *testing.T) {
	for _, tc := range []struct{ harness, fields, want string }{
		{"claude", `,"permission_denials":[]`, `{"status":"reported","count":0}`},
		{"claude", `,"permission_denials":null`, `{"status":"unavailable"}`},
		{"codex", "", ""},
	} {
		t.Run(tc.harness+tc.fields, func(t *testing.T) {
			root, job, _ := buildFixture(t)
			if tc.harness == "claude" {
				configPath := filepath.Join(root, "factory", "config.json")
				write(t, configPath, strings.Replace(read(t, configPath), `"builder":{"harness":"codex","model":"gpt-explicit"}`,
					`"builder":{"harness":"claude","model":"opus"}`, 1))
				fakeClaudeResult(t, "", validBuild, tc.fields)
			} else {
				fakeCodexResponse(t, "", validBuild)
			}
			if err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{}); !errors.Is(err, errNoCandidate) {
				t.Fatalf("expected the existing no-change refusal, got %v", err)
			}
			var record map[string]json.RawMessage
			if err := readJSON(filepath.Join(job, "worker-round-1-builder.json.failure"), &record); err != nil {
				t.Fatal(err)
			}
			if got := string(record["native_permission_denials"]); got != tc.want {
				t.Fatalf("native denial observation = %q, want %q", got, tc.want)
			}
		})
	}
}
