package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

func TestClaudeEvidenceRefusalDiagnostics(t *testing.T) {
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n"), "/tmp/private-other": []byte("other")}
	const private = "PRIVATE-native-text-do-not-print"
	events := func(parts ...string) string { return strings.Join(append([]string{claudeReadInit}, parts...), "\n") }
	duplicate := strings.ReplaceAll(claudeReadCall, "toolu_1", private)
	for _, tc := range []struct {
		name, stream, reason, scope   string
		requested, completed, pending int
	}{
		{"invalid input", events(strings.Replace(claudeReadCall, `"file_path":`, `"private_argument":"`+private+`","file_path":`, 1)), "invalid_read_input", "", 0, 0, 0},
		{"partial window", events(strings.Replace(claudeReadCall, `"file_path":`, `"limit":1,"file_path":`, 1)), "unsupported_read_window", "mandatory", 0, 0, 0},
		{"offered supplemental", events(strings.Replace(claudeReadCall, "/tmp/evidence/spec.txt", "/tmp/"+private, 1)), "supplemental_read_unverified", "", 0, 0, 0},
		{"duplicate", events(claudeReadCall, claudeReadReceipt, duplicate), "duplicate_read", "mandatory", 1, 1, 0},
		{"metadata mismatch", events(claudeReadCall, strings.Replace(claudeReadReceipt, `"filePath":"/tmp/evidence/spec.txt"`, `"filePath":"/`+private+`"`, 1)), "structured_result_mismatch", "", 1, 0, 1},
		{"render mismatch", events(claudeReadCall, strings.Replace(claudeReadReceipt, `1\tfirst`, private, 1)), "rendered_result_mismatch", "", 1, 0, 1},
		{"incomplete", events(claudeReadCall, claudeReadReceipt, claudeReadVerdict, claudeReadFinal), "incomplete_mandatory_reads", "", 1, 1, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := validateClaudeReadEvents([]byte(strings.ReplaceAll(tc.stream, "test-session", private)), expected, nil)
			want := fmt.Sprintf(`%s; diagnostic={"reason":"%s","mandatory_expected":2,"mandatory_requested":%d,"mandatory_completed":%d,"pending_reads":%d`, errClaudeEvidenceReads, tc.reason, tc.requested, tc.completed, tc.pending)
			if tc.scope != "" {
				want += fmt.Sprintf(`,"read_scope":"%s"`, tc.scope)
			}
			want += "}"
			if got != nil || !errors.Is(err, errClaudeEvidenceReads) || err.Error() != want {
				t.Fatalf("artifact present=%t, refusal=%v, want %s", got != nil, err, want)
			}
		})
	}
}

func TestClaudeApprovalRefusesUnsupportedSupplementalWindowAfterMandatoryReads(t *testing.T) {
	s := reviewSnapshotFixture(t, strings.Repeat("x\n", 2000))
	path := filepath.Join(s.checkout, "candidate.txt")
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	call := strings.NewReplacer("toolu_1", "toolu_2", "/tmp/evidence/spec.txt", path).Replace(claudeReadCall)
	for _, input := range []string{call, strings.Replace(call, `"file_path":`, `"offset":2,"file_path":`, 1)} {
		supplemental := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head}
		stream := strings.Join([]string{claudeReadInit, claudeReadCall, claudeReadReceipt, input,
			strings.Replace(claudeReadVerdict, "VERDICT: PASS", "APPROVE", 1),
			strings.Replace(claudeReadFinal, "VERDICT: PASS", "APPROVE", 1)}, "\n")
		got, err := validateClaudeReadEvents([]byte(stream), expected, &supplemental)
		want := errClaudeEvidenceReads.Error() + `; diagnostic={"reason":"unsupported_read_window","mandatory_expected":1,"mandatory_requested":1,"mandatory_completed":1,"pending_reads":0,"read_scope":"supplemental"}`
		if got != nil || !errors.Is(err, errClaudeEvidenceReads) || err.Error() != want {
			t.Fatalf("expected a bounded refusal after all required reads, got %q and %v", got, err)
		}
	}
}

func TestClaudeRefusalDiagnosticsForInvalidExpectedFrame(t *testing.T) {
	got, err := validateClaudeReadEvents(nil, map[string][]byte{"private-invalid-path": []byte("private-frame")}, nil)
	want := errClaudeEvidenceReads.Error() + `; diagnostic={"reason":"invalid_expected_frame","mandatory_expected":1,"mandatory_requested":0,"mandatory_completed":0,"pending_reads":0}`
	if got != nil || !errors.Is(err, errClaudeEvidenceReads) || err.Error() != want {
		t.Fatalf("expected a sanitized pinned-frame refusal, got %v", err)
	}
}

func TestClaudeSupplementalRefusalLeavesMandatoryCountsUnchanged(t *testing.T) {
	s := reviewSnapshotFixture(t, "first\nsecond\n")
	supplemental := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head}
	path := filepath.Join(s.checkout, "candidate.txt")
	call := strings.ReplaceAll(claudeReadCall, "/tmp/evidence/spec.txt", path)
	receipt := strings.NewReplacer("/tmp/evidence/spec.txt", path, `1\tfirst`, "private-rendered-content").Replace(claudeReadReceipt)
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	got, err := validateClaudeReadEvents([]byte(strings.Join([]string{claudeReadInit, call, receipt}, "\n")), expected, &supplemental)
	want := errClaudeEvidenceReads.Error() + `; diagnostic={"reason":"rendered_result_mismatch","mandatory_expected":1,"mandatory_requested":0,"mandatory_completed":0,"pending_reads":1}`
	if got != nil || !errors.Is(err, errClaudeEvidenceReads) || err.Error() != want {
		t.Fatalf("expected a pending supplemental read and zero mandatory progress, got %v", err)
	}
}

func TestClaudeRefusalDiagnosticsPreserveNoPublication(t *testing.T) {
	root, round, _ := claudeReviewFixture(t)
	fakeClaudeResult(t, "", "VERDICT: PASS\n\n## Summary\nprivate-result-body\n", `,"permission_denials":[]`)
	var out bytes.Buffer
	err := run(root, []string{"001-fixture", "reviewer-code"}, &out)
	if !errors.Is(err, errClaudeEvidenceReads) || !strings.Contains(err.Error(), `"reason":"incomplete_mandatory_reads"`) ||
		strings.Contains(err.Error(), "private-result-body") || len(err.Error()) > 320 || out.Len() != 0 {
		t.Fatalf("expected bounded refusal with no output, got %v", err)
	}
	noFile(t, filepath.Join(round, "review-code.md"))
	noFile(t, filepath.Join(round, "worker-reviewer-code.json"))
}
