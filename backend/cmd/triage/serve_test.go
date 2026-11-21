package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

// syncBuffer is a bytes.Buffer safe to write from runServe's goroutine.
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func runQuick(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // a run that got as far as serving would stop at once
	var out, errb bytes.Buffer
	code := runServe(ctx, args, &out, &errb)
	return code, out.String(), errb.String()
}

func TestServeArgs(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "file")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		args []string
		want string
	}{
		{[]string{"--port", "0"}, "error: the following arguments are required: --runs\n"},
		{[]string{"--runs", dir, "--port", "0", "--host", "0.0.0.0"}, "error: --host must be the literal 127.0.0.1\n"},
		{[]string{"--runs", dir, "--port", "0", "--host", "localhost"}, "error: --host must be the literal 127.0.0.1\n"},
		{[]string{"--runs", file, "--port", "0"}, "error: --runs " + file + " is not a directory\n"},
	}
	for _, c := range cases {
		code, out, errs := runQuick(t, c.args...)
		if code != 2 || errs != c.want || out != "" {
			t.Errorf("%v: code %d stderr %q stdout %q", c.args, code, errs, out)
		}
	}
	code, out, errs := runQuick(t, "--runs", dir, "--bogus")
	if code != 2 || !strings.HasPrefix(errs, "error: ") || strings.Count(errs, "\n") != 1 || out != "" {
		t.Errorf("unknown flag: code %d stderr %q", code, errs)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	port := ln.Addr().(*net.TCPAddr).Port
	code, out, errs = runQuick(t, "--runs", dir, "--port", fmt.Sprint(port))
	if code != 1 || !strings.HasPrefix(errs, "error: cannot start the console: ") || strings.Count(errs, "\n") != 1 || out != "" {
		t.Errorf("occupied port: code %d stderr %q stdout %q", code, errs, out)
	}
}

func TestServeStartsAndStops(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	var out, errb syncBuffer
	done := make(chan int, 1)
	go func() {
		done <- runServe(ctx, []string{"--runs", t.TempDir(), "--port", "0", "--web", t.TempDir()}, &out, &errb)
	}()

	line := regexp.MustCompile(`^admin console on http://127\.0\.0\.1:(\d+)/ \(Ctrl-C to stop\)\n`)
	var port string
	deadline := time.Now().Add(5 * time.Second)
	for port == "" {
		if m := line.FindStringSubmatch(out.String()); m != nil {
			port = m[1]
		} else if time.Now().After(deadline) {
			cancel()
			t.Fatalf("no startup line; stdout %q stderr %q", out.String(), errb.String())
		} else {
			time.Sleep(10 * time.Millisecond)
		}
	}
	resp, err := http.Get("http://127.0.0.1:" + port + "/api/runs")
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Errorf("GET /api/runs: %d", resp.StatusCode)
	}
	http.DefaultClient.CloseIdleConnections()

	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Errorf("exit %d, stderr %q", code, errb.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runServe did not return within 5 s")
	}
	if !strings.HasSuffix(out.String(), "stopped\n") {
		t.Errorf("stdout %q does not end with stopped", out.String())
	}
	if c, err := net.DialTimeout("tcp", "127.0.0.1:"+port, time.Second); err == nil {
		c.Close()
		t.Error("port still accepts connections")
	}
}

// rawServe sends one request line exactly as given and returns status and body.
func rawServe(t *testing.T, addr, method, target string) (int, string) {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := fmt.Fprintf(conn, "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", method, target, addr); err != nil {
		t.Fatal(err)
	}
	r, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: method})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	b, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	return r.StatusCode, string(b)
}

func TestServeRefusesNonOriginTargets(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	var out, errb syncBuffer
	done := make(chan int, 1)
	go func() {
		done <- runServe(ctx, []string{"--runs", t.TempDir(), "--port", "0", "--web", t.TempDir()}, &out, &errb)
	}()

	line := regexp.MustCompile(`^admin console on http://127\.0\.0\.1:(\d+)/ \(Ctrl-C to stop\)\n`)
	var port string
	deadline := time.Now().Add(5 * time.Second)
	for port == "" {
		if m := line.FindStringSubmatch(out.String()); m != nil {
			port = m[1]
		} else if time.Now().After(deadline) {
			cancel()
			t.Fatalf("no startup line; stdout %q stderr %q", out.String(), errb.String())
		} else {
			time.Sleep(10 * time.Millisecond)
		}
	}
	addr := "127.0.0.1:" + port
	for _, c := range []struct{ method, target string }{{"OPTIONS", "*"}, {"GET", "http://localhost/api/runs"}} {
		if status, body := rawServe(t, addr, c.method, c.target); status != 400 || body != "bad request\n" {
			t.Errorf("%s %s: %d %q", c.method, c.target, status, body)
		}
	}
	if status, body := rawServe(t, addr, "GET", "/api/runs"); status != 200 {
		t.Errorf("GET /api/runs: %d %q", status, body)
	}

	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Errorf("exit %d, stderr %q", code, errb.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runServe did not return within 5 s")
	}
}

func TestServeDefaults(t *testing.T) {
	for _, h := range []string{"--help", "-h"} {
		code, out, errs := runQuick(t, h)
		if code != 0 || errs != "" {
			t.Errorf("%s: code %d stderr %q", h, code, errs)
		}
		for _, want := range []string{"--runs", "--port", "--host", "--rules", "--thresholds", "--web",
			"(default 8770)", "8765 is taken on the VPS", "(default 127.0.0.1)", "(default web/dist)",
			"(default policies/rules.json)", "(default evals/thresholds.json)"} {
			if !strings.Contains(out, want) {
				t.Errorf("%s: help lacks %q:\n%s", h, want, out)
			}
		}
	}
}
