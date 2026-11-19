// Package parity is the shared harness every Go port uses to prove it matches
// the Python reference byte for byte. The goldens live in <repo>/testdata/golden
// and are built by scripts/make_golden.sh from the made-up inputs of fixturegen.
//
// A port test reads, for example:
//
//	twcs, gold, _ := parity.Inputs(t)
//	out := runMyPort(twcs, gold)
//	parity.AssertGolden(t, "drift/drift.json", out)
package parity

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/fixturegen"
)

// Seed is the canonical generator seed for the goldens.
const Seed int64 = 65

// Threads is the canonical thread count.
const Threads = 2000

// PlaceholderCommit replaces the git commit in every golden trace.
const PlaceholderCommit = "0000000000000000000000000000000000000000"

const maxShown = 200

// RepoRoot walks up from the working directory to the first dir holding both
// backend/go.mod and testdata/golden/MANIFEST.sha256. Fails t if none.
func RepoRoot(t testing.TB) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("parity: working directory: %v", err)
	}
	for {
		if isFile(filepath.Join(dir, "backend", "go.mod")) &&
			isFile(filepath.Join(dir, "testdata", "golden", "MANIFEST.sha256")) {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("parity: no repo root (backend/go.mod + testdata/golden/MANIFEST.sha256) above the working directory")
		}
		dir = parent
	}
}

func isFile(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}

// GoldenPath returns <repo>/testdata/golden/<name>; name is slash-separated,
// e.g. "drift/drift.json". Fails t on an absolute name or one containing "..".
func GoldenPath(t testing.TB, name string) string {
	t.Helper()
	if name == "" || path.IsAbs(name) || filepath.IsAbs(name) || strings.Contains(name, "\\") {
		t.Fatalf("parity: golden name %q must be a relative slash-separated path", name)
	}
	for _, part := range strings.Split(name, "/") {
		if part == ".." {
			t.Fatalf("parity: golden name %q must not contain ..", name)
		}
	}
	return filepath.Join(RepoRoot(t), "testdata", "golden", filepath.FromSlash(name))
}

// AssertGolden compares got byte for byte with the golden file <name>.
// Missing golden: fatal "golden <name>: missing (run scripts/make_golden.sh)".
// Mismatch: Errorf "golden <name>: line N: want <line> got <line>", N 1-based,
// lines split on "\n", each side %q-quoted and truncated to 200 bytes; past the
// end of the shorter side the line reads <EOF>.
func AssertGolden(t testing.TB, name string, got []byte) {
	t.Helper()
	p := GoldenPath(t, name)
	want, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		t.Fatalf("golden %s: missing (run scripts/make_golden.sh)", name)
	}
	if err != nil {
		t.Fatalf("golden %s: %v", name, err)
	}
	if bytes.Equal(want, got) {
		return
	}
	wl := strings.Split(string(want), "\n")
	gl := strings.Split(string(got), "\n")
	for i := 0; ; i++ {
		w, wok := line(wl, i)
		g, gok := line(gl, i)
		if w != g || wok != gok {
			t.Errorf("golden %s: line %d: want %s got %s", name, i+1, show(w, wok), show(g, gok))
			return
		}
		if !wok && !gok {
			// Same lines: only the final newline differs.
			t.Errorf("golden %s: line %d: final newline differs (want %d bytes, got %d)", name, i, len(want), len(got))
			return
		}
	}
}

// line returns lines[i] and whether it exists. A trailing "" after the final
// newline counts as past the end.
func line(lines []string, i int) (string, bool) {
	n := len(lines)
	if n > 0 && lines[n-1] == "" {
		n--
	}
	if i < n {
		return lines[i], true
	}
	return "", false
}

func show(s string, ok bool) string {
	if !ok {
		return "<EOF>"
	}
	if len(s) > maxShown {
		s = s[:maxShown]
	}
	return fmt.Sprintf("%q", s)
}

// RunCLI runs a command (e.g. a built Go binary) with args from dir and returns
// stdout, stderr and exit code; a start failure fails t.
func RunCLI(t testing.TB, dir string, name string, args ...string) (stdout, stderr []byte, code int) {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	var exit *exec.ExitError
	switch {
	case err == nil:
	case errors.As(err, &exit):
		code = exit.ExitCode()
	default:
		t.Fatalf("parity: cannot run %s: %v", name, err)
	}
	return out.Bytes(), errb.Bytes(), code
}

