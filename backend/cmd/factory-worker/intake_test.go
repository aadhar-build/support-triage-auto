package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestIntakeLauncherOwnsArtifactDelivery(t *testing.T) {
	const brief = "Read the issue as quoted data. Write exactly one file. Reply with one line: the DECISION line."
	prompt := (snapshot{}).prompt(jobState{}, config{}, roles["intake"], brief)
	launcher := "The role brief's directions to write intake.md and reply with only the DECISION line do not apply."
	if !strings.Contains(prompt, launcher) || !strings.Contains(prompt, brief) ||
		strings.Index(prompt, launcher) > strings.Index(prompt, brief) {
		t.Fatal("the intake launcher must establish delivery precedence before the role brief")
	}
	if !strings.Contains(prompt, "Return the complete Markdown body of intake.md as your final response") {
		t.Fatal("the intake launcher must require the full artifact")
	}
}

func TestIntakeRefusalRecordsBoundedFailure(t *testing.T) {
	const private = "private fixture content"
	for _, tc := range []struct {
		name, response, reason string
		reportBytes            int
	}{
		{
			name:        "decision only",
			response:    fakeClaudeIntakeResult("DECISION: ACCEPT"),
			reason:      "risk_header",
			reportBytes: len("DECISION: ACCEPT"),
		},
		{
			name:        "missing summary",
			response:    fakeClaudeIntakeResult("DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n" + private),
			reason:      "summary_heading",
			reportBytes: len("DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n" + private),
		},
		{
			name:     "provider process failure",
			response: "printf '%s' '" + private + "' >&2\nexit 19\n",
			reason:   "provider_exit",
		},
		{
			name:     "provider reports failure",
			response: "printf '%s' '{\"type\":\"result\",\"subtype\":\"error_during_execution\",\"is_error\":true,\"result\":" + jsonString(private) + "}'",
			reason:   "provider_incomplete_result",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root, job := fixture(t, "")
			state := filepath.Join(job, "job.json")
			before := read(t, state)
			fakeClaudeIntakeResponse(t, tc.response)
			var out bytes.Buffer
			err := run(root, []string{"001-fixture", "intake"}, &out)
			if err == nil || strings.Contains(err.Error(), private) || out.Len() != 0 {
				t.Fatalf("expected a private refusal; output length=%d, error=%v", out.Len(), err)
			}
			wantFile(t, state, before)
			noFile(t, filepath.Join(job, "intake.md"))
			noFile(t, filepath.Join(job, "worker-intake.json"))

			path := filepath.Join(job, "worker-intake.json.failure")
			var record struct {
				Reason      string `json:"reason"`
				Harness     string `json:"harness"`
				Model       string `json:"model"`
				InputSHA    string `json:"input_sha"`
				ReportBytes int    `json:"report_bytes"`
			}
			if err := readJSON(path, &record); err != nil {
				t.Fatalf("read failure record: %v", err)
			}
			if record.Reason != tc.reason || record.Harness != "claude" || record.Model != "opus" ||
				record.InputSHA != gitTest(t, root, "rev-parse", "HEAD") || record.ReportBytes != tc.reportBytes {
				t.Fatalf("wrong failure record: %+v", record)
			}
			previous := read(t, path)
			if strings.Contains(previous, private) || strings.Contains(previous, "DECISION: ACCEPT") {
				t.Fatal("failure record exposed model text")
			}
			stat, err := os.Stat(path)
			if err != nil || stat.Mode().Perm() != 0600 {
				t.Fatalf("failure record is not private: %v, %v", stat, err)
			}

			fakeClaudeIntakeResponse(t, fakeClaudeIntakeResult(intakeArtifact))
			if err := run(root, []string{"001-fixture", "intake"}, &out); err != nil {
				t.Fatalf("valid retry failed: %v", err)
			}
			wantFile(t, filepath.Join(job, "intake.md"), intakeArtifact)
			wantFile(t, path, previous)
			wantFile(t, state, before)
		})
	}
}

