package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/admin"
)

const serveHost = "127.0.0.1"

const serveUsage = `usage: triage serve --runs DIR [--port N] [--host 127.0.0.1] [--rules PATH] [--thresholds PATH] [--web DIR]

Read-only local admin console, run in the foreground on 127.0.0.1 only (GET only).

  --runs DIR          folder of run subfolders, read only (required)
  --port N            port; 0 picks a free one (default 8770) - 8765 is taken on the VPS
  --host H            only the literal 127.0.0.1 is accepted (default 127.0.0.1)
  --rules PATH        rules file, read only; accepted for CLI compatibility, not read yet (default policies/rules.json)
  --thresholds PATH   thresholds file, read only (default evals/thresholds.json)
  --web DIR           built console, served by this same server (default web/dist)
`

// runServe is `triage serve`. It returns the exit code when ctx is cancelled
// or the arguments or listener fail.
func runServe(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("triage serve", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	runs := fs.String("runs", "", "")
	port := fs.Int("port", 8770, "")
	host := fs.String("host", serveHost, "")
	fs.String("rules", "policies/rules.json", "")
	thresholds := fs.String("thresholds", "evals/thresholds.json", "")
	web := fs.String("web", "web/dist", "")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			fmt.Fprint(stdout, serveUsage)
			return 0
		}
		fmt.Fprintf(stderr, "error: %s\n", firstLine(err.Error()))
		return 2
	}
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	switch {
	case fs.NArg() > 0:
		fmt.Fprintf(stderr, "error: unrecognized arguments: %s\n", strings.Join(fs.Args(), " "))
		return 2
	case !set["runs"]:
		fmt.Fprintln(stderr, "error: the following arguments are required: --runs")
		return 2
	case *host != serveHost:
		fmt.Fprintln(stderr, "error: --host must be the literal 127.0.0.1")
		return 2
	}
	if st, err := os.Stat(*runs); err != nil || !st.IsDir() {
		fmt.Fprintf(stderr, "error: --runs %s is not a directory\n", *runs)
		return 2
	}

	ln, err := net.Listen("tcp", net.JoinHostPort(serveHost, strconv.Itoa(*port)))
	if err != nil {
		fmt.Fprintf(stderr, "error: cannot start the console: %s\n", firstLine(err.Error()))
		return 1
	}
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok || addr.IP.String() != serveHost {
		ln.Close()
		fmt.Fprintf(stderr, "error: cannot start the console: bound to %s, not %s\n", ln.Addr(), serveHost)
		return 1
	}
	srv := &http.Server{
		Handler:           admin.NewHandler(admin.Config{Runs: *runs, Thresholds: *thresholds, Web: *web}),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    64 << 10,
	}
	fmt.Fprintf(stdout, "admin console on http://%s:%d/ (Ctrl-C to stop)\n", serveHost, addr.Port)
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()
	select {
	case <-ctx.Done():
	case err := <-served:
		fmt.Fprintf(stderr, "error: cannot start the console: %s\n", firstLine(err.Error()))
		return 1
	}
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(sctx)
	fmt.Fprintln(stdout, "stopped")
	return 0
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(s, "\n")
	return line
}
