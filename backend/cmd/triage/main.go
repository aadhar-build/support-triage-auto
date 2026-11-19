// Command triage is the Go entry point. Subcommands are added by the port issues.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

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
	fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", os.Args[1])
	os.Exit(2)
}
