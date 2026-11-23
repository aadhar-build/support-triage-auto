// Command factory-autonomy-observe reports one read-only scheduling observation.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryproposer"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryshadow"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factorystate"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr, factoryshadow.Runner{Observe: factorystate.Observe})
	stop()
	os.Exit(code)
}

func run(ctx context.Context, args []string, output, diagnostics io.Writer, runner factoryshadow.Runner) int {
	var cfg factoryproposer.Config
	flags := flag.NewFlagSet("factory-autonomy-observe", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	checkout := flags.String("checkout", "", "absolute path to the trusted main factory checkout")
	allowModel := flags.Bool("allow-model", false, "allow native subscription model calls")
	flags.StringVar(&cfg.ClaudeBinary, "claude-bin", "", "absolute path to the trusted Claude executable")
	flags.StringVar(&cfg.ClaudeModel, "claude-model", "", "Claude model name")
	flags.StringVar(&cfg.ClaudeHome, "claude-home", "", "absolute path to a private Claude scheduling home")
	flags.StringVar(&cfg.CodexBinary, "codex-bin", "", "absolute path to the trusted Codex executable")
	flags.StringVar(&cfg.CodexModel, "codex-model", "", "Codex model name")
	flags.StringVar(&cfg.CodexHome, "codex-home", "", "absolute path to a private Codex scheduling home")
	if err := flags.Parse(args); errors.Is(err, flag.ErrHelp) {
		flags.SetOutput(output)
		fmt.Fprintln(output, "Usage: factory-autonomy-observe -checkout /absolute/path [model options]")
		flags.PrintDefaults()
		return 0
	} else if err != nil || flags.NArg() != 0 || !filepath.IsAbs(*checkout) {
		fmt.Fprintln(diagnostics, "Invalid options. Supply an absolute -checkout path, or use -help.")
		return 2
	}
	if !*allowModel {
		runner.NewProposer = nil
	} else if runner.NewProposer == nil {
		runner.NewProposer = func() (factoryshadow.Proposer, error) {
			return newProposer(*checkout, cfg)
		}
	}
	result := runner.Run(ctx, *checkout)
	if err := json.NewEncoder(output).Encode(result); err != nil {
		fmt.Fprintln(diagnostics, "Cannot write the observation report.")
		return 2
	}
	switch result.Status {
	case factoryshadow.StatusObserved, factoryshadow.StatusAccepted:
		return 0
	case "model-configuration-invalid":
		return 2
	default:
		return 1
	}
}

func newProposer(checkout string, cfg factoryproposer.Config) (factoryshadow.Proposer, error) {
	for _, item := range []struct {
		path        *string
		unavailable *bool
	}{
		{&cfg.ClaudeBinary, &cfg.ClaudeUnavailable}, {&cfg.CodexBinary, &cfg.CodexUnavailable},
		{&cfg.ClaudeHome, nil}, {&cfg.CodexHome, nil},
	} {
		resolved, missing, err := externalPath(checkout, *item.path, item.unavailable != nil)
		if err != nil {
			return nil, err
		}
		*item.path = resolved
		if item.unavailable != nil {
			*item.unavailable = missing
		}
	}
	return factoryproposer.New(cfg)
}

func externalPath(checkout, path string, allowMissing bool) (string, bool, error) {
	invalid := errors.New("model configuration requires absolute paths outside the checkout")
	if !filepath.IsAbs(path) {
		return "", false, invalid
	}
	checkout, err := filepath.EvalSymlinks(checkout)
	if err != nil {
		return "", false, invalid
	}
	checkoutInfo, err := os.Stat(checkout)
	if err != nil {
		return "", false, invalid
	}
	path = filepath.Clean(path)
	ancestor := path
	var resolved string
	for {
		resolved, err = filepath.EvalSymlinks(ancestor)
		if err == nil {
			break
		}
		if !allowMissing || !errors.Is(err, os.ErrNotExist) || filepath.Dir(ancestor) == ancestor {
			return "", false, invalid
		}
		// A dangling link is a configuration error. Only absent path entries
		// can make a provider unavailable while leaving the other usable.
		if _, err := os.Lstat(ancestor); !errors.Is(err, os.ErrNotExist) {
			return "", false, invalid
		}
		ancestor = filepath.Dir(ancestor)
	}
	if !outsideTree(ancestor, checkoutInfo) || !outsideTree(resolved, checkoutInfo) {
		return "", false, invalid
	}
	if ancestor == path {
		info, err := os.Stat(resolved)
		if err != nil || !outsideTree(checkout, info) {
			return "", false, invalid
		}
		return resolved, false, nil
	}
	missing, err := filepath.Rel(ancestor, path)
	if err != nil {
		return "", false, invalid
	}
	return filepath.Join(resolved, missing), true, nil
}

func outsideTree(path string, root os.FileInfo) bool {
	for {
		info, err := os.Stat(path)
		if err != nil || os.SameFile(info, root) {
			return false
		}
		parent := filepath.Dir(path)
		if parent == path {
			return true
		}
		path = parent
	}
}
