package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func runArgs(args ...string) (int, string, string) {
	var out, errb bytes.Buffer
	code := run(args, &out, &errb)
	return code, out.String(), errb.String()
}

func TestWritesBothFiles(t *testing.T) {
	dir := t.TempDir()
	code, _, stderr := runArgs("--seed", "7", "--threads", "40", "--out-dir", dir)
	if code != 0 {
		t.Fatalf("exit %d, stderr %q", code, stderr)
	}
	for _, name := range []string{"twcs.csv", "gold.csv"} {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if len(b) == 0 {
			t.Errorf("%s is empty", name)
		}
	}
}

func assertRefused(t *testing.T, dir string, args ...string) {
	t.Helper()
	code, _, stderr := runArgs(args...)
	if code != 2 {
		t.Errorf("args %q: exit %d, want 2", args, code)
	}
	if n := strings.Count(stderr, "\n"); n != 1 || !strings.HasSuffix(stderr, "\n") {
		t.Errorf("args %q: stderr %q, want exactly one line", args, stderr)
	}
	if dir != "" {
		entries, _ := os.ReadDir(dir)
		if len(entries) != 0 {
			t.Errorf("args %q: %d files written, want none", args, len(entries))
		}
	}
}

func TestRefusesUnknownFlag(t *testing.T) {
	dir := t.TempDir()
	assertRefused(t, dir, "--seed", "1", "--threads", "10", "--out-dir", dir, "--bogus")
}

func TestRefusesNonPositiveThreads(t *testing.T) {
	dir := t.TempDir()
	assertRefused(t, dir, "--seed", "1", "--threads", "0", "--out-dir", dir)
	assertRefused(t, dir, "--seed", "1", "--threads", "-3", "--out-dir", dir)
}

func TestRefusesMissingOutDir(t *testing.T) {
	assertRefused(t, "", "--seed", "1", "--threads", "10")
}

func TestRefusesOutDirUnderDataRaw(t *testing.T) {
	// From backend/cmd/gentwcs the repo's data/raw is ../../../data/raw.
	target := filepath.Join("..", "..", "..", "data", "raw", "gentwcs-test-should-not-exist")
	assertRefused(t, "", "--seed", "1", "--threads", "10", "--out-dir", target)
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Errorf("%s exists after a refused run", target)
	}
}
