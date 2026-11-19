package parity

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// recorder is a testing.TB that records failures instead of raising them.
// Fatalf ends the calling goroutine, like the real one.
type recorder struct {
	testing.TB
	msgs  []string
	fatal bool
}

func (r *recorder) Helper() {}
func (r *recorder) Errorf(format string, args ...any) {
	r.msgs = append(r.msgs, fmt.Sprintf(format, args...))
}
func (r *recorder) Fatalf(format string, args ...any) {
	r.msgs = append(r.msgs, fmt.Sprintf(format, args...))
	r.fatal = true
	runtime.Goexit()
}

// record runs fn with a recorder in its own goroutine and returns it.
func record(fn func(tb testing.TB)) *recorder {
	r := &recorder{}
	done := make(chan struct{})
	go func() {
		defer close(done)
		fn(r)
	}()
	<-done
	return r
}

// fakeRepo builds a minimal repo tree with the given goldens and chdirs into
// backend/internal/parity inside it.
func fakeRepo(t *testing.T, goldens map[string]string) {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{
		"backend/go.mod":                  "module x\n",
		"testdata/golden/MANIFEST.sha256": "",
	}
	for name, body := range goldens {
		files["testdata/golden/"+name] = body
	}
	for name, body := range files {
		p := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	work := filepath.Join(root, "backend", "internal", "parity")
	if err := os.MkdirAll(work, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(work)
}

const fourLines = "alpha\nbeta\ngamma\ndelta\n"

func TestAssertGoldenMatch(t *testing.T) {
	fakeRepo(t, map[string]string{"x/out.txt": fourLines})
	r := record(func(tb testing.TB) { AssertGolden(tb, "x/out.txt", []byte(fourLines)) })
	if len(r.msgs) != 0 {
		t.Errorf("matching bytes reported %q", r.msgs)
	}
}

func TestAssertGoldenMismatchLine3(t *testing.T) {
	fakeRepo(t, map[string]string{"x/out.txt": fourLines})
	got := "alpha\nbeta\nGAMMA\ndelta\n"
	r := record(func(tb testing.TB) { AssertGolden(tb, "x/out.txt", []byte(got)) })
	if len(r.msgs) != 1 {
		t.Fatalf("messages = %q, want one", r.msgs)
	}
	want := `golden x/out.txt: line 3: want "gamma" got "GAMMA"`
	if r.msgs[0] != want {
		t.Errorf("message = %q, want %q", r.msgs[0], want)
	}
	if r.fatal {
		t.Error("a mismatch must use Errorf, not Fatalf")
	}
}

func TestAssertGoldenMissing(t *testing.T) {
	fakeRepo(t, nil)
	r := record(func(tb testing.TB) { AssertGolden(tb, "x/none.txt", []byte("a")) })
	if !r.fatal || len(r.msgs) != 1 {
		t.Fatalf("fatal=%v messages=%q, want one fatal", r.fatal, r.msgs)
	}
	want := "golden x/none.txt: missing (run scripts/make_golden.sh)"
	if r.msgs[0] != want {
		t.Errorf("message = %q, want %q", r.msgs[0], want)
	}
}

func TestAssertGoldenLengthOnly(t *testing.T) {
	fakeRepo(t, map[string]string{"x/out.txt": fourLines})
	r := record(func(tb testing.TB) { AssertGolden(tb, "x/out.txt", []byte("alpha\nbeta\n")) })
	want := `golden x/out.txt: line 3: want "gamma" got <EOF>`
	if len(r.msgs) != 1 || r.msgs[0] != want {
		t.Errorf("messages = %q, want [%q]", r.msgs, want)
	}
	r = record(func(tb testing.TB) { AssertGolden(tb, "x/out.txt", []byte(fourLines+"extra\n")) })
	want = `golden x/out.txt: line 5: want <EOF> got "extra"`
	if len(r.msgs) != 1 || r.msgs[0] != want {
		t.Errorf("messages = %q, want [%q]", r.msgs, want)
	}
}

func TestAssertGoldenTruncatesLongLines(t *testing.T) {
	long := strings.Repeat("a", 300)
	fakeRepo(t, map[string]string{"x/out.txt": long + "\n"})
	r := record(func(tb testing.TB) { AssertGolden(tb, "x/out.txt", []byte(long+"b\n")) })
	want := `golden x/out.txt: line 1: want "` + strings.Repeat("a", 200) + `" got "` + strings.Repeat("a", 200) + `"`
	if len(r.msgs) != 1 || r.msgs[0] != want {
		t.Errorf("messages = %q, want [%q]", r.msgs, want)
	}
}

func TestGoldenPathRejectsBadNames(t *testing.T) {
	for _, name := range []string{"../x", "/abs", "a/../../x"} {
		r := record(func(tb testing.TB) { GoldenPath(tb, name) })
		if !r.fatal {
			t.Errorf("GoldenPath(%q) did not fail", name)
		}
	}
}

func TestRepoRootFromPackageDir(t *testing.T) {
	root := RepoRoot(t)
	if _, err := os.Stat(filepath.Join(root, "backend", "go.mod")); err != nil {
		t.Errorf("RepoRoot %s has no backend/go.mod: %v", root, err)
	}
	if _, err := os.Stat(filepath.Join(root, "testdata", "golden", "MANIFEST.sha256")); err != nil {
		t.Errorf("RepoRoot %s has no testdata/golden/MANIFEST.sha256: %v", root, err)
	}
	if got := GoldenPath(t, "drift/drift.json"); got != filepath.Join(root, "testdata", "golden", "drift", "drift.json") {
		t.Errorf("GoldenPath = %s", got)
	}
}

func TestNormalizeTrace(t *testing.T) {
	in := `{"code_dirty": true, "code_version": "ac10de45ada3f4a653575fa7ef9107813372b700", "drift_config_version": null, "final": {"action": "ask"}, "seed": 0, "thread_id": 1}` + "\n" +
		`{"code_dirty": false, "code_version": "0123456789abcdef0123456789abcdef01234567", "thread_id": 2}` + "\n"
	want := `{"code_dirty": false, "code_version": "0000000000000000000000000000000000000000", "drift_config_version": null, "final": {"action": "ask"}, "seed": 0, "thread_id": 1}` + "\n" +
		`{"code_dirty": false, "code_version": "0000000000000000000000000000000000000000", "thread_id": 2}` + "\n"
	got, err := NormalizeTrace([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != want {
		t.Errorf("NormalizeTrace =\n%s\nwant\n%s", got, want)
	}
	again, err := NormalizeTrace(got)
	if err != nil {
		t.Fatal(err)
	}
	if string(again) != want {
		t.Errorf("NormalizeTrace is not idempotent:\n%s", again)
	}
}

func TestNormalizeTraceRejectsNonTraceLine(t *testing.T) {
	if _, err := NormalizeTrace([]byte("{\"thread_id\": 1}\n")); err == nil {
		t.Error("a line without code_version/code_dirty was accepted")
	}
}

func TestRunCLI(t *testing.T) {
	out, errb, code := RunCLI(t, t.TempDir(), "sh", "-c", "echo hi; echo oops >&2; exit 3")
	if string(out) != "hi\n" || string(errb) != "oops\n" || code != 3 {
		t.Errorf("RunCLI = %q, %q, %d", out, errb, code)
	}
	r := record(func(tb testing.TB) { RunCLI(tb, t.TempDir(), "/nonexistent/binary-xyz") })
	if !r.fatal {
		t.Error("a start failure did not fail t")
	}
}
