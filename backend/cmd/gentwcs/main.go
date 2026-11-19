// Command gentwcs writes the made-up twcs.csv and gold.csv from fixturegen.
//
// Usage (from backend/): go run ./cmd/gentwcs --seed N --threads N --out-dir DIR
//
// Bad or missing flags, or an --out-dir that resolves under <repo>/data/raw/,
// exit 2 with one stderr line and write nothing.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/fixturegen"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("gentwcs", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	seed := fs.Int64("seed", 0, "generator seed")
	threads := fs.Int("threads", 0, "number of threads")
	outDir := fs.String("out-dir", "", "output directory")
	fail := func(format string, a ...any) int {
		fmt.Fprintf(stderr, "gentwcs: "+format+"\n", a...)
		return 2
	}
	if err := fs.Parse(args); err != nil {
		return fail("%s", strings.ReplaceAll(err.Error(), "\n", " "))
	}
	if fs.NArg() > 0 {
		return fail("unexpected argument %q", fs.Arg(0))
	}
	if *threads < 1 {
		return fail("--threads must be positive, got %d", *threads)
	}
	if *outDir == "" {
		return fail("--out-dir is required")
	}
	if err := refuseRaw(*outDir); err != nil {
		return fail("%v", err)
	}
	twcs, gold, err := fixturegen.Generate(*seed, *threads)
	if err != nil {
		return fail("%v", err)
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		return fail("%v", err)
	}
	for _, f := range []struct {
		name string
		b    []byte
	}{{"twcs.csv", twcs}, {"gold.csv", gold}} {
		if err := os.WriteFile(filepath.Join(*outDir, f.name), f.b, 0o644); err != nil {
			return fail("%v", err)
		}
	}
	fmt.Fprintf(stdout, "wrote %s and %s\n", filepath.Join(*outDir, "twcs.csv"), filepath.Join(*outDir, "gold.csv"))
	return 0
}

// refuseRaw fails when dir, with symlinks in its existing part resolved, is the
// repo's data/raw or under it. The repo is found by walking up from the working
// directory to the first dir holding backend/go.mod.
func refuseRaw(dir string) error {
	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	repo := ""
	for d := cwd; ; d = filepath.Dir(d) {
		if _, err := os.Stat(filepath.Join(d, "backend", "go.mod")); err == nil {
			repo = d
			break
		}
		if filepath.Dir(d) == d {
			break
		}
	}
	if repo == "" {
		return nil
	}
	raw := resolve(filepath.Join(repo, "data", "raw"))
	got := resolve(dir)
	if got == raw || strings.HasPrefix(got, raw+string(filepath.Separator)) {
		return errors.New("--out-dir " + dir + " is under data/raw; refused")
	}
	return nil
}

// resolve returns the absolute path with symlinks resolved in its longest
// existing prefix.
func resolve(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	rest := ""
	for d := abs; ; d = filepath.Dir(d) {
		if r, err := filepath.EvalSymlinks(d); err == nil {
			return filepath.Join(r, rest)
		}
		if filepath.Dir(d) == d {
			return abs
		}
		rest = filepath.Join(filepath.Base(d), rest)
	}
}
