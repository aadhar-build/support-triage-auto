// Command triage is the Go entry point. Subcommands are added by the port issues.
package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/version"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "%s: no subcommand yet\n", version.Name)
		os.Exit(2)
	}
	if os.Args[1] == "serve" {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		code := runServe(ctx, os.Args[2:], os.Stdout, os.Stderr)
		stop()
		os.Exit(code)
	}
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run dispatches every subcommand except serve, which main handles with a
// signal context.
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) < 1 {
		fmt.Fprintf(stderr, "%s: no subcommand yet\n", version.Name)
		return 2
	}
	switch args[0] {
	case "brand-rules":
		return policy.BrandRulesMain(args[1:], stdout, stderr)
	}
	fmt.Fprintf(stderr, "unknown subcommand: %s\n", args[0])
	return 2
}