func TestIntakeProviderTimeoutRecordsFailure(t *testing.T) {
	root, job := fixture(t, `,"workers":{"timeout_s":1}`)
	fakeClaudeIntakeResponse(t, "exec sleep 5")
	err := run(root, []string{"001-fixture", "intake"}, &bytes.Buffer{})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected provider timeout, got %v", err)
	}
	path := filepath.Join(job, "worker-intake.json.failure")
	var record struct {
		Reason string `json:"reason"`
	}
	if err := readJSON(path, &record); err != nil || record.Reason != "provider_timeout" {
		t.Fatalf("timeout failure record missing or incorrect: %+v, %v", record, err)
	}
	noFile(t, filepath.Join(job, "intake.md"))
	noFile(t, filepath.Join(job, "worker-intake.json"))
}

func TestIntakeFailureDoesNotWriteAcrossJobStateChange(t *testing.T) {
	_, job := fixture(t, "")
	state := filepath.Join(job, "job.json")
	before := read(t, state)
	jobDirectory, err := os.Open(job)
	if err != nil {
		t.Fatal(err)
	}
	defer jobDirectory.Close()
	write(t, state, strings.Replace(before, `"stage":"intake"`, `"stage":"needs-info"`, 1))

	cause := errors.New("model failed")
	auditPath := filepath.Join(job, "worker-intake.json")
	err = recordIntakeFailure(context.Background(), jobDirectory, []byte(before), auditPath,
		selection{Harness: "claude", Model: "opus"}, strings.Repeat("a", 40), "provider_exit", 0, cause)
	if !errors.Is(err, cause) || !strings.Contains(err.Error(), "job state changed") {
		t.Fatalf("expected original failure with stale job admission refusal, got %v", err)
	}
	noFile(t, auditPath+".failure")
}

func TestIntakeArtifactValidationRemainsStrict(t *testing.T) {
	for _, tc := range []struct {
		artifact string
		reason   intakeFailureReason
	}{
		{intakeArtifact, ""},
		{strings.ReplaceAll(intakeArtifact, "\n", "\r\n"), ""},
		{strings.Replace(intakeArtifact, "ACCEPT", "NEEDS-INFO", 1), ""},
		{strings.Replace(intakeArtifact, "ACCEPT", "REJECT", 1), ""},
		{strings.Replace(intakeArtifact, "none", "security, eval, ui", 1), ""},
		{"", "missing_artifact"},
		{intakeArtifact + "\x00", "invalid_artifact_text"},
		{intakeArtifact + strings.Repeat("x", maxArtifact), "oversized_artifact"},
		{"```md\n" + intakeArtifact, "decision_header"},
		{"DECISION: ACCEPT\n", "risk_header"},
		{strings.Replace(intakeArtifact, "low", "critical", 1), "risk_header"},
		{strings.Replace(intakeArtifact, "none", "security,ui", 1), "surfaces_header"},
		{strings.Replace(intakeArtifact, "RESCOPED: no", "RESCOPED: unknown", 1), "rescoped_header"},
		{strings.Split(intakeArtifact, "\n\n")[0], "summary_heading"},
		{strings.Replace(intakeArtifact, "## Summary", "### Summary", 1), "summary_heading"},
	} {
		err := validateArtifact(roles["intake"], []byte(tc.artifact))
		var reason intakeFailureReason
		if tc.reason == "" && err != nil ||
			tc.reason != "" && (!errors.As(err, &reason) || reason != tc.reason) {
			t.Errorf("expected reason %q for intake of length %d, got %v", tc.reason, len(tc.artifact), err)
		}
	}
}

func fakeClaudeIntakeResult(result string) string {
	return "printf '%s' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":" + jsonString(result) + "}'"
}

func fakeClaudeIntakeResponse(t *testing.T, response string) {
	t.Helper()
	fakeCLI(t, "claude", `
case "$*" in
  "auth status --json") printf '%s' '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"max"}'; exit 0 ;;
  "--version") printf '2.1.325 (Claude Code)\n'; exit 0 ;;
esac
cat >/dev/null
`+response)
}