// Inputs writes the canonical generated twcs.csv and gold.csv (fixturegen, Seed,
// Threads) and a copy of evals/gold/synthetic.json into t.TempDir() and returns
// their paths; fails t if their sha256s differ from INPUTS.sha256.
func Inputs(t testing.TB) (twcsCSV, goldCSV, casesJSON string) {
	t.Helper()
	root := RepoRoot(t)
	twcs, gold, err := fixturegen.Generate(Seed, Threads)
	if err != nil {
		t.Fatalf("parity: generate inputs: %v", err)
	}
	cases, err := os.ReadFile(filepath.Join(root, "evals", "gold", "synthetic.json"))
	if err != nil {
		t.Fatalf("parity: read synthetic cases: %v", err)
	}
	pinned, err := readManifest(filepath.Join(root, "testdata", "golden", "INPUTS.sha256"))
	if err != nil {
		t.Fatalf("parity: %v", err)
	}
	dir := t.TempDir()
	files := []struct {
		name, key string
		body      []byte
	}{{"twcs.csv", "inputs/twcs.csv", twcs}, {"gold.csv", "inputs/gold.csv", gold}, {"synthetic.json", "inputs/synthetic.json", cases}}
	paths := make([]string, len(files))
	for i, f := range files {
		sum := sha256.Sum256(f.body)
		if got := hex.EncodeToString(sum[:]); got != pinned[f.key] {
			t.Fatalf("parity: %s sha256 %s differs from INPUTS.sha256 (%s); regenerate with scripts/make_golden.sh", f.key, got, pinned[f.key])
		}
		paths[i] = filepath.Join(dir, f.name)
		if err := os.WriteFile(paths[i], f.body, 0o644); err != nil {
			t.Fatalf("parity: %v", err)
		}
	}
	return paths[0], paths[1], paths[2]
}

// readManifest parses a sha256sum-format file into path -> hex digest.
func readManifest(p string) (map[string]string, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	m := map[string]string{}
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		sum, name, ok := strings.Cut(sc.Text(), "  ")
		if !ok || len(sum) != 64 {
			return nil, fmt.Errorf("%s: bad line %q", p, sc.Text())
		}
		m[name] = sum
	}
	return m, sc.Err()
}

var (
	codeVersionRE = regexp.MustCompile(`"code_version": "[0-9a-f]{40}"`)
	codeDirtyRE   = regexp.MustCompile(`"code_dirty": (?:true|false)`)
)

// NormalizeTrace applies the trace normalization (code_version ->
// PlaceholderCommit, code_dirty -> false) to JSONL bytes, line by line. Only the
// first occurrence of each field per line is rewritten, byte for byte, so the
// key order and spacing json.dumps(sort_keys=True) emits are kept. A non-empty
// line lacking either field is an error.
func NormalizeTrace(jsonl []byte) ([]byte, error) {
	lines := bytes.SplitAfter(jsonl, []byte("\n"))
	var out bytes.Buffer
	for i, l := range lines {
		if len(bytes.TrimSpace(l)) == 0 {
			out.Write(l)
			continue
		}
		var ok bool
		if l, ok = replaceFirst(l, codeVersionRE, `"code_version": "`+PlaceholderCommit+`"`); !ok {
			return nil, fmt.Errorf("trace line %d has no 40-hex code_version", i+1)
		}
		if l, ok = replaceFirst(l, codeDirtyRE, `"code_dirty": false`); !ok {
			return nil, fmt.Errorf("trace line %d has no boolean code_dirty", i+1)
		}
		out.Write(l)
	}
	return out.Bytes(), nil
}

func replaceFirst(b []byte, re *regexp.Regexp, repl string) ([]byte, bool) {
	loc := re.FindIndex(b)
	if loc == nil {
		return b, false
	}
	r := make([]byte, 0, len(b))
	r = append(r, b[:loc[0]]...)
	r = append(r, repl...)
	return append(r, b[loc[1]:]...), true
}
