package ingest

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/fixturegen"
)

const scaleEnv = "INGEST_SCALE_CHILD_CSV"

// TestMain lets the test binary re-run itself as the scale child: with
// INGEST_SCALE_CHILD_CSV set it runs BuildThreads plus a Splitter on that file
// with a no-op emit and prints rows_read.
func TestMain(m *testing.M) {
	if p := os.Getenv(scaleEnv); p != "" {
		os.Exit(scaleChild(p, os.Getenv("INGEST_SCALE_CHILD_SPLIT")))
	}
	os.Exit(m.Run())
}

func scaleChild(csvPath, splitPath string) int {
	cfg, err := LoadSplitConfig(splitPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	sp := NewSplitter(cfg)
	st, err := BuildThreads(csvPath, Options{TempDir: filepath.Dir(csvPath)}, func(th Thread) error {
		sp.Add(th.RootID)
		return nil
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	fmt.Printf("rows_read=%d threads=%d split_total=%d\n", st.Load.RowsRead, st.ThreadsBuilt, sp.Report().Total)
	return 0
}

type childRun struct {
	rows   int
	wall   time.Duration
	maxRSS int64 // bytes
}

func runScaleChild(t *testing.T, threads int) childRun {
	t.Helper()
	twcs, _, err := fixturegen.Generate(65, threads)
	must(t, err)
	dir := t.TempDir()
	p := filepath.Join(dir, "scale.csv")
	must(t, os.WriteFile(p, twcs, 0o600))
	twcs = nil
	exe, err := os.Executable()
	must(t, err)
	cmd := exec.Command(exe, "-test.run=^$")
	cmd.Env = append(os.Environ(), scaleEnv+"="+p, "INGEST_SCALE_CHILD_SPLIT="+splitJSONPath(t))
	start := time.Now()
	out, err := cmd.Output()
	wall := time.Since(start)
	if err != nil {
		t.Fatalf("scale child: %v\n%s", err, out)
	}
	fields := strings.Fields(string(out))
	if len(fields) == 0 || !strings.HasPrefix(fields[0], "rows_read=") {
		t.Fatalf("scale child output %q", out)
	}
	rows, err := strconv.Atoi(strings.TrimPrefix(fields[0], "rows_read="))
	must(t, err)
	ru := cmd.ProcessState.SysUsage().(*syscall.Rusage)
	return childRun{rows: rows, wall: wall, maxRSS: int64(ru.Maxrss) * 1024}
}

func splitJSONPath(t *testing.T) string {
	return filepath.Join(repoRootForScale(t), "data", "split.json")
}

func TestScale(t *testing.T) {
	// fixturegen writes 1 to 3 rows per thread (about 1.8 on average) plus 4
	// bad rows; 120,000 threads gives well over 200,000 rows.
	big := runScaleChild(t, 120000)
	if big.rows < 200000 {
		t.Fatalf("rows_read %d, want >= 200000", big.rows)
	}
	small := runScaleChild(t, 12000)
	t.Logf("200k child: rows %d, wall %s, peak RSS %d bytes; 20k child: rows %d, wall %s, peak RSS %d bytes; growth %d bytes",
		big.rows, big.wall.Round(time.Millisecond), big.maxRSS, small.rows, small.wall.Round(time.Millisecond), small.maxRSS, big.maxRSS-small.maxRSS)
	if big.wall > 60*time.Second {
		t.Errorf("wall %s > 60s", big.wall)
	}
	if big.maxRSS > 1_000_000_000 {
		t.Errorf("peak RSS %d > 1e9 bytes", big.maxRSS)
	}
	if growth := big.maxRSS - small.maxRSS; growth > 32<<20 {
		t.Errorf("peak RSS grew %d bytes from %d to %d rows, want <= %d", growth, small.rows, big.rows, 32<<20)
	}
}

func repoRootForScale(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	must(t, err)
	for {
		if _, err := os.Stat(filepath.Join(dir, "data", "split.json")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("no repo root with data/split.json")
		}
		dir = parent
	}
}
