package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestReviewReadPlanPreservesEveryEvidenceByteOutsideInstructions(t *testing.T) {
	s := reviewSnapshotFixture(t, "candidate edit\n")
	// Put the first CRLF across the normal frame boundary.
	spec := strings.Repeat("x", maxReviewReadBytes-1024-1) + "\r\n" +
		strings.Repeat("fixture Δ line\r\n", 350) + "OWNER ANSWER FIXTURE"
	write(t, filepath.Join(s.job, "spec.md"), spec)
	if err := s.writeReviewInputs(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := s.prepareReviewReads(map[string]string{"job.json": "semantic digest", "spec.md": contentDigest([]byte(spec))}); err != nil {
		t.Fatal(err)
	}
	var got []byte
	for _, read := range s.reviewReads {
		disk, err := os.ReadFile(read.path)
		if err != nil || !bytes.Equal(disk, read.data) {
			t.Fatalf("bounded read does not match generated file: %v", err)
		}
		if len(disk) > maxReviewReadBytes {
			t.Fatalf("read too large: %d bytes", len(disk))
		}
		if read.name == "spec.md" {
			_, contents, found := bytes.Cut(disk, []byte("\n"))
			payload, ends := bytes.CutSuffix(contents, []byte("\nEnd of evidence part.\n"))
			if !found || !ends {
				t.Fatal("malformed file frame")
			}
			if bytes.HasSuffix(payload, []byte("\r")) {
				t.Fatal("frame split CRLF across two parts")
			}
			got = append(got, payload...)
		}
	}
	if string(got) != spec || len(s.reviewReads) < 4 {
		t.Fatal("read plan lost spec, diff or commit evidence")
	}
	prompt := s.prompt(jobState{}, config{}, roles["reviewer-code"], "Review fixture.")
	instructions := reviewReadInstructions(s.reviewReads, "codex")
	if strings.Contains(prompt+instructions, "OWNER ANSWER FIXTURE") || !strings.Contains(instructions, s.reviewReads[0].path) {
		t.Fatal("review instructions must contain paths without evidence text")
	}
}

func TestReviewReadPlanRejectsUnsafeControls(t *testing.T) {
	s := reviewSnapshotFixture(t, "candidate edit\n")
	write(t, filepath.Join(s.job, "spec.md"), "fixture spec\n")
	if err := s.writeReviewInputs(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, spec := range []string{"bare\rreturn", "trailing return\r", "extra return\r\r\n", "escape\x1b[2J"} {
		write(t, filepath.Join(s.job, "spec.md"), spec)
		err := s.prepareReviewReads(map[string]string{"spec.md": contentDigest([]byte(spec))})
		if err == nil || !strings.Contains(err.Error(), "unsupported text encoding or control characters") {
			t.Errorf("expected an unsupported control character error for %q, got %v", spec, err)
		}
	}
}

func TestReviewReadPlanRejectsChangedOrMissingInput(t *testing.T) {
	s := reviewSnapshotFixture(t, "candidate edit\n")
	write(t, filepath.Join(s.job, "spec.md"), "fixture spec")
	if err := s.writeReviewInputs(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, inputs := range []map[string]string{
		{"job.json": "semantic digest", "spec.md": contentDigest([]byte("older spec"))},
		{"job.json": "semantic digest", "spec.md": contentDigest([]byte("fixture spec")), "build.md": contentDigest(nil)},
	} {
		if err := s.prepareReviewReads(inputs); err == nil {
			t.Fatal("accepted missing or changed input")
		}
	}
}

func TestReviewStreamCaptureRefusesOversizedOutput(t *testing.T) {
	shell, err := exec.LookPath("sh")
	if err != nil {
		t.Fatal(err)
	}
	for _, redirect := range []string{"", " >&2"} {
		_, err := runNativeReview(context.Background(), shell, t.TempDir(), nil,
			[]string{"-c", "head -c 9000000 /dev/zero" + redirect}, "")
		if err == nil || !strings.Contains(err.Error(), "exceeded") {
			t.Fatalf("accepted oversized review event output: %v", err)
		}
	}
}

func TestReviewStreamRefusesFailedProcessWithoutExposingStderr(t *testing.T) {
	shell, err := exec.LookPath("sh")
	if err != nil {
		t.Fatal(err)
	}
	output, err := runNativeReview(context.Background(), shell, t.TempDir(), nil,
		[]string{"-c", "printf 'partial output'; printf 'private fixture diagnostic' >&2; exit 1"}, "")
	if output != nil || err == nil {
		t.Fatal("failed review process must not return output")
	}
	if strings.Contains(err.Error(), "private fixture diagnostic") {
		t.Fatal("review error exposed the worker's stderr")
	}
}

func TestExecuteReviewUsesObservedReadsForPositiveVerdict(t *testing.T) {
	for _, tc := range []struct{ harness, role, verdict string }{
		{"codex", "reviewer-code", "VERDICT: PASS"},
		{"codex", "approver", "APPROVE"},
		{"claude", "reviewer-code", "VERDICT: PASS"},
		{"claude", "approver", "APPROVE"},
	} {
		t.Run(tc.harness+"/"+tc.role, func(t *testing.T) {
			harness := tc.harness
			result := tc.verdict + "\n\n## Summary\n- Reviewed the fixture.\n"
			s := reviewSnapshotFixture(t, "candidate edit\n")
			s.temp = t.TempDir()
			const spec = "github owner answer fixture\r\n"
			write(t, filepath.Join(s.job, "spec.md"), spec)
			inputs := map[string]string{"spec.md": contentDigest([]byte(spec))}
			if tc.role == "approver" {
				const path = "round-1/review-code.md"
				const review = "VERDICT: PASS\r\n\r\n## Summary\r\n- Reviewed pinned evidence.\r\n"
				if err := os.Mkdir(filepath.Join(s.job, "round-1"), 0700); err != nil {
					t.Fatal(err)
				}
				write(t, filepath.Join(s.job, path), review)
				inputs[path] = contentDigest([]byte(review))
			}
			if err := s.writeReviewInputs(context.Background()); err != nil {
				t.Fatal(err)
			}
			if err := s.prepareReviewReads(inputs); err != nil {
				t.Fatal(err)
			}
			// Exercise the production wiring with a supplemental read of unchanged
			// repository rules in addition to all mandatory framed reads.
			reads := append([]reviewRead{}, s.reviewReads...)
			reads = append(reads, reviewRead{"rules", filepath.Join(s.checkout, "AGENTS.md"), []byte("# Fixture repository rules\n")})
			var events []string
			switch harness {
			case "codex":
				events = []string{`{"type":"thread.started","thread_id":"fixture"}`, `{"type":"turn.started"}`}
				for i, read := range reads {
					cmd := jsonString("/bin/bash -c '/bin/cat -- " + read.path + "'")
					item := fmt.Sprintf(`"type":"command_execution","id":"read-%d","command":%s`, i, cmd)
					events = append(events,
						`{"type":"item.started","item":{`+item+`,"status":"in_progress","exit_code":null,"aggregated_output":""}}`,
						`{"type":"item.completed","item":{`+item+`,"status":"completed","exit_code":0,"aggregated_output":`+jsonString(string(read.data))+`}}`)
				}
				events = append(events, `{"type":"item.completed","item":{"type":"agent_message","id":"answer","text":`+jsonString(result)+`}}`,
					`{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":0,"output_tokens":50}}`)
			case "claude":
				events = []string{`{"type":"system","subtype":"init","session_id":"fixture"}`}
				for i, read := range reads {
					id := fmt.Sprintf("read-%d", i)
					events = append(events,
						`{"type":"assistant","session_id":"fixture","parent_tool_use_id":null,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Read","id":`+jsonString(id)+`,"input":{"file_path":`+jsonString(read.path)+`}}]}}`)
					var rendered []string
					for j, line := range strings.Split(string(read.data), "\n") {
						rendered = append(rendered, fmt.Sprintf("%d\t%s", j+1, line))
					}
					metadata := fmt.Sprintf(`{"type":"text","file":{"filePath":%s,"content":%s,"startLine":1,"numLines":%d,"totalLines":%d}}`,
						jsonString(read.path), jsonString(string(read.data)), len(rendered), len(rendered))
					events = append(events,
						`{"type":"user","session_id":"fixture","parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":`+jsonString(id)+`,"content":`+jsonString(strings.Join(rendered, "\n"))+`}]}, "tool_use_result":`+metadata+`}`)
				}
				events = append(events,
					`{"type":"assistant","session_id":"fixture","parent_tool_use_id":null,"message":{"role":"assistant","content":[{"type":"text","text":`+jsonString(result)+`}]}}`,
					`{"type":"result","session_id":"fixture","subtype":"success","is_error":false,"permission_denials":[],"result":`+jsonString(result)+`}`)
			}
			flavor := "--json"
			startup := ""
			if harness == "claude" {
				flavor = "--output-format stream-json"
			} else {
				startup = "printf '%s\\n' 'WARNING: proceeding, even though we could not create PATH aliases: Operation not permitted (os error 1)' >&2\n"
			}
			fakeCLI(t, harness, `
prompt=$(cat)
case "$prompt" in *"github owner answer fixture"*) exit 11 ;; esac
case " $* " in *" `+flavor+` "*) ;; *) exit 12 ;; esac
case " $* " in *" --output-last-message "*) exit 13 ;; esac
`+startup+`printf '%s' `+fixtureEvents(events...))
			bin, err := exec.LookPath(harness)
			if err != nil {
				t.Fatal(err)
			}
			r := roles[tc.role]
			got, err := execute(context.Background(), bin, selection{Harness: harness, Model: "fixture"}, s, s.prompt(jobState{}, config{}, r, "Fixture role."), r)
			if err != nil || string(got) != result {
				t.Fatalf("expected only the verified review artifact, got %q and %v", got, err)
			}
		})
	}
}

func TestSupplementalReadsResolveOnlyPinnedRegularText(t *testing.T) {
	s := reviewSnapshotFixture(t, "pinned\n")
	dir := filepath.Join(s.checkout, "rules")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{
		"rules/default":    "this rule is evidence\n",
		"too-big":          strings.Repeat("x", maxSupplementalReadBytes+1),
		"binary":           "a\x00b",
		"invalid-utf":      "\xff",
		"terminal-control": "\x1b[2J",
		"at-cap":           strings.Repeat("x", maxSupplementalReadBytes),
		"empty":            "",
		"with space":       "file",
	} {
		write(t, filepath.Join(s.checkout, name), value)
	}
	symlink := filepath.Join(s.checkout, "tracked-link")
	if err := os.Symlink("candidate.txt", symlink); err != nil {
		t.Fatal(err)
	}
	gitTest(t, s.checkout, "add", "-A")
	gitTest(t, s.checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture files")
	s.head = gitTest(t, s.checkout, "rev-parse", "HEAD")
	// Even if HEAD and the working file move, the proof must use the dispatch SHA.
	write(t, filepath.Join(s.checkout, "candidate.txt"), "later\n")
	gitTest(t, s.checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qam", "later")
	write(t, filepath.Join(s.checkout, "untracked"), "not evidence")
	reads := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head}
	for name, want := range map[string]string{"candidate.txt": "pinned\n", "rules/default": "this rule is evidence\n", "empty": ""} {
		if got, ok := reads.resolve(filepath.Join(s.checkout, name)); !ok || string(got) != want {
			t.Errorf("read %s: wanted %q, got %q (%t)", name, want, got, ok)
		}
	}
	// Exercise pinned tree mode independently from the live symlink check.
	if err := os.Rename(symlink, filepath.Join(s.checkout, "live-link")); err != nil {
		t.Fatal(err)
	}
	write(t, symlink, "candidate.txt")
	if err := os.Rename(dir, dir+"-moved"); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("rules-moved", dir); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"rules/default", "tracked-link", "live-link", "untracked", ".git/config", "too-big", "binary",
		"invalid-utf", "terminal-control", "with space", "rules-moved", "./candidate.txt", "../other", "candidate.txt && pwd"} {
		if _, ok := reads.resolve(s.checkout + "/" + path); ok {
			t.Errorf("accepted supplemental read %q", path)
		}
	}
	if _, ok := reads.resolve(filepath.Join(t.TempDir(), "candidate.txt")); ok {
		t.Error("accepted path outside checkout")
	}

	for _, tc := range []struct {
		name        string
		path        string
		count, size int
	}{
		{"read count", "empty", maxSupplementalReads - 1, 0},
		{"total bytes", "at-cap", 0, maxSupplementalTotalBytes - maxSupplementalReadBytes},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := supplementalReviewReads{ctx: context.Background(), checkout: s.checkout, head: s.head, count: tc.count, bytes: tc.size}
			path := filepath.Join(s.checkout, tc.path)
			if _, ok := r.resolve(path); !ok {
				t.Fatal("refused exact budget")
			}
			if _, ok := r.resolve(path); ok {
				t.Fatal("exceeded budget")
			}
		})
	}
}
