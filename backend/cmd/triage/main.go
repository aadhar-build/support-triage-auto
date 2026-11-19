// Command triage is the Go entry point. Subcommands are added by the port issues.
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/version"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

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
