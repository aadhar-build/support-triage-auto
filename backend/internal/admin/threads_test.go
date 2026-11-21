package admin

import (
	"encoding/json"
	"maps"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// baseRecord is a made-up synthetic trace record the Rules tier decided.
const baseRecord = `{"code_dirty": false, "code_version": "0000000000000000000000000000000000000000", "drift_config_version": null, ` +
	`"final": {"action": "route", "confidence": 0.8, "intent": "delivery", "urgency": "normal"}, "reason_codes": ["cheap_confident"], ` +
	`"rules_version": "fixture-rules-v3", "seed": 7, "split": "synthetic", "threshold": 0.5, "thread_id": 1, ` +
	`"tiers": [{"action": "route", "confidence": 0.8, "intent": "delivery", "model_id": "keywords", "tier": "cheap", "urgency": "normal"}], ` +
	`"topic_id": [], "trace_schema": 2}`

func traceRec(t *testing.T, id any, over map[string]any) map[string]any {
	t.Helper()
	var r map[string]any
	if err := json.Unmarshal([]byte(baseRecord), &r); err != nil {
		t.Fatal(err)
	}
	r["thread_id"] = id
	maps.Copy(r, over)
	return r
}

func tierRec(tier string, modelID any, action string) map[string]any {
	return map[string]any{"tier": tier, "model_id": modelID, "intent": "delivery", "urgency": "normal", "action": action, "confidence": 0.8}
}

func finalRec(action, urgency string, confidence float64) map[string]any {
	return map[string]any{"intent": "delivery", "urgency": urgency, "action": action, "confidence": confidence}
}

func jsonl(t *testing.T, recs ...map[string]any) string {
	t.Helper()
	var b strings.Builder
	for _, r := range recs {
		line, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		b.Write(line)
		b.WriteByte('\n')
	}
	return b.String()
}

// writeSyntheticRun writes an ok synthetic run named name whose trace is body.
func writeSyntheticRun(t *testing.T, runs, name, body string) string {
	t.Helper()
	d := makeRun(t, runs, name, goodManifest(), nil)
	writeFile(t, d, "trace.jsonl", body)
	return d
}

func fixtureServer(t *testing.T) *httptest.Server {
	t.Helper()
	runs := t.TempDir()
	writeSyntheticRun(t,runs, "syn", readFixture(t, "trace_synthetic.jsonl"))
	return testServer(t, runsCfg(t, runs))
}

func unusableBody(name string) string {
	return "{\n  \"name\": \"" + name + "\",\n  \"reason\": \"no usable trace file\",\n  \"status\": \"ok\",\n  \"threads\": null\n}\n"
}

func TestReasonCopyCoversRegistry(t *testing.T) {
	b, err := os.ReadFile(filepath.Join(parity.RepoRoot(t), "web", "src", "reason-copy.json"))
	if err != nil {
		t.Fatal(err)
	}
	var copyText map[string]any
	if err := json.Unmarshal(b, &copyText); err != nil {
		t.Fatal(err)
	}
	for _, code := range route.ReasonCodes {
		if s, ok := copyText[code].(string); !ok || strings.TrimSpace(s) == "" {
			t.Errorf("reason code %q has no plain-language copy", code)
		}
	}
	for k := range copyText {
		if !slices.Contains(route.ReasonCodes, k) {
			t.Errorf("copy for %q, which is not a registry reason code", k)
		}
	}
}

func TestThreadDecidedBy(t *testing.T) {
	srv := fixtureServer(t)
	r := get(t, srv, "/api/runs/syn/threads/1")
	want := `{
  "decided_by": {
    "model_id": "keywords",
    "source": "cheap"
  },
  "final": {
    "action": "route",
    "confidence": 0.8,
    "intent": "delivery",
    "urgency": "normal"
  },
  "name": "syn",
  "reason_codes": [
    "cheap_confident"
  ],
  "thread_id": "1",
  "topic_id": []
}
`
	if r.status != 200 || r.body != want || r.header.Get("Content-Type") != "application/json; charset=utf-8" {
		t.Errorf("cheap: %d\n%s", r.status, r.body)
	}
	for _, c := range []struct{ id, name, want string }{
		{"2", "larger route", "  \"decided_by\": {\n    \"model_id\": \"fake-larger\",\n    \"source\": \"larger\"\n  },\n"},
		{"3", "actionable agent", "  \"decided_by\": {\n    \"model_id\": \"fake-agent\",\n    \"source\": \"agent\"\n  },\n"},
		{"4", "always-escalate over an answer draft", "  \"decided_by\": {\n    \"model_id\": null,\n    \"source\": \"escalate_always\"\n  },\n"},
		{"5", "always-escalate with repeat", "  \"decided_by\": {\n    \"model_id\": null,\n    \"source\": \"escalate_always\"\n  },\n"},
		{"6", "repeat escalation", "  \"decided_by\": {\n    \"model_id\": null,\n    \"source\": \"repeat_contact\"\n  },\n"},
		{"7", "repeat urgency-only", "  \"decided_by\": {\n    \"model_id\": \"fake-larger\",\n    \"source\": \"larger\"\n  },\n"},
	} {
		r := get(t, srv, "/api/runs/syn/threads/"+c.id)
		if r.status != 200 || !strings.Contains(r.body, c.want) {
			t.Errorf("%s: %d\n%s", c.name, r.status, r.body)
		}
	}
	r = get(t, srv, "/api/runs/syn/threads/7")
	if !strings.Contains(r.body, "  \"reason_codes\": [\n    \"high_risk_to_larger\",\n    \"repeat_contact\"\n  ],\n") {
		t.Errorf("urgency-only lost its repeat reason:\n%s", r.body)
	}
	r = get(t, srv, "/api/runs/syn/threads/4")
	if !strings.Contains(r.body, "  \"topic_id\": [\n    \"t-zorblax\"\n  ]\n") {
		t.Errorf("always-escalate topics:\n%s", r.body)
	}
	for _, field := range []string{"code_version", "seed", "threshold", "tiers", "split", "fake-agent"} {
		if strings.Contains(r.body, field) {
			t.Errorf("detail serves %q:\n%s", field, r.body)
		}
	}
}

func TestThreadDecidedByNull(t *testing.T) {
	runs := t.TempDir()
	cheap := tierRec("cheap", "keywords", "route")
	larger := tierRec("larger", "fake-larger", "route")
	writeSyntheticRun(t,runs, "syn", jsonl(t,
		traceRec(t, 20, nil),
		traceRec(t, 21, map[string]any{"tiers": []any{tierRec("cheap", "keywords", "answer")}, "final": finalRec("answer", "normal", 0.8)}),
		traceRec(t, 22, map[string]any{"reason_codes": []any{"low_confidence_to_larger"}, "tiers": []any{cheap, larger}, "final": finalRec("escalate", "normal", 0.8)}),
		traceRec(t, 23, map[string]any{"tiers": []any{cheap, larger}}),
		traceRec(t, 24, map[string]any{"tiers": []any{tierRec("mystery", "keywords", "route")}}),
		traceRec(t, 25, map[string]any{"tiers": []any{tierRec("cheap", 7, "route")}}),
	))
	srv := testServer(t, runsCfg(t, runs))
	if r := get(t, srv, "/api/runs/syn/threads/20"); r.status != 200 || !strings.Contains(r.body, "\"source\": \"cheap\"") {
		t.Fatalf("control: %d\n%s", r.status, r.body)
	}
	for _, c := range []struct{ id, name string }{
		{"21", "missing agent"}, {"22", "wrong final action"}, {"23", "extra step"},
		{"24", "unknown tier name"}, {"25", "non-string model_id"},
	} {
		r := get(t, srv, "/api/runs/syn/threads/"+c.id)
		if r.status != 200 || !strings.HasPrefix(r.body, "{\n  \"decided_by\": null,\n  \"final\": {") {
			t.Errorf("%s: %d\n%s", c.name, r.status, r.body)
		}
	}
}

func TestThreadsRunEligibility(t *testing.T) {
	runs := t.TempDir()
	trace := jsonl(t, traceRec(t, 11, nil))
	good := writeSyntheticRun(t,runs, "syn", trace)
	writeFile(t, makeRun(t, runs, "gold", manifest("gold_random", "2026-10-01T00:00:00Z", 0.12, true), nil), "trace.jsonl", trace)
	writeFile(t, makeRun(t, runs, "broken", "{", nil), "trace.jsonl", trace)
	writeFile(t, makeRun(t, runs, "bare", nil, nil), "trace.jsonl", trace)
	if err := os.Symlink(good, filepath.Join(runs, "linked")); err != nil {
		t.Fatal(err)
	}
	srv := testServer(t, runsCfg(t, runs))
	if r := get(t, srv, "/api/runs/syn/threads"); r.status != 200 {
		t.Fatalf("control: %d %s", r.status, r.body)
	}
	if r := get(t, srv, "/api/runs/syn/threads/11"); r.status != 200 {
		t.Fatalf("control detail: %d %s", r.status, r.body)
	}
	for _, name := range []string{"nope", "gold", "broken", "bare", "linked", "%2e%2e"} {
		for _, p := range []string{"/api/runs/" + name + "/threads", "/api/runs/" + name + "/threads/11"} {
			if r := get(t, srv, p); r.status != 404 || r.body != "not found\n" || r.header.Get("Content-Type") != "text/plain; charset=utf-8" {
				t.Errorf("%s: %d %q", p, r.status, r.body)
			}
		}
	}
}

func TestThreadAbsentVsRunAbsent(t *testing.T) {
	srv := fixtureServer(t)
	if r := get(t, srv, "/api/runs/syn/threads/999"); r.status != 404 || r.body != "not found\n" {
		t.Errorf("absent thread: %d %q", r.status, r.body)
	}
	r := get(t, srv, "/api/runs/syn/threads")
	if r.status != 200 || !strings.HasPrefix(r.body, "{\n  \"name\": \"syn\",\n  \"status\": \"ok\",\n  \"threads\": [\n") {
		t.Fatalf("list: %d\n%s", r.status, r.body)
	}
	first := "    {\n      \"action\": \"route\",\n      \"reason_codes\": [\n        \"cheap_confident\"\n      ],\n      \"thread_id\": \"1\"\n    },\n"
	if !strings.Contains(r.body, first) || !strings.Contains(r.body, "\"thread_id\": \"7\"") || strings.Contains(r.body, "decided_by") {
		t.Errorf("list rows:\n%s", r.body)
	}
	if r := get(t, srv, "/api/runs/nope/threads/1"); r.status != 404 {
		t.Errorf("absent run: %d", r.status)
	}
}

func TestThreadsUnusableTrace(t *testing.T) {
	runs := t.TempDir()
	good := jsonl(t, traceRec(t, 11, nil))
	writeSyntheticRun(t,runs, "empty", "")
	makeRun(t, runs, "missing", goodManifest(), nil)
	writeSyntheticRun(t,runs, "malformed", good+"{\"thread_id\": 12\n")
	writeSyntheticRun(t,runs, "not-object", good+"[1]\n")
	writeSyntheticRun(t,runs, "blank-line", good+"\n"+jsonl(t, traceRec(t, 12, nil)))
	writeSyntheticRun(t,runs, "bad-utf8", strings.Replace(good, "fixture-rules-v3", "fixture-\xff", 1))
	writeSyntheticRun(t,runs, "duplicate", good+good)
	writeSyntheticRun(t,runs, "string-id", jsonl(t, traceRec(t, "11", nil)))
	writeSyntheticRun(t,runs, "bad-final", jsonl(t, traceRec(t, 11, map[string]any{"final": finalRec("reply", "normal", 0.8)})))
	writeSyntheticRun(t,runs, "mixed-split", jsonl(t, traceRec(t, 424242, nil), traceRec(t, 515151, map[string]any{"split": "dev"})))
	elsewhere := writeFile(t, t.TempDir(), "trace.jsonl", good)
	if err := os.Symlink(elsewhere, filepath.Join(makeRun(t, runs, "symlink", goodManifest(), nil), "trace.jsonl")); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(makeRun(t, runs, "fifo", goodManifest(), nil), "trace.jsonl"), 0o644); err != nil {
		t.Fatal(err)
	}
	srv := testServer(t, runsCfg(t, runs))

	if r := get(t, srv, "/api/runs/empty/threads"); r.status != 200 || r.body != "{\n  \"name\": \"empty\",\n  \"status\": \"ok\",\n  \"threads\": []\n}\n" {
		t.Errorf("empty: %d\n%s", r.status, r.body)
	}
	for _, name := range []string{"missing", "malformed", "not-object", "blank-line", "bad-utf8", "duplicate",
		"string-id", "bad-final", "mixed-split", "symlink", "fifo"} {
		if r := get(t, srv, "/api/runs/"+name+"/threads"); r.status != 200 || r.body != unusableBody(name) {
			t.Errorf("%s list: %d\n%s", name, r.status, r.body)
		}
		if r := get(t, srv, "/api/runs/"+name+"/threads/11"); r.status != 404 || r.body != "not found\n" {
			t.Errorf("%s detail: %d %q", name, r.status, r.body)
		}
	}
	for _, p := range []string{"/api/runs/mixed-split/threads", "/api/runs/mixed-split/threads/424242", "/api/runs/mixed-split/threads/515151"} {
		b := get(t, srv, p).body
		if strings.Contains(b, "424242") || strings.Contains(b, "515151") {
			t.Errorf("%s leaks a thread id: %s", p, b)
		}
	}

	t.Run("byte cap", func(t *testing.T) {
		old := traceMaxBytes
		t.Cleanup(func() { traceMaxBytes = old })
		traceMaxBytes = int64(len(good))
		runs := t.TempDir()
		writeSyntheticRun(t,runs, "at-cap", good)
		writeSyntheticRun(t,runs, "over-cap", strings.TrimSuffix(good, "\n")+" \n")
		srv := testServer(t, runsCfg(t, runs))
		if r := get(t, srv, "/api/runs/at-cap/threads/11"); r.status != 200 {
			t.Errorf("at cap: %d %s", r.status, r.body)
		}
		if r := get(t, srv, "/api/runs/over-cap/threads"); r.status != 200 || r.body != unusableBody("over-cap") {
			t.Errorf("over cap list: %d\n%s", r.status, r.body)
		}
		if r := get(t, srv, "/api/runs/over-cap/threads/11"); r.status != 404 {
			t.Errorf("over cap detail: %d", r.status)
		}
	})
}

