package ingest

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// TestGoldenDumperRefusesRawData uses made-up paths only; nothing under
// data/raw is opened.
func TestGoldenDumperRefusesRawData(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Fatalf("python3 is required for the dumper check: %v", err)
	}
	root := parity.RepoRoot(t)
	link := filepath.Join(t.TempDir(), "link.csv")
	must(t, os.Symlink(filepath.Join(root, "data", "raw", "nope.csv"), link))
	split := filepath.Join(root, "data", "split.json")
	for name, data := range map[string]string{"literal": "data/raw/nope.csv", "symlink": link} {
		t.Run(name, func(t *testing.T) {
			out := t.TempDir()
			cmd := exec.Command("python3", filepath.Join(root, "scripts", "golden_ingest.py"),
				"--data", data, "--split-config", split, "--out-prefix", filepath.Join(out, "x"))
			cmd.Dir = root
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			err := cmd.Run()
			exit, ok := err.(*exec.ExitError)
			if !ok || exit.ExitCode() != 2 {
				t.Errorf("err %v, want exit 2", err)
			}
			if s := stderr.String(); strings.Count(s, "\n") != 1 || !strings.HasSuffix(s, "\n") {
				t.Errorf("stderr %q, want exactly one line", s)
			}
			assertEmptyDir(t, out)
		})
	}
}
