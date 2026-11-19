// Command triage is the Go entry point. Subcommands are added by the port issues.
package main

import (
	"fmt"
	"os"

	"github.com/aadhar-build/support-triage-auto/backend/internal/version"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "%s: no subcommand yet\n", version.Name)
		os.Exit(2)
	}
	fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", os.Args[1])
	os.Exit(2)
}
