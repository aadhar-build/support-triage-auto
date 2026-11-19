package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

const receiptFile = "worker-events-v1"

func receiptRows(t *testing.T, dir string) []map[string]any {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var rows []map[string]any
	for _, entry := range entries {
		if entry.Name() == ".staging" {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal([]byte(read(t, filepath.Join(dir, entry.Name(), "receipt.json"))), &row); err != nil {
			t.Fatal(err)
		}
		rows = append(rows, row)
	}
	return rows
}

func TestWorkerPolicyDecisionReceipts(t *testing.T) {
	for _, c := range []struct{ path, kind string }{{"factory/keep.txt", "protected_path_refused"}, {"outside.txt", "scope_refused"}, {"approved.txt", ""}} {
		t.Run(c.path, func(t *testing.T) {
			root, job, wt := buildFixture(t)
			state := filepath.Join(job, "job.json")
			write(t, state, strings.Replace(read(t, state), "{", `{"origin":"controlled",`, 1))
			head := gitTest(t, wt, "rev-parse", "HEAD")
			fakeCodexResponse(t, "printf 'private-token' > "+c.path, validBuild)
			err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
			path := filepath.Join(job, receiptFile)
			if c.kind == "" {
				if err != nil {
					t.Fatal(err)
				}
				noFile(t, path)
				return
			}
			if err == nil {
				t.Fatal("candidate was accepted")
			}
			rows := receiptRows(t, path)
			rawBytes, _ := json.Marshal(rows)
			raw := string(rawBytes)
			if len(rows) != 1 {
				t.Fatalf("receipt rows %v %v", rows, err)
			}
			event := rows[0]
			for k, want := range map[string]any{"kind": c.kind, "round": float64(1), "job_id": "001-fixture", "origin": "controlled", "base_sha": head, "input_sha": head, "spec_digest": contentDigest([]byte(read(t, filepath.Join(job, "spec.md"))))} {
				if event[k] != want {
					t.Fatalf("%s: %v != %v", k, event[k], want)
				}
			}
			if len(event["policy_digest"].(string)) != 64 || !gitSHA.MatchString(event["candidate_sha"].(string)) || event["candidate_sha"] == head {
				t.Fatal("missing policy or rejected candidate binding")
			}
			if _, err := time.Parse("2006-01-02T15:04:05.000000Z", event["occurred_at"].(string)); err != nil {
				t.Fatal(err)
			}
			for _, secret := range []string{c.path, "private-token", root, validBuild} {
				if strings.Contains(raw, secret) {
					t.Fatal("receipt leaked input")
				}
			}
			if gitTest(t, wt, "rev-parse", "HEAD") != head {
				t.Fatal("candidate published")
			}
		})
	}
}

func TestWorkerPolicyRuntimeErrorIsNotAnIncident(t *testing.T) {
	root, job, _ := buildFixture(t)
	fakeCodexResponse(t, "printf x > outside.txt", validBuild)
	fakeCLI(t, "python3", "exit 1")
	err := run(root, []string{"001-fixture", "builder"}, &bytes.Buffer{})
	if err == nil || strings.Contains(err.Error(), "spec scope") {
		t.Fatalf("policy runtime error misclassified: %v", err)
	}
	noFile(t, filepath.Join(job, receiptFile))
}

func TestRecoveryReceiptSurvivesJournalDeletionAndReplay(t *testing.T) {
	root, job, wt := buildFixture(t)
	target := filepath.Join(job, "build.md")
	if err := os.Mkdir(target, 0700); err != nil {
		t.Fatal(err)
	}
	fakeCodexResponse(t, "printf x > approved.txt", validBuild)
	if err := run(root, []string{"001-fixture", "builder", "--replace"}, &bytes.Buffer{}); err == nil {
		t.Fatal("publication should be pending")
	}
	path := filepath.Join(job, receiptFile)
	pending := filepath.Join(job, "worker-round-1-builder.json.pending")
	journal := read(t, pending)
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("recovery should fail")
	}
	noFile(t, path)
	if err := os.Remove(target); err != nil {
		t.Fatal(err)
	}
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	noFile(t, pending)
	rows := receiptRows(t, path)
	if len(rows) != 1 {
		t.Fatalf("recovery receipts: %v", rows)
	}
	row := rows[0]
	if row["kind"] != "builder_publication_recovered" || row["candidate_sha"] != gitTest(t, wt, "rev-parse", "HEAD") || row["artifact_digest"] != contentDigest([]byte(validBuild)) || row["audit_digest"] != contentDigest([]byte(read(t, filepath.Join(job, "worker-round-1-builder.json")))) {
		t.Fatalf("bad recovery binding: %v", row)
	}
	// Exercise the operator's normal read-only entry point on actual worker bytes.
	launcher, err := filepath.Abs("../../../factory/bin/job")
	if err != nil {
		t.Fatal(err)
	}
	report := exec.Command(launcher, "recovery", "001-fixture")
	report.Dir = root
	raw, err := report.CombinedOutput()
	if err != nil {
		t.Fatalf("job recovery: %v %s", err, raw)
	}
	var view struct {
		Records []map[string]any `json:"worker_receipts"`
	}
	if err := json.Unmarshal(raw, &view); err != nil || len(view.Records) != 1 || view.Records[0]["verification"] != "unverified" || view.Records[0]["candidate_sha"] != row["candidate_sha"] || view.Records[0]["origin"] != "unknown" {
		t.Fatalf("worker/CLI contract: %v %v", view, err)
	}
	// Simulate loss of the last directory sync: outputs and receipt survived but the journal reappeared.
	write(t, pending, journal)
	// Unrelated HEAD must not produce a policy event, even if the stored
	// candidate now violates policy.
	candidate := gitTest(t, wt, "rev-parse", "HEAD")
	gitTest(t, wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "unrelated")
	unrelated := gitTest(t, wt, "rev-parse", "HEAD")
	// Policy may have been tightened while the journal remained. Revalidate
	// it, then retain the first successful publication identity and timestamp.
	config := filepath.Join(root, "factory", "config.json")
	originalPolicy := read(t, config)
	write(t, config, strings.Replace(originalPolicy, `"factory/"`, `"factory/", "approved.txt"`, 1))
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("unrelated HEAD accepted")
	}
	if len(receiptRows(t, path)) != 1 {
		t.Fatal("unrelated HEAD created a policy event")
	}
	gitTest(t, wt, "update-ref", "HEAD", candidate, unrelated)

	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("updated policy was bypassed")
	}
	write(t, config, strings.Replace(originalPolicy, `"factory/"`, `"factory/", "new-protected-prefix/"`, 1))
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	replay := receiptRows(t, path)
	if len(replay) != 2 {
		t.Fatalf("duplicate recovery %v", replay)
	}
	kinds := map[string]bool{}
	for _, record := range replay {
		kinds[record["kind"].(string)] = true
		if record["kind"] == "builder_publication_recovered" && (record["occurred_at"] != row["occurred_at"] || record["policy_digest"] != row["policy_digest"]) {
			t.Fatal("replay overwrote original publication")
		}
		if record["kind"] == "recovery_protected_path_refused" && record["worktree_position"] != "candidate" {
			t.Fatal("already-published candidate described incorrectly")
		}
	}
	if !kinds["builder_publication_recovered"] || !kinds["recovery_protected_path_refused"] {
		t.Fatalf("incorrect event types: %v", kinds)
	}
	noFile(t, pending)
	// A hardlink to an existing final record must leave the journal intact.
	names, err := os.ReadDir(path)
	if err != nil {
		t.Fatal(err)
	}
	var receipt string
	for _, entry := range names {
		if entry.Name() == ".staging" {
			continue
		}
		f := filepath.Join(path, entry.Name(), "receipt.json")
		if strings.Contains(read(t, f), `"builder_publication_recovered"`) {
			receipt = f
		}
	}
	other := filepath.Join(job, "unrelated-link")
	if err := os.Link(receipt, other); err != nil {
		t.Fatal(err)
	}
	write(t, pending, journal)
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("hardlinked record accepted")
	}
	wantFile(t, pending, journal)
	if err := os.Remove(other); err != nil {
		t.Fatal(err)
	}
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	noFile(t, pending)
}

