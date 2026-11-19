package main

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
)

const codexReadTranscript = `{"type":"thread.started","thread_id":"thread-fixture"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/cat -- /fixture/spec","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/cat -- /fixture/spec","aggregated_output":"BEGIN spec\ncomplete spec\nEND spec\n","exit_code":0,"status":"completed"}}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/cat -- /fixture/patch","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/cat -- /fixture/patch","aggregated_output":"BEGIN patch\ncomplete patch\nEND patch\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"VERDICT: PASS\n\n## Summary\n- Reviewed the fixture.\n"}}
{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":0,"output_tokens":25}}
`

func TestCodexReadEventsRequiresExactCompleteReadsBeforeVerdict(t *testing.T) {
	expected := map[string][]byte{
		"/bin/cat -- /fixture/spec":  []byte("BEGIN spec\ncomplete spec\nEND spec\n"),
		"/bin/cat -- /fixture/patch": []byte("BEGIN patch\ncomplete patch\nEND patch\n"),
	}
	for _, stream := range []string{
		codexReadTranscript,
		strings.ReplaceAll(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/sh -c '/bin/cat -- /fixture/spec'`),
	} {
		result, err := validateCodexReadEvents([]byte(stream), expected, nil)
		if err != nil {
			t.Fatal(err)
		}
		if want := "VERDICT: PASS\n\n## Summary\n- Reviewed the fixture.\n"; string(result) != want {
			t.Fatalf("verdict %q, want %q", result, want)
		}
	}

	lines := strings.Split(codexReadTranscript, "\n")
	for _, tc := range []struct {
		name, stream string
	}{
		{"missing read", withoutTranscriptLine(codexReadTranscript, 5)},
		{"command has no start", withoutTranscriptLine(codexReadTranscript, 2)},
		{"missing verdict", withoutTranscriptLine(codexReadTranscript, 6)},
		{"verdict before final read", strings.Join(append(append(append([]string{}, lines[:5]...), lines[6], lines[5]), lines[7:]...), "\n")},
		{"wrong completion ID", strings.Replace(codexReadTranscript, `"item.completed","item":{"id":"item_0"`, `"item.completed","item":{"id":"item_9"`, 1)},
		{"changed command", strings.Replace(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/cat -- /fixture/other`, 1)},
		{"login shell wrapper", strings.ReplaceAll(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/sh -lc '/bin/cat -- /fixture/spec'`)},
		{"wrapper includes another command", strings.ReplaceAll(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/sh -c '/bin/cat -- /fixture/spec && true'`)},
		{"wrapper missing content and closing quote", strings.ReplaceAll(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/sh -c '`)},
		{"mixed direct and wrapped command", strings.Replace(codexReadTranscript, `/bin/cat -- /fixture/spec`, `/bin/sh -c '/bin/cat -- /fixture/spec'`, 1)},
		{"repeated command", strings.ReplaceAll(codexReadTranscript, `/bin/cat -- /fixture/patch`, `/bin/cat -- /fixture/spec`)},
		{"repeated ID", strings.ReplaceAll(codexReadTranscript, `"item_1"`, `"item_0"`)},
		{"repeated completion", strings.Replace(codexReadTranscript, lines[5], lines[5]+"\n"+lines[5], 1)},
		{"one last byte missing", strings.Replace(codexReadTranscript, `END patch\n`, `END patch`, 1)},
		{"truncated middle with markers", strings.Replace(codexReadTranscript, `complete patch`, `...`, 1)},
		{"in_progress result", strings.Replace(codexReadTranscript, `"status":"completed"`, `"status":"in_progress"`, 1)},
		{"failed status", strings.Replace(codexReadTranscript, `"status":"completed"`, `"status":"failed"`, 1)},
		{"nonzero exit", strings.Replace(codexReadTranscript, `"exit_code":0`, `"exit_code":1`, 1)},
		{"denial event", strings.Replace(codexReadTranscript, lines[6], `{"type":"error","message":"permission denied"}`+"\n"+lines[6], 1)},
		{"unknown event", strings.Replace(codexReadTranscript, lines[6], `{"type":"turn.changed"}`+"\n"+lines[6], 1)},
		{"duplicate field", strings.Replace(codexReadTranscript, `"exit_code":0`, `"exit_code":1,"exit_code":0`, 1)},
		{"malformed event", strings.Replace(codexReadTranscript, lines[4], `{"type":`, 1)},
		{"missing turn completion", withoutTranscriptLine(codexReadTranscript, 7)},
		{"truncated turn completion", strings.TrimSuffix(codexReadTranscript, "}\n")},
		{"event after completion", codexReadTranscript + lines[6] + "\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := validateCodexReadEvents([]byte(tc.stream), expected, nil)
			if err == nil || got != nil {
				t.Fatalf("expected refusal without a verdict, got %q and %v", got, err)
			}
		})
	}
}

func withoutTranscriptLine(stream string, line int) string {
	lines := strings.Split(stream, "\n")
	return strings.Join(append(lines[:line], lines[line+1:]...), "\n")
}

func TestCodexReadEventsAdvisoryVerdictMayFollowIncompleteReads(t *testing.T) {
	lines := strings.Split(codexReadTranscript, "\n")
	const reasoning = `{"type":"item.completed","item":{"id":"note-1","type":"reasoning","text":""}}`
	const commentary = `{"type":"item.completed","item":{"id":"note-2","type":"agent_message","text":"I am checking the available evidence."}}`
	const answer = `{"type":"item.completed","item":{"id":"answer","type":"agent_message","text":"VERDICT: CHANGES\r\n\r\n## Summary\r\n- Missing evidence.\r\n"}}`
	expected := map[string][]byte{
		"/bin/cat -- /fixture/spec":  []byte("BEGIN spec\ncomplete spec\nEND spec\n"),
		"/bin/cat -- /fixture/patch": []byte("BEGIN patch\ncomplete patch\nEND patch\n"),
	}
	stream := strings.Join([]string{lines[0], lines[1], reasoning, commentary, lines[2], lines[3], answer, lines[7], ""}, "\n")
	content, err := validateCodexReadEvents([]byte(stream), expected, nil)
	if err != nil || !strings.HasPrefix(string(content), "VERDICT: CHANGES\r\n") {
		t.Fatalf("wanted an advisory verdict, got %q and %v", content, err)
	}
	for _, bad := range []string{
		strings.Replace(stream, lines[3]+"\n", "", 1),
		strings.Replace(stream, answer+"\n", "", 1),
		strings.Replace(stream, answer, answer+"\n"+commentary, 1),
		strings.Replace(stream, "VERDICT: CHANGES", "VERDICT: PASS", 1),
		strings.Replace(stream, "VERDICT: CHANGES", "APPROVE", 1),
	} {
		if got, err := validateCodexReadEvents([]byte(bad), expected, nil); err == nil || got != nil {
			t.Fatalf("expected refusal without a verdict, got %q and %v", got, err)
		}
	}
}

func TestCodexSupplementalReadCannotReplaceMandatoryEvidence(t *testing.T) {
	s := reviewSnapshotFixture(t, "candidate")
	expected := map[string][]byte{"/bin/cat -- /fixture/spec": []byte("BEGIN spec\ncomplete spec\nEND spec\n")}
	path := filepath.Join(s.checkout, "AGENTS.md")
	stream := strings.ReplaceAll(codexReadTranscript, "/fixture/patch", path)
	stream = strings.Replace(stream, `BEGIN patch\ncomplete patch\nEND patch\n`, `# Fixture repository rules\n`, 1)
	lines := strings.Split(stream, "\n")
	for _, tc := range []struct {
		name, stream string
		valid        bool
	}{
		{"verified", stream, true},
		{"wrong pinned bytes", strings.Replace(stream, "Fixture repository rules", "changed rules", 1), false},
		{"missing required read", strings.Join(append(append([]string{}, lines[:2]...), lines[4:]...), "\n"), false},
		{"unfinished supplemental", withoutTranscriptLine(stream, 5), false},
		{"two inputs", strings.ReplaceAll(stream, path, path+" "+path), false},
		{"shell operator", strings.ReplaceAll(stream, path, path+" | cat"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head}
			result, err := validateCodexReadEvents([]byte(tc.stream), expected, &r)
			if (err == nil) != tc.valid || (result != nil) != tc.valid {
				t.Fatalf("result %q, error %v", result, err)
			}
		})
	}
}
