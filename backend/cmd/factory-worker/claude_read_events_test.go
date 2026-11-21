package main

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

const claudeReadInit = `{"type":"system","subtype":"init","session_id":"test-session"}`
const claudeThinkingTokens = `{"type":"system","subtype":"thinking_tokens","session_id":"test-session","uuid":"cd18d57a-39f1-4d8f-b486-26c0f937a60a","estimated_tokens":42,"estimated_tokens_delta":0}`
const claudeReadCall = `{"type":"assistant","session_id":"test-session","parent_tool_use_id":null,"message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/tmp/evidence/spec.txt"}}]}}`
const claudeReadReceipt = `{"type":"user","session_id":"test-session","parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"1\tfirst\n2\tsecond\n3\t"}]},"tool_use_result":{"type":"text","file":{"filePath":"/tmp/evidence/spec.txt","content":"first\nsecond\n","numLines":3,"startLine":1,"totalLines":3}}}`
const claudeReadVerdict = `{"type":"assistant","session_id":"test-session","parent_tool_use_id":null,"message":{"role":"assistant","content":[{"type":"text","text":"VERDICT: PASS\n\n## Summary\n- Evidence checked."}]}}`
const claudeReadFinal = `{"type":"result","session_id":"test-session","subtype":"success","is_error":false,"num_turns":2,"result":"VERDICT: PASS\n\n## Summary\n- Evidence checked.","permission_denials":[]}`

func TestValidateClaudeReadEventsAcceptsCompleteReadBeforeVerdict(t *testing.T) {
	stream := strings.Join([]string{claudeReadInit, claudeReadCall, claudeReadReceipt, claudeReadVerdict, claudeReadFinal}, "\n") + "\n"
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	got, err := validateClaudeReadEvents([]byte(stream), expected, nil)
	if err != nil || string(got) != "VERDICT: PASS\n\n## Summary\n- Evidence checked." {
		t.Fatalf("got artifact %q, error %v", got, err)
	}
}

func TestValidateClaudeReadEventsIgnoresNativeThinkingTelemetry(t *testing.T) {
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	updatedTelemetry := strings.NewReplacer(
		`"estimated_tokens":42`, `"estimated_tokens":41`,
		`"estimated_tokens_delta":0`, `"estimated_tokens_delta":-1`,
		`cd18d57a-39f1-4d8f-b486-26c0f937a60a`, `1c86a776-4e90-44f6-a13d-126fd0cf530d`,
	).Replace(claudeThinkingTokens)
	stream := strings.Join([]string{claudeReadInit, claudeReadCall, claudeThinkingTokens, claudeReadReceipt, claudeReadVerdict, updatedTelemetry, claudeReadFinal}, "\n")
	got, err := validateClaudeReadEvents([]byte(stream), expected, nil)
	if err != nil || string(got) != "VERDICT: PASS\n\n## Summary\n- Evidence checked." {
		t.Fatalf("got artifact %q, error %v", got, err)
	}

	withoutRead := strings.Join([]string{claudeReadInit, claudeThinkingTokens, claudeReadVerdict, claudeReadFinal}, "\n")
	if got, err := validateClaudeReadEvents([]byte(withoutRead), expected, nil); got != nil || !errors.Is(err, errClaudeEvidenceReads) {
		t.Fatalf("wanted an evidence refusal, got artifact %q and error %v", got, err)
	}
	withoutVerdict := strings.Join([]string{claudeReadInit, claudeReadCall, claudeReadReceipt, claudeThinkingTokens, claudeReadFinal}, "\n")
	if got, err := validateClaudeReadEvents([]byte(withoutVerdict), expected, nil); got != nil || err != errClaudeReadStream {
		t.Fatalf("wanted a missing verdict refusal, got artifact %q and error %v", got, err)
	}
}