func TestThreadIDAbove2p53(t *testing.T) {
	runs := t.TempDir()
	writeSyntheticRun(t,runs, "big", jsonl(t, traceRec(t, uint64(9007199254740993), map[string]any{
		"reason_codes": []any{"low_confidence_to_larger"},
		"tiers":        []any{tierRec("cheap", "keywords", "route"), tierRec("larger", "fake-larger", "route")},
		"final":        finalRec("route", "normal", 0.875),
	})))
	srv := testServer(t, runsCfg(t, runs))
	want := `{
  "name": "big",
  "status": "ok",
  "threads": [
    {
      "action": "route",
      "reason_codes": [
        "low_confidence_to_larger"
      ],
      "thread_id": "9007199254740993"
    }
  ]
}
`
	if r := get(t, srv, "/api/runs/big/threads"); r.status != 200 || r.body != want {
		t.Errorf("list: %d\n%s", r.status, r.body)
	}
	r := get(t, srv, "/api/runs/big/threads/9007199254740993")
	if r.status != 200 || !strings.Contains(r.body, "  \"thread_id\": \"9007199254740993\",\n") ||
		!strings.Contains(r.body, "    \"confidence\": 0.875,\n") {
		t.Errorf("detail: %d\n%s", r.status, r.body)
	}
	if r := get(t, srv, "/api/runs/big/threads/9007199254740992"); r.status != 404 {
		t.Errorf("neighbour id: %d", r.status)
	}
}

