package parity

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// runScript runs scripts/make_golden.sh with args, optionally with a PATH
// prefix, and returns stderr and the exit code. A missing bash or python3
// fails the test; it never skips.
func runScript(t *testing.T, pathPrefix string, args ...string) (string, int) {
	t.Helper()
	for _, tool := range []string{"bash", "python3"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Fatalf("%s is required for the golden regen check: %v", tool, err)
		}
	}
	root := RepoRoot(t)
	cmd := exec.Command("bash", append([]string{filepath.Join(root, "scripts", "make_golden.sh")}, args...)...)
	cmd.Dir = root
	env := os.Environ()
	if pathPrefix != "" {
		env = append(env, "PATH="+pathPrefix+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	cmd.Env = env
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := 0
	if exit, ok := err.(*exec.ExitError); ok {
		code = exit.ExitCode()
	} else if err != nil {
		t.Fatalf("cannot run make_golden.sh: %v", err)
	}
	return stderr.String(), code
}

func readFile(t *testing.T, p string) []byte {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func goldenDir(t *testing.T) string {
	return filepath.Join(RepoRoot(t), "testdata", "golden")
}

// stubGit writes a git that reports a different commit and a dirty tree.
func stubGit(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\ncase \"$*\" in\n" +
		"  *rev-parse*) echo 1111111111111111111111111111111111111111 ;;\n" +
		"  *status*) echo ' M stub-file' ;;\n" +
		"  *) exit 1 ;;\nesac\n"
	if err := os.WriteFile(filepath.Join(dir, "git"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

// TestCommittedGoldensMatchScript also covers TestScriptReproducible and
// TestGoldensIgnoreCommitAndDirty below by sharing the two script runs.
func TestCommittedGoldensMatchScript(t *testing.T) {
	start := time.Now()
	committed := goldenDir(t)
	runA := t.TempDir()
	if stderr, code := runScript(t, "", "--out", runA); code != 0 {
		t.Fatalf("make_golden.sh exit %d: %s", code, stderr)
	}
	runB := t.TempDir()
	if stderr, code := runScript(t, stubGit(t), "--out", runB); code != 0 {
		t.Fatalf("make_golden.sh (stub git) exit %d: %s", code, stderr)
	}
	t.Logf("two script runs took %s", time.Since(start).Round(time.Millisecond))

	for _, m := range []string{"MANIFEST.sha256", "INPUTS.sha256"} {
		want := readFile(t, filepath.Join(committed, m))
		t.Run("regen/"+m, func(t *testing.T) {
			if got := readFile(t, filepath.Join(runA, m)); !bytes.Equal(got, want) {
				t.Errorf("regenerated %s differs from the committed one; run scripts/make_golden.sh\n%s", m, got)
			}
		})
		t.Run("TestScriptReproducible/"+m, func(t *testing.T) {
			if !bytes.Equal(readFile(t, filepath.Join(runA, m)), readFile(t, filepath.Join(runB, m))) {
				t.Errorf("%s differs between two script runs", m)
			}
		})
		t.Run("TestGoldensIgnoreCommitAndDirty/"+m, func(t *testing.T) {
			if got := readFile(t, filepath.Join(runB, m)); !bytes.Equal(got, want) {
				t.Errorf("%s with a stub git (other commit, dirty) differs from the committed one\n%s", m, got)
			}
		})
	}

	manifest, err := readManifest(filepath.Join(committed, "MANIFEST.sha256"))
	if err != nil {
		t.Fatal(err)
	}
	if len(manifest) == 0 {
		t.Fatal("committed MANIFEST.sha256 is empty")
	}
	for name, sum := range manifest {
		got := sha256.Sum256(readFile(t, filepath.Join(committed, filepath.FromSlash(name))))
		if hex.EncodeToString(got[:]) != sum {
			t.Errorf("committed golden %s does not hash to its manifest entry", name)
		}
	}
}

func TestScriptRefusesRawData(t *testing.T) {
	root := RepoRoot(t)
	link := filepath.Join(t.TempDir(), "link.csv")
	if err := os.Symlink(filepath.Join(root, "data", "raw", "twcs.csv"), link); err != nil {
		t.Fatal(err)
	}
	ok := filepath.Join(root, "evals", "gold", "synthetic.json")
	cases := map[string][]string{
		"data":         {"--data", "data/raw/nope.csv", "--gold", ok},
		"data-symlink": {"--data", link, "--gold", ok},
		"gold":         {"--data", ok, "--gold", "data/raw/nope.csv"},
		"gold-symlink": {"--data", ok, "--gold", link},
		"cases":        {"--cases", "data/raw/nope.json"},
		"cases-link":   {"--cases", link},
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			out := t.TempDir()
			stderr, code := runScript(t, "", append(args, "--out", out)...)
			if code != 2 {
				t.Errorf("exit %d, want 2", code)
			}
			if strings.Count(stderr, "\n") != 1 || !strings.HasSuffix(stderr, "\n") {
				t.Errorf("stderr %q, want exactly one line", stderr)
			}
			flag := "--" + strings.Split(name, "-")[0]
			if !strings.Contains(stderr, flag) {
				t.Errorf("stderr %q does not name %s", stderr, flag)
			}
			if entries, _ := os.ReadDir(out); len(entries) != 0 {
				t.Errorf("--out holds %d entries after a refusal", len(entries))
			}
		})
	}
}

func countLines(b []byte) int { return bytes.Count(b, []byte("\n")) }

func TestGoldensNonTrivial(t *testing.T) {
	g := goldenDir(t)
	if n := countLines(readFile(t, filepath.Join(g, "incidents", "incidents.jsonl"))); n < 1 {
		t.Errorf("incidents.jsonl has %d lines, want >= 1", n)
	}
	var drift struct {
		Weeks int `json:"weeks"`
	}
	if err := json.Unmarshal(readFile(t, filepath.Join(g, "drift", "drift.json")), &drift); err != nil {
		t.Fatal(err)
	}
	if drift.Weeks < 6 {
		t.Errorf("drift weeks = %d, want >= 6", drift.Weeks)
	}
	var eval struct {
		ThreadsRouted int `json:"threads_routed"`
		ThreadsScored int `json:"threads_scored"`
	}
	if err := json.Unmarshal(readFile(t, filepath.Join(g, "run_eval", "eval.json")), &eval); err != nil {
		t.Fatal(err)
	}
	if eval.ThreadsRouted == 0 || eval.ThreadsScored == 0 {
		t.Errorf("eval routed=%d scored=%d, want > 0", eval.ThreadsRouted, eval.ThreadsScored)
	}
	var cases []json.RawMessage
	raw := readFile(t, filepath.Join(RepoRoot(t), "evals", "gold", "synthetic.json"))
	if err := json.Unmarshal(raw, &cases); err != nil {
		var wrapped struct {
			Cases []json.RawMessage `json:"cases"`
		}
		if err := json.Unmarshal(raw, &wrapped); err != nil {
			t.Fatalf("synthetic.json: %v", err)
		}
		cases = wrapped.Cases
	}
	if n := countLines(readFile(t, filepath.Join(g, "run_synthetic", "trace.jsonl"))); n != len(cases) || n == 0 {
		t.Errorf("run_synthetic trace has %d lines, want one per case (%d)", n, len(cases))
	}
}

func TestGoldensHaveNoLeaks(t *testing.T) {
	root := RepoRoot(t)
	g := goldenDir(t)
	err := filepath.WalkDir(g, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		b := readFile(t, p)
		rel, _ := filepath.Rel(g, p)
		for _, bad := range []string{root, "/tmp/", "make_golden.", "data/raw"} {
			if bytes.Contains(b, []byte(bad)) {
				t.Errorf("golden %s contains %q", rel, bad)
			}
		}
		if filepath.Base(p) == "trace.jsonl" {
			for i, line := range strings.Split(strings.TrimSuffix(string(b), "\n"), "\n") {
				if !strings.Contains(line, `"code_version": "`+PlaceholderCommit+`"`) ||
					!strings.Contains(line, `"code_dirty": false`) {
					t.Errorf("%s line %d is not normalized", rel, i+1)
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestScriptUsesOnlyFakeLargerTier(t *testing.T) {
	s := string(readFile(t, filepath.Join(RepoRoot(t), "scripts", "make_golden.sh")))
	if !strings.Contains(s, "--larger fake") {
		t.Error("make_golden.sh does not pass --larger fake")
	}
	if strings.Contains(s, "--larger real") {
		t.Error("make_golden.sh mentions --larger real")
	}
}

func TestInputsMatchPinnedHashes(t *testing.T) {
	twcs, gold, cases := Inputs(t)
	for _, p := range []string{twcs, gold, cases} {
		if st, err := os.Stat(p); err != nil || st.Size() == 0 {
			t.Errorf("input %s missing or empty: %v", p, err)
		}
	}
}