func TestValidateClaudeReadEventsRefusesMalformedThinkingTelemetry(t *testing.T) {
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	for _, tc := range []struct{ name, beforeInit, telemetry, afterResult string }{
		{"before init", claudeThinkingTokens, "", ""},
		{"after result", "", "", claudeThinkingTokens},
		{"cross session", "", strings.Replace(claudeThinkingTokens, `"test-session"`, `"other-session"`, 1), ""},
		{"unknown subtype", "", strings.Replace(claudeThinkingTokens, `"thinking_tokens"`, `"other_telemetry"`, 1), ""},
		{"duplicate count", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens":42`, `"estimated_tokens":1,"estimated_tokens":42`, 1), ""},
		{"missing delta", "", strings.Replace(claudeThinkingTokens, `,"estimated_tokens_delta":0`, "", 1), ""},
		{"misnamed field", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens_delta"`, `"Estimated_Tokens_Delta"`, 1), ""},
		{"string count", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens":42`, `"estimated_tokens":"42"`, 1), ""},
		{"null count", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens":42`, `"estimated_tokens":null`, 1), ""},
		{"fractional count", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens":42`, `"estimated_tokens":0.25`, 1), ""},
		{"boolean delta", "", strings.Replace(claudeThinkingTokens, `"estimated_tokens_delta":0`, `"estimated_tokens_delta":true`, 1), ""},
		{"blank UUID", "", strings.Replace(claudeThinkingTokens, `"cd18d57a-39f1-4d8f-b486-26c0f937a60a"`, `" "`, 1), ""},
		{"embedded result", "", strings.TrimSuffix(claudeThinkingTokens, "}") + `,"result":"VERDICT: PASS"}`, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stream := strings.Join([]string{tc.beforeInit, claudeReadInit, claudeReadCall, claudeReadReceipt, tc.telemetry, claudeReadVerdict, claudeReadFinal, tc.afterResult}, "\n")
			if got, err := validateClaudeReadEvents([]byte(stream), expected, nil); got != nil || err != errClaudeReadStream {
				t.Fatalf("wanted invalid stream refusal, got artifact %q and error %v", got, err)
			}
		})
	}
}

func TestValidateClaudeReadEventsRejectsUnprovenVerdicts(t *testing.T) {
	for _, tc := range []struct {
		name, call, receipt, verdict, final, after string
	}{
		{"no reads", "", "", claudeReadVerdict, claudeReadFinal, ""},
		{"missing result", claudeReadCall, "", claudeReadVerdict, claudeReadFinal, ""},
		{"unmatched result", claudeReadCall, strings.ReplaceAll(claudeReadReceipt, "toolu_1", "toolu_2"), claudeReadVerdict, claudeReadFinal, ""},
		{"missing rendered data", claudeReadCall, strings.ReplaceAll(claudeReadReceipt, `1\tfirst\n2\tsecond\n3\t`, `1\tfirst`), claudeReadVerdict, claudeReadFinal, ""},
		{"failed tool result", claudeReadCall, strings.Replace(claudeReadReceipt, `"tool_use_id":`, `"is_error":true,"tool_use_id":`, 1), claudeReadVerdict, claudeReadFinal, ""},
		{"malformed tool result status", claudeReadCall, strings.Replace(claudeReadReceipt, `"tool_use_id":`, `"is_error":null,"tool_use_id":`, 1), claudeReadVerdict, claudeReadFinal, ""},
		{"incorrect structured data", claudeReadCall, strings.Replace(claudeReadReceipt, `"numLines":3`, `"numLines":1`, 1), claudeReadVerdict, claudeReadFinal, ""},
		{"truncated flag", claudeReadCall, strings.Replace(claudeReadReceipt, `"totalLines":3`, `"totalLines":3,"truncatedByTokenCap":true`, 1), claudeReadVerdict, claudeReadFinal, ""},
		{"wrong read range", strings.Replace(claudeReadCall, `"file_path":`, `"offset":2,"file_path":`, 1), claudeReadReceipt, claudeReadVerdict, claudeReadFinal, ""},
		{"duplicate result", claudeReadCall, claudeReadReceipt + "\n" + claudeReadReceipt, claudeReadVerdict, claudeReadFinal, ""},
		{"duplicate call ID", claudeReadCall + "\n" + claudeReadCall, claudeReadReceipt, claudeReadVerdict, claudeReadFinal, ""},
		{"cross-session result", claudeReadCall, strings.ReplaceAll(claudeReadReceipt, "test-session", "other-session"), claudeReadVerdict, claudeReadFinal, ""},
		{"subagent result", claudeReadCall, strings.ReplaceAll(claudeReadReceipt, `"parent_tool_use_id":null`, `"parent_tool_use_id":"toolu_parent"`), claudeReadVerdict, claudeReadFinal, ""},
		{"missing assistant verdict", claudeReadCall, claudeReadReceipt, "", claudeReadFinal, ""},
		{"assistant verdict before receipt", claudeReadCall, claudeReadVerdict + "\n" + claudeReadReceipt, "", claudeReadFinal, ""},
		{"different terminal result", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.ReplaceAll(claudeReadFinal, "Evidence checked.", "Evidence incomplete."), ""},
		{"terminal text with added space", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.ReplaceAll(claudeReadFinal, "Evidence checked.", "Evidence checked. "), ""},
		{"no verdict line", claudeReadCall, claudeReadReceipt, strings.ReplaceAll(claudeReadVerdict, "VERDICT: PASS", "Review complete."), strings.ReplaceAll(claudeReadFinal, "VERDICT: PASS", "Review complete."), ""},
		{"missing denials array", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.Replace(claudeReadFinal, `,"permission_denials":[]`, "", 1), ""},
		{"null denials array", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.Replace(claudeReadFinal, `"permission_denials":[]`, `"permission_denials":null`, 1), ""},
		{"non-empty denials array", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.Replace(claudeReadFinal, `"permission_denials":[]`, `"permission_denials":[{"tool_name":"Read"}]`, 1), ""},
		{"multiple terminal results", claudeReadCall, claudeReadReceipt, claudeReadVerdict, claudeReadFinal, claudeReadFinal},
		{"unknown event", claudeReadCall, claudeReadReceipt + "\n" + `{"type":"unknown","session_id":"test-session"}`, claudeReadVerdict, claudeReadFinal, ""},
		{"malformed frame", claudeReadCall, claudeReadReceipt, claudeReadVerdict, claudeReadFinal, `{"type":`},
		{"duplicate JSON key", claudeReadCall, claudeReadReceipt, claudeReadVerdict, strings.Replace(claudeReadFinal, `"permission_denials":[]`, `"permission_denials":[1],"permission_denials":[]`, 1), ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stream := strings.Join([]string{claudeReadInit, tc.call, tc.receipt, tc.verdict, tc.final, tc.after}, "\n")
			if _, err := validateClaudeReadEvents([]byte(stream), map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}, nil); err == nil {
				t.Fatal("accepted an unproven positive verdict")
			}
		})
	}
}

func TestValidateClaudeReadEventsAllowsCompleteNegativeVerdictWithoutRead(t *testing.T) {
	verdict := strings.ReplaceAll(claudeReadVerdict, `VERDICT: PASS\n\n## Summary\n- Evidence checked.`, `VERDICT: CHANGES\n\n## Summary\n- Evidence incomplete.\n\n## Problems\n- Missing evidence.`)
	final := strings.ReplaceAll(claudeReadFinal, `VERDICT: PASS\n\n## Summary\n- Evidence checked.`, `VERDICT: CHANGES\n\n## Summary\n- Evidence incomplete.\n\n## Problems\n- Missing evidence.`)
	stream := strings.Join([]string{claudeReadInit, verdict, final}, "\n")
	if _, err := validateClaudeReadEvents([]byte(stream), map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}, nil); err != nil {
		t.Fatal(err)
	}
}

func TestValidateClaudeReadEventsAcceptsNativeStartupAndTabAwareRead(t *testing.T) {
	startup := `{"type":"system","subtype":"ui_invalidate","event":"ui.render","session_id":"test-session","uuid":"startup"}`
	thinking := `{"type":"assistant","session_id":"test-session","parent_tool_use_id":null,"uuid":"thinking","message":{"id":"msg_1","role":"assistant","content":[{"type":"thinking","thinking":"I need the evidence."}]}}`
	call := strings.Replace(claudeReadCall, `"role":"assistant"`, `"id":"msg_1","role":"assistant"`, 1)
	receipt := `{"type":"user","session_id":"test-session","parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"1:\twithout a final LF"}]}}`
	stream := strings.Join([]string{startup, claudeReadInit, thinking, call, receipt, claudeReadVerdict, claudeReadFinal}, "\n")
	if _, err := validateClaudeReadEvents([]byte(stream), map[string][]byte{"/tmp/evidence/spec.txt": []byte("\twithout a final LF")}, nil); err != nil {
		t.Fatal(err)
	}
}

func TestValidateClaudeReadEventsReportsPermissionDenials(t *testing.T) {
	final := strings.Replace(claudeReadFinal, `"permission_denials":[]`, `"permission_denials":[{"tool_name":"Read","tool_input":{"file_path":"synthetic-private-test-value"}}]`, 1)
	stream := strings.Join([]string{claudeReadInit, claudeReadVerdict, final}, "\n")
	_, err := validateClaudeReadEvents([]byte(stream), map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}, nil)
	if err == nil || !strings.Contains(err.Error(), "permission denial") || strings.Contains(err.Error(), "synthetic-private-test-value") {
		t.Fatalf("expected a fixed permission-denial error, got %v", err)
	}
}