func TestThreadsGuards(t *testing.T) {
	runs := t.TempDir()
	writeSyntheticRun(t,runs, "syn", jsonl(t, traceRec(t, 1, nil)))
	srv := testServer(t, runsCfg(t, runs))
	for _, p := range []string{"/api/runs/syn/threads", "/api/runs/syn/threads/1"} {
		if r := get(t, srv, p); r.status != 200 {
			t.Fatalf("control %s: %d", p, r.status)
		}
		if r := raw(t, srv, "POST", p, hostOf(srv), "HTTP/1.1"); r.status != 405 || r.body != "method not allowed: this console is read-only\n" {
			t.Errorf("POST %s: %d %q", p, r.status, r.body)
		}
		if r := raw(t, srv, "GET", p, str("evil.example"), "HTTP/1.1"); r.status != 403 {
			t.Errorf("foreign host %s: %d", p, r.status)
		}
	}
	for _, id := range []string{"01", "+1", "18446744073709551616", "-1", "1.0", "1e0", "%31", ""} {
		p := "/api/runs/syn/threads/" + id
		if r := get(t, srv, p); r.status != 404 || r.body != "not found\n" {
			t.Errorf("%s: %d %q", p, r.status, r.body)
		}
	}
	for _, p := range []string{"/api/runs/syn/threads/1/x", "/api/runs/syn/thread", "/api/runs//threads"} {
		if r := get(t, srv, p); r.status != 404 {
			t.Errorf("%s: %d", p, r.status)
		}
	}
}
