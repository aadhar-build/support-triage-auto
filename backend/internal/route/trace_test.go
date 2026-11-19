package route

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestPyJSON(t *testing.T) {
	rv := "v1.2-draft"
	rep03 := RoutedThread{
		ThreadID:    59,
		Labels:      Labels{"delivery", "high", "escalate", 0.6},
		CheapLabels: Labels{"delivery", "normal", "ask", 0.6},
		Tiers:       []string{"cheap"},
		Steps:       []TierStep{{"cheap", "keywords", Labels{"delivery", "normal", "ask", 0.6}}},
		ReasonCodes: []string{"cheap_confident", "repeat_contact"},
		TopicID:     []string{},
	}
	run := RunInfo{CodeVersion: "0000000000000000000000000000000000000000", RulesVersion: &rv, Threshold: 0.5, Split: "synthetic"}
	cases := []struct {
		in   any
		want string
	}{
		{BuildRecord(rep03, run), `{"code_dirty": false, "code_version": "0000000000000000000000000000000000000000", "drift_config_version": null, "final": {"action": "escalate", "confidence": 0.6, "intent": "delivery", "urgency": "high"}, "reason_codes": ["cheap_confident", "repeat_contact"], "rules_version": "v1.2-draft", "seed": 0, "split": "synthetic", "thread_id": 59, "threshold": 0.5, "tiers": [{"action": "ask", "confidence": 0.6, "intent": "delivery", "model_id": "keywords", "tier": "cheap", "urgency": "normal"}], "topic_id": [], "trace_schema": 2}`},
		{1.0, `1.0`},
		{0.6, `0.6`},
		{0.75, `0.75`},
		{1e16, `1e+16`},
		{1.5e-05, `1.5e-05`},
		{0.0001, `0.0001`},
		{123456789012345.6, `123456789012345.6`},
		{"\u00e9", `"\u00e9"`},
		{" ", `" "`},
		{"\u00a0", `"\u00a0"`},
		{"\U0001F600", `"\ud83d\ude00"`},
		{"\x1c", `"\u001c"`},
		{"\x7f", `"\u007f"`},
		{"</a>&", `"</a>&"`},
		{`"`, `"\""`},
		{`\`, `"\\"`},
		{"a\nb\r\t\b\f", `"a\nb\r\t\b\f"`},
		{map[string]any{"b": map[string]any{"z": 1, "a": nil}, "a": []any{true, false}}, `{"a": [true, false], "b": {"a": null, "z": 1}}`},
		{nil, `null`},
		{[]string{}, `[]`},
		{int64(-3), `-3`},
		{uint64(18446744073709551615), `18446744073709551615`},
	}
	for _, c := range cases {
		got, err := pyjson(c.in)
		if err != nil || got != c.want {
			t.Errorf("pyjson(%#v) = %s, %v\nwant %s", c.in, got, err, c.want)
		}
	}
	for _, bad := range []any{nanF(), map[string]any{"x": infF()}} {
		if _, err := pyjson(bad); err == nil {
			t.Errorf("pyjson(%v) accepted", bad)
		}
	}
}

func nanF() float64 { z := 0.0; return z / z }
func infF() float64 { z := 0.0; return 1 / z }

var testRun = RunInfo{CodeVersion: strings.Repeat("a", 40), Threshold: 0.5, Split: "dev", Seed: 3}

func routedRec(t *testing.T, rootID uint64) Record {
	t.Helper()
	c, l, a := fakes()
	r := mustRoute(t, thread(rootID, "hello SENTINEL_TEXT"), c, l, a, 0.5, nil, nil)
	return BuildRecord(r, testRun)
}

func forcedRec(t *testing.T) Record {
	t.Helper()
	c, l, a := fakes()
	r := mustRoute(t, thread(22, "thanks, quuxfrob"), c, l, a, 0.5, topics, nil)
	return BuildRecord(r, testRun)
}

var sortedRecordKeys = []string{"code_dirty", "code_version", "drift_config_version", "final", "reason_codes", "rules_version", "seed", "split", "thread_id", "threshold", "tiers", "topic_id", "trace_schema"}

func TestRecordKeys(t *testing.T) {
	rec := routedRec(t, 21)
	if !slices.Equal(sortedKeys(rec), sortedRecordKeys) {
		t.Errorf("keys %v", sortedKeys(rec))
	}
	line, err := pyjson(rec)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"code_dirty": false, "code_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "drift_config_version": null, "final": {"action": "ask", "confidence": 1.0, "intent": "other", "urgency": "normal"}, "reason_codes": ["low_confidence_to_larger"], "rules_version": null, "seed": 3, "split": "dev", "thread_id": 21, "threshold": 0.5, "tiers": [{"action": "ask", "confidence": 0.2, "intent": "other", "model_id": "keywords", "tier": "cheap", "urgency": "normal"}, {"action": "ask", "confidence": 1.0, "intent": "other", "model_id": "fake-larger", "tier": "larger", "urgency": "normal"}, {"action": "ask", "confidence": 1.0, "intent": "other", "model_id": "fake-agent", "tier": "agent", "urgency": "normal"}], "topic_id": [], "trace_schema": 2}`
	if line != want {
		t.Errorf("got  %s\nwant %s", line, want)
	}
	if strings.Contains(line, "SENTINEL") {
		t.Error("text leaked")
	}
	if err := ValidateRecord(rec); err != nil {
		t.Error(err)
	}
}

func wantErr(t *testing.T, rec any, msg string) {
	t.Helper()
	err := ValidateRecord(rec)
	var te *TraceError
	if err == nil || !errors.As(err, &te) || err.Error() != msg {
		t.Errorf("ValidateRecord err = %v, want %q", err, msg)
		return
	}
	if strings.Contains(msg, "\n") || len(strings.Split(err.Error(), "\n")) != 1 {
		t.Errorf("not one line: %q", err)
	}
}

func TestValidateMissingOrNull(t *testing.T) {
	for _, key := range sortedRecordKeys {
		rec := routedRec(t, 21)
		delete(rec, key)
		where := "trace record for thread 21: "
		if key == "thread_id" {
			where = "trace record for thread None: "
		}
		wantErr(t, rec, where+"'"+key+"' is missing")
		if key == "rules_version" || key == "drift_config_version" {
			continue
		}
		rec = routedRec(t, 21)
		rec[key] = nil
		wantErr(t, rec, "trace record for thread "+map[bool]string{true: "None", false: "21"}[key == "thread_id"]+": '"+key+"' is null")
	}
	p := "trace record for thread 21: "
	rec := routedRec(t, 21)
	rec["tiers"] = []any{}
	wantErr(t, rec, p+"'tiers' is not a non-empty list")
	rec = routedRec(t, 21)
	rec["tiers"].([]any)[0].(map[string]any)["model_id"] = nil
	wantErr(t, rec, p+"'model_id' is missing or null in tier entry 0")
	rec = routedRec(t, 21)
	delete(rec["tiers"].([]any)[1].(map[string]any), "confidence")
	wantErr(t, rec, p+"'confidence' is missing or null in tier entry 1")
	rec = routedRec(t, 21)
	rec["tiers"] = []any{"x"}
	wantErr(t, rec, p+"'tiers' entry 0 is not an object")
	rec = routedRec(t, 21)
	rec["final"] = "x"
	wantErr(t, rec, p+"'final' is not an object")
	rec = routedRec(t, 21)
	delete(rec["final"].(map[string]any), "urgency")
	wantErr(t, rec, p+"'urgency' is missing or null in 'final'")
	rec = routedRec(t, 21)
	rec["reason_codes"] = []string{}
	wantErr(t, rec, p+"'reason_codes' is not a non-empty list")
	rec = routedRec(t, 21)
	rec["reason_codes"] = []any{"nope"}
	wantErr(t, rec, p+"'reason_codes' holds an unregistered code 'nope'")
	rec = routedRec(t, 21)
	rec["reason_codes"] = []any{3}
	wantErr(t, rec, p+"'reason_codes' holds an unregistered code 3")
	for _, bad := range []any{1, true, 2.5, "2"} {
		rec = routedRec(t, 21)
		rec["trace_schema"] = bad
		wantErr(t, rec, p+"'trace_schema' is not 2")
	}
	for _, ok := range []any{2.0, int64(2), uint64(2)} {
		rec = routedRec(t, 21)
		rec["trace_schema"] = ok
		if err := ValidateRecord(rec); err != nil {
			t.Errorf("trace_schema %#v: %v", ok, err)
		}
	}
	wantErr(t, "x", "trace record is not an object")
	wantErr(t, nil, "trace record is not an object")
}

func TestValidateForcedAndTopicID(t *testing.T) {
	rec := forcedRec(t)
	if err := ValidateRecord(rec); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(rec["topic_id"].([]string), []string{"t-zorblax"}) {
		t.Errorf("topic_id %v", rec["topic_id"])
	}
	line, _ := pyjson(rec)
	if strings.Contains(line, "quuxfrob") {
		t.Error("text leaked")
	}
	p := "trace record for thread 22: "
	cases := []struct {
		forced bool
		val    any
		msg    string
	}{
		{false, "t-zorblax", "'topic_id' is not a list of kebab-case topic ids"},
		{true, []any{"Not_Kebab"}, "'topic_id' is not a list of kebab-case topic ids"},
		{true, []any{3}, "'topic_id' is not a list of kebab-case topic ids"},
		{true, []string{}, "'topic_id' is empty but reason_codes holds 'escalate_always'"},
		{false, []string{"t-x"}, "'topic_id' is not empty but reason_codes lacks 'escalate_always'"},
	}
	for _, c := range cases {
		var rec Record
		if c.forced {
			rec = forcedRec(t)
		} else {
			rec = routedRec(t, 22)
		}
		rec["topic_id"] = c.val
		wantErr(t, rec, p+c.msg)
	}
}

func TestValidateNullableVersions(t *testing.T) {
	r1, d1 := "rules-1", "drift-1"
	c, l, a := fakes()
	r := mustRoute(t, thread(21, "hello"), c, l, a, 0.5, nil, nil)
	rec := BuildRecord(r, RunInfo{strings.Repeat("b", 40), true, &r1, &d1, 0.7, "eval", 0})
	if rec["rules_version"] != "rules-1" || rec["drift_config_version"] != "drift-1" || rec["code_dirty"] != true {
		t.Errorf("rec %v", rec)
	}
	if err := ValidateRecord(rec); err != nil {
		t.Error(err)
	}
}

func gitT(t *testing.T, repo string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v %s", args, err, out)
	}
}

func TestCodeVersionCleanAndDirty(t *testing.T) {
	repo := t.TempDir()
	gitT(t, repo, "init", "-q")
	if err := os.WriteFile(filepath.Join(repo, "a.txt"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, repo, "add", "a.txt")
	gitT(t, repo, "commit", "-q", "-m", "init")
	commit, dirty, err := CodeVersion(repo)
	if err != nil || !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(commit) || dirty {
		t.Fatalf("clean: %q %v %v", commit, dirty, err)
	}
	os.WriteFile(filepath.Join(repo, "untracked.txt"), []byte("x\n"), 0o644)
	if c2, d2, err := CodeVersion(repo); err != nil || c2 != commit || d2 {
		t.Errorf("untracked: %q %v %v", c2, d2, err)
	}
	os.WriteFile(filepath.Join(repo, "a.txt"), []byte("two\n"), 0o644)
	if c2, d2, err := CodeVersion(repo); err != nil || c2 != commit || !d2 {
		t.Errorf("dirty: %q %v %v", c2, d2, err)
	}
}

func TestCodeVersionNotARepo(t *testing.T) {
	dir := t.TempDir()
	_, _, err := CodeVersion(dir)
	var te *TraceError
	if err == nil || !errors.As(err, &te) || !strings.HasPrefix(err.Error(), "git rev-parse failed in "+dir+": ") {
		t.Errorf("err %v", err)
	}
}

// stubGit puts a fake git script first on PATH.
func stubGit(t *testing.T, script string) {
	t.Helper()
	dir := t.TempDir()
	if script != "" {
		if err := os.WriteFile(filepath.Join(dir, "git"), []byte("#!/bin/sh\n"+script+"\n"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
}

func TestCodeVersionStubs(t *testing.T) {
	repo := t.TempDir()
	stubGit(t, "")
	if _, _, err := CodeVersion(repo); err == nil || err.Error() != "git is not installed; cannot read the code version" {
		t.Errorf("not installed: %v", err)
	}
	stubGit(t, "echo abc")
	if _, _, err := CodeVersion(repo); err == nil || err.Error() != "git rev-parse HEAD in "+repo+" did not return a 40-hex commit" {
		t.Errorf("not 40-hex: %v", err)
	}
	stubGit(t, "echo 'fatal: bad thing' >&2; echo second >&2; exit 3")
	if _, _, err := CodeVersion(repo); err == nil || err.Error() != "git rev-parse failed in "+repo+": fatal: bad thing" {
		t.Errorf("failed: %v", err)
	}
	stubGit(t, "exit 4")
	if _, _, err := CodeVersion(repo); err == nil || err.Error() != "git rev-parse failed in "+repo+": exit 4" {
		t.Errorf("exit: %v", err)
	}
	old := gitTimeout
	gitTimeout = 200 * time.Millisecond
	t.Cleanup(func() { gitTimeout = old })
	stubGit(t, "exec /usr/bin/sleep 30")
	start := time.Now()
	if _, _, err := CodeVersion(repo); err == nil || err.Error() != "git rev-parse timed out in "+repo {
		t.Errorf("timeout: %v", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Errorf("timeout took %v", time.Since(start))
	}
}

func realDir(t *testing.T) string {
	t.Helper()
	d, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func records(recs ...Record) func(func(Record) bool) {
	return func(yield func(Record) bool) {
		for _, r := range recs {
			if !yield(r) {
				return
			}
		}
	}
}

func listDir(t *testing.T, dir string) []string {
	t.Helper()
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range ents {
		names = append(names, e.Name())
	}
	return names
}

func TestWriteTraceOK(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	dest := filepath.Join(out, "t.jsonl")
	n, err := WriteTrace(repo, dest, records(routedRec(t, 1), routedRec(t, 2)))
	if err != nil || n != 2 {
		t.Fatalf("%d %v", n, err)
	}
	b, _ := os.ReadFile(dest)
	lines := strings.Split(strings.TrimSuffix(string(b), "\n"), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], `"thread_id": 1,`) || !strings.Contains(lines[1], `"thread_id": 2,`) {
		t.Errorf("lines %q", lines)
	}
	if !slices.Equal(listDir(t, out), []string{"t.jsonl"}) {
		t.Errorf("dir %v", listDir(t, out))
	}
	if fi, _ := os.Stat(dest); fi.Mode().Perm() != 0o600 {
		t.Errorf("mode %v", fi.Mode())
	}
}

func TestWriteTraceRefusesInsideRepo(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	if err := os.Symlink(repo, filepath.Join(out, "link")); err != nil {
		t.Fatal(err)
	}
	cases := []struct{ path, resolved string }{
		{filepath.Join(repo, "t.jsonl"), repo + "/t.jsonl"},
		{repo, repo},
		{filepath.Join(out, "link", "t.jsonl"), repo + "/t.jsonl"},
	}
	for _, c := range cases {
		msg := "trace path " + c.resolved + " is inside the repository " + repo + "; write it outside"
		if err := CheckTracePath(repo, c.path); err == nil || err.Error() != msg {
			t.Errorf("CheckTracePath(%s) = %v", c.path, err)
		}
		if _, err := WriteTrace(repo, c.path, records(routedRec(t, 1))); err == nil || err.Error() != msg {
			t.Errorf("WriteTrace(%s) = %v", c.path, err)
		}
	}
	if len(listDir(t, repo)) != 0 {
		t.Errorf("repo written: %v", listDir(t, repo))
	}
	// A sibling whose name shares the root's prefix is outside.
	if err := CheckTracePath(repo, repo+"2/t.jsonl"); err != nil {
		t.Errorf("sibling: %v", err)
	}
}

func TestWriteTraceMissingDir(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	_, err := WriteTrace(repo, filepath.Join(out, "nope", "t.jsonl"), records(routedRec(t, 1)))
	if err == nil || !strings.HasPrefix(err.Error(), "cannot write trace in "+out+"/nope: ") {
		t.Errorf("err %v", err)
	}
	if _, err := os.Stat(filepath.Join(out, "nope")); !os.IsNotExist(err) {
		t.Error("parent created")
	}
}

func TestWriteTraceAtomic(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	dest := filepath.Join(out, "t.jsonl")
	bad := routedRec(t, 3)
	delete(bad, "final")
	_, err := WriteTrace(repo, dest, records(routedRec(t, 1), routedRec(t, 2), bad, routedRec(t, 4)))
	if err == nil || err.Error() != "trace record for thread 3: 'final' is missing" {
		t.Errorf("err %v", err)
	}
	if len(listDir(t, out)) != 0 {
		t.Errorf("dir %v", listDir(t, out))
	}
	os.WriteFile(dest, []byte("old content\n"), 0o644)
	bad = routedRec(t, 2)
	bad["reason_codes"] = []any{"made_up_code"}
	if _, err := WriteTrace(repo, dest, records(routedRec(t, 1), bad)); err == nil {
		t.Error("accepted bad record")
	}
	if b, _ := os.ReadFile(dest); string(b) != "old content\n" {
		t.Errorf("dest changed: %q", b)
	}
	if !slices.Equal(listDir(t, out), []string{"t.jsonl"}) {
		t.Errorf("dir %v", listDir(t, out))
	}
}

// TestResolveLikePython: Python's non-strict Path.resolve() resolves `link`
// before applying the following `..`.
func TestResolveLikePython(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	if err := os.MkdirAll(filepath.Join(repo, "a", "b"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(repo, "a", "b"), filepath.Join(out, "link")); err != nil {
		t.Fatal(err)
	}
	// <out>/link/../x resolves to <repo>/a/x, inside the repo.
	msg := "trace path " + repo + "/a/x is inside the repository " + repo + "; write it outside"
	if err := CheckTracePath(repo, out+"/link/../x"); err == nil || err.Error() != msg {
		t.Errorf("link/..: %v", err)
	}
	// A relative path resolves against the working directory.
	t.Chdir(out)
	if err := CheckTracePath(repo, "link/../x"); err == nil || err.Error() != msg {
		t.Errorf("relative link/..: %v", err)
	}
	// A destination that doesn't exist yet under an existing dir is fine.
	if err := CheckTracePath(repo, out+"/new.jsonl"); err != nil {
		t.Errorf("new file: %v", err)
	}
	// A non-existent dir followed by .. is applied lexically.
	if err := CheckTracePath(repo, out+"/missing/../new.jsonl"); err != nil {
		t.Errorf("missing/..: %v", err)
	}
	// The repo root is resolved too: a symlink to the repo as root.
	if err := os.Symlink(repo, filepath.Join(out, "rootlink")); err != nil {
		t.Fatal(err)
	}
	if err := CheckTracePath(out+"/rootlink", repo+"/t.jsonl"); err == nil || err.Error() != "trace path "+repo+"/t.jsonl is inside the repository "+repo+"; write it outside" {
		t.Errorf("root symlink: %v", err)
	}
}

// A symlink loop is refused, as Python's resolve raises on one, rather than
// left unresolved.
func TestResolveSymlinkLoopRefused(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	if err := os.Symlink(filepath.Join(out, "b"), filepath.Join(out, "a")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(out, "a"), filepath.Join(out, "b")); err != nil {
		t.Fatal(err)
	}
	err := CheckTracePath(repo, out+"/a/t.jsonl")
	var te *TraceError
	if err == nil || !errors.As(err, &te) || !strings.Contains(err.Error(), "symlink loop") {
		t.Errorf("loop: %v", err)
	}
	if _, err := WriteTrace(repo, out+"/a/t.jsonl", records(routedRec(t, 1))); err == nil {
		t.Error("WriteTrace accepted a looping path")
	}
}