func TestValidateClaudeReadEventsRejectsOutputTokenLimit(t *testing.T) {
	for _, tc := range []struct{ name, verdict, final string }{
		{"terminal", claudeReadVerdict, strings.Replace(claudeReadFinal, `"subtype":`, `"stop_reason":"max_tokens","subtype":`, 1)},
		{"assistant", strings.Replace(claudeReadVerdict, `"role":"assistant"`, `"role":"assistant","stop_reason":"max_tokens"`, 1), claudeReadFinal},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stream := strings.Join([]string{claudeReadInit, claudeReadCall, claudeReadReceipt, tc.verdict, tc.final}, "\n")
			if _, err := validateClaudeReadEvents([]byte(stream), map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}, nil); err == nil {
				t.Fatal("accepted output cut by the token limit")
			}
		})
	}
}

func TestClaudeSupplementalReadCannotReplaceMandatoryEvidence(t *testing.T) {
	s := reviewSnapshotFixture(t, "first\nsecond\n")
	path := filepath.Join(s.checkout, "candidate.txt")
	expected := map[string][]byte{"/tmp/evidence/spec.txt": []byte("first\nsecond\n")}
	call := strings.NewReplacer("toolu_1", "toolu_2", "/tmp/evidence/spec.txt", path).Replace(claudeReadCall)
	receipt := strings.NewReplacer("toolu_1", "toolu_2", "/tmp/evidence/spec.txt", path).Replace(claudeReadReceipt)
	for _, tc := range []struct {
		name, required, call, receipt string
		valid                         bool
	}{
		{"verified", claudeReadCall + "\n" + claudeReadReceipt, call, receipt, true},
		{"missing required read", "", call, receipt, false},
		{"wrong pin bytes", claudeReadCall + "\n" + claudeReadReceipt, call, strings.ReplaceAll(receipt, "first", "wrong"), false},
		{"partial file", claudeReadCall + "\n" + claudeReadReceipt, strings.Replace(call, `"file_path":`, `"limit":1,"file_path":`, 1), receipt, false},
		{"unknown selector", claudeReadCall + "\n" + claudeReadReceipt, strings.Replace(call, `"file_path":`, `"pages":"1","file_path":`, 1), receipt, false},
		{"unfinished supplemental", claudeReadCall + "\n" + claudeReadReceipt, call, "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head}
			stream := strings.Join([]string{claudeReadInit, tc.required, tc.call, tc.receipt, claudeReadVerdict, claudeReadFinal}, "\n")
			result, err := validateClaudeReadEvents([]byte(stream), expected, &r)
			if (err == nil) != tc.valid || (result != nil) != tc.valid {
				t.Fatalf("result %q, error %v", result, err)
			}
		})
	}
}