func TestRecoveryRefusesMismatchedAuditAndRetainsJournalOnReceiptFailure(t *testing.T) {
	root, job, _ := buildFixture(t)
	build := filepath.Join(job, "build.md")
	if err := os.Mkdir(build, 0700); err != nil {
		t.Fatal(err)
	}
	fakeCodexResponse(t, "printf x > approved.txt", validBuild)
	if err := run(root, []string{"001-fixture", "builder", "--replace"}, &bytes.Buffer{}); err == nil {
		t.Fatal("publication should be pending")
	}
	if err := os.Remove(build); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(job, receiptFile)
	pending := filepath.Join(job, "worker-round-1-builder.json.pending")
	journal := read(t, pending)
	var original publication
	if err := json.Unmarshal([]byte(journal), &original); err != nil {
		t.Fatal(err)
	}
	original.Audit = []byte(`{"role":"intake"}`)
	changed, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	write(t, pending, string(changed))
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("forged audit accepted")
	}
	noFile(t, path)
	wantFile(t, pending, string(changed))
	write(t, pending, journal)
	write(t, path, "damaged receipt directory")
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err == nil {
		t.Fatal("receipt publication failed silently")
	}
	wantFile(t, pending, journal)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 1024; i++ {
		write(t, filepath.Join(path, fmt.Sprintf("%064x.json", i)), "{}")
	}
	if err := run(root, []string{"--recover", "001-fixture"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	noFile(t, pending)
	if files, err := os.ReadDir(path); err != nil || len(files) != 1026 {
		t.Fatal("receipt missing")
	}
}

func TestConcurrentReceiptPublicationPreservesDistinctKeysAndFirstPublication(t *testing.T) {
	dir, err := os.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer dir.Close()
	state := jobState{ID: "001-fixture", Round: 1}
	event := newWorkerEvent(state, "scope_refused", strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40), strings.Repeat("d", 64), strings.Repeat("e", 64))
	other := event
	other.Candidate = strings.Repeat("f", 40)
	ready := make(chan struct{})
	results := make(chan error, 3)
	for _, row := range []workerEvent{event, event, other} {
		go func(e workerEvent) { <-ready; results <- saveWorkerEvent(dir, e) }(row)
	}
	close(ready)
	for i := 0; i < 3; i++ {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	rows := receiptRows(t, filepath.Join(dir.Name(), receiptFile))
	if len(rows) != 2 {
		t.Fatal("concurrent publication lost or duplicated a receipt")
	}
}

// Kill a publisher at the rename boundary. Only its private staging directory
// may survive; the next ordinary refusal can publish normally.
func TestReceiptPublisherDeathBeforeRename(t *testing.T) {
	const marker = "FACTORY_TEST_RECEIPT_CRASH_DIR"
	event := newWorkerEvent(jobState{ID: "001-fixture", Round: 1}, "scope_refused", strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40), strings.Repeat("d", 64), strings.Repeat("e", 64))
	if folder := os.Getenv(marker); folder != "" {
		dir, err := os.Open(folder)
		if err != nil {
			t.Fatal(err)
		}
		defer dir.Close()
		t.Fatal("publisher survived crash barrier", saveWorkerEvent(dir, event))
	}
	folder := t.TempDir()
	original := os.Getenv("PATH")
	fakeCLI(t, "python3", `kill -KILL "$PPID"; exit 99`)
	child := exec.Command(os.Args[0], "-test.run=^TestReceiptPublisherDeathBeforeRename$")
	child.Env = append(os.Environ(), marker+"="+folder)
	err := child.Run()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
		t.Fatalf("barrier did not kill publisher: %v", err)
	}
	store := filepath.Join(folder, receiptFile)
	entries, err := os.ReadDir(store)
	if err != nil || len(entries) != 1 || entries[0].Name() != ".staging" {
		t.Fatalf("crash exposed final receipt: %v %v", entries, err)
	}
	staged, err := os.ReadDir(filepath.Join(store, ".staging"))
	if err != nil || len(staged) != 1 {
		t.Fatalf("crash fixture did not reach staged publication: %v %v", staged, err)
	}
	t.Setenv("PATH", original)
	dir, err := os.Open(folder)
	if err != nil {
		t.Fatal(err)
	}
	defer dir.Close()
	if err := saveWorkerEvent(dir, event); err != nil {
		t.Fatal(err)
	}
	if len(receiptRows(t, store)) != 1 {
		t.Fatal("retry did not publish one receipt")
	}
}
