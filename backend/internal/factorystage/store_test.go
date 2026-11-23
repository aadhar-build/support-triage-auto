package factorystage

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func sampleEvent(id string) Event {
	return Event{
		EventID: id, JobID: "job-21", Round: 2, AttemptID: "attempt-3",
		JobRevision: "rev-4", Stage: "build", Status: "finished", Outcome: "passed",
		EvidenceRef: "receipts/r2/attempt-3.json", Provider: "codex", Model: "test-model",
		Reason: "independent checks passed", OperationID: "operation-5", SupersedesAttemptID: "attempt-2",
		OccurredAt: time.Date(2026, 11, 22, 7, 0, 0, 250, time.UTC),
	}
}

func openStore(t *testing.T, dir string) *Store {
	t.Helper()
	store, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func appendNew(t *testing.T, store *Store, event Event) {
	t.Helper()
	if added, err := store.Append(event); err != nil || !added {
		t.Fatalf("Append = (%t, %v), want (true, nil)", added, err)
	}
}

func TestReplayPreservesCommittedOrderAndIdentity(t *testing.T) {
	dir := t.TempDir()
	store := openStore(t, dir)
	if events, err := store.ReadAll(); err != nil || len(events) != 0 {
		t.Fatalf("new store ReadAll = (%v, %v)", events, err)
	}

	first := sampleEvent("event-z")
	second := sampleEvent("event-a")
	second.OccurredAt = second.OccurredAt.Add(-time.Hour)
	second.AttemptID = "attempt-4"
	second.SupersedesAttemptID = first.AttemptID
	second.Provider = ""
	second.Model = ""
	second.Reason = ""
	second.OperationID = ""
	appendNew(t, store, first)
	appendNew(t, store, second)

	store = openStore(t, dir)
	events, err := store.ReadAll()
	if err != nil || len(events) != 2 || events[0] != first || events[1] != second {
		t.Fatalf("ReadAll = (%+v, %v), want the two events in append order", events, err)
	}
}

func TestReplayPreservesRoundZeroBeforeFirstBuild(t *testing.T) {
	dir := t.TempDir()
	event := sampleEvent("spec-accepted")
	event.Round = 0
	event.Stage = "spec"
	event.Status = "finished"
	event.Outcome = "accepted"
	event.EvidenceRef = "spec.md"
	event.SupersedesAttemptID = ""
	appendNew(t, openStore(t, dir), event)

	events, err := openStore(t, dir).ReadAll()
	if err != nil || len(events) != 1 || events[0] != event {
		t.Fatalf("ReadAll = (%+v, %v), want the round-zero specification event", events, err)
	}
}

func TestAppendIdempotentAcrossReopen(t *testing.T) {
	dir := t.TempDir()
	event := sampleEvent("event-1")
	appendNew(t, openStore(t, dir), event)
	logPath := filepath.Join(dir, "stage-events-v1.jsonl")
	before, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}

	store := openStore(t, dir)
	retry := event
	retry.OccurredAt = event.OccurredAt.In(time.FixedZone("offset", 3600))
	if added, err := store.Append(retry); err != nil || added {
		t.Fatalf("identical Append = (%t, %v), want (false, nil)", added, err)
	}
	conflicts := []Event{event, event, event}
	conflicts[0].Outcome = "failed"
	conflicts[1].JobRevision = "rev-6"
	conflicts[2].OccurredAt = event.OccurredAt.Add(time.Nanosecond)
	for _, conflict := range conflicts {
		if added, err := store.Append(conflict); !errors.Is(err, ErrConflict) || added {
			t.Fatalf("conflicting Append = (%t, %v), want (false, ErrConflict)", added, err)
		}
	}

	after, err := os.ReadFile(logPath)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatalf("log bytes changed on retry or conflict: %v", err)
	}
}

func TestAppendRejectsInvalidEvents(t *testing.T) {
	store := openStore(t, t.TempDir())
	cases := map[string]func(*Event){
		"event id":      func(e *Event) { e.EventID = "" },
		"job id":        func(e *Event) { e.JobID = "" },
		"attempt id":    func(e *Event) { e.AttemptID = " " },
		"round":         func(e *Event) { e.Round = -1 },
		"job revision":  func(e *Event) { e.JobRevision = "" },
		"stage":         func(e *Event) { e.Stage = "build\ngate" },
		"status":        func(e *Event) { e.Status = "" },
		"outcome":       func(e *Event) { e.Outcome = "" },
		"evidence":      func(e *Event) { e.EvidenceRef = "" },
		"time":          func(e *Event) { e.OccurredAt = time.Time{} },
		"invalid time":  func(e *Event) { e.OccurredAt = time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC) },
		"long reason":   func(e *Event) { e.Reason = strings.Repeat("x", 513) },
		"invalid UTF-8": func(e *Event) { e.Reason = "\xff" },
		"own attempt":   func(e *Event) { e.SupersedesAttemptID = e.AttemptID },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			event := sampleEvent("event-1")
			change(&event)
			if added, err := store.Append(event); !errors.Is(err, ErrInvalidEvent) || added {
				t.Fatalf("Append = (%t, %v), want (false, ErrInvalidEvent)", added, err)
			}
		})
	}
	if events, err := store.ReadAll(); err != nil || len(events) != 0 {
		t.Fatalf("ReadAll after invalid inputs = (%v, %v)", events, err)
	}
}

func TestOpenRequiresExistingDirectory(t *testing.T) {
	if _, err := Open(""); err == nil {
		t.Fatal("Open accepted an empty path")
	}
	dir := t.TempDir()
	if _, err := Open(filepath.Join(dir, "missing")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("Open missing path = %v, want os.ErrNotExist", err)
	}
	file := filepath.Join(dir, "file")
	if err := os.WriteFile(file, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(file); err == nil {
		t.Fatal("Open accepted a regular file")
	}
}

func TestOpenRefusesUnsafeDirectory(t *testing.T) {
	dir := t.TempDir()
	alias := filepath.Join(t.TempDir(), "alias")
	if err := os.Symlink(dir, alias); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(alias); err == nil {
		t.Fatal("Open accepted a symlink")
	}
	for _, mode := range []os.FileMode{0770, 0702} {
		if err := os.Chmod(dir, mode); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(dir); err == nil {
			t.Errorf("Open accepted directory mode %#o", mode)
		}
	}
}

func TestStoreRefusesReplacementAndPermissionChange(t *testing.T) {
	path := filepath.Join(t.TempDir(), "store")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	store := openStore(t, path)
	original := sampleEvent("original-store-event")
	appendNew(t, store, original)

	if err := os.Chmod(path, 0770); err != nil {
		t.Fatal(err)
	}
	if events, err := store.ReadAll(); err == nil || events != nil {
		t.Fatalf("ReadAll after permission change = (%v, %v), want error", events, err)
	}
	if err := os.Chmod(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path, path+"-original"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}

	if events, err := store.ReadAll(); err == nil || events != nil {
		t.Fatalf("ReadAll after replacement = (%v, %v), want error", events, err)
	}
	if added, err := store.Append(sampleEvent("replacement-event")); err == nil || added {
		t.Fatalf("Append after replacement = (%t, %v), want error", added, err)
	}
	if events, err := openStore(t, path).ReadAll(); err != nil || len(events) != 0 {
		t.Fatalf("replacement store ReadAll = (%v, %v), want empty", events, err)
	}
	if events, err := openStore(t, path+"-original").ReadAll(); err != nil || len(events) != 1 || events[0] != original {
		t.Fatalf("original store ReadAll = (%v, %v), want original event", events, err)
	}
}

func TestInterruptedPublicationLeavesCommittedEventsReadable(t *testing.T) {
	dir := t.TempDir()
	first, second := sampleEvent("event-1"), sampleEvent("event-2")
	appendNew(t, openStore(t, dir), first)
	cmd := exec.Command(os.Args[0], "-test.run=^TestStageInterruptionProcess$")
	cmd.Env = []string{"FACTORY_STAGE_TEST_DIR=" + dir}
	cmd.Stderr = os.Stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()
	if line, err := bufio.NewReader(stdout).ReadString('\n'); err != nil || line != "staged\n" {
		t.Fatalf("child staging = (%q, %v), want staged", line, err)
	}

	probe, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	lockErr := syscall.Flock(int(probe.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	probe.Close()
	if !errors.Is(lockErr, syscall.EWOULDBLOCK) {
		t.Fatalf("child did not hold an exclusive lock: %v", lockErr)
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Wait(); err == nil {
		t.Fatal("interrupted child exited successfully")
	}

	restarted := openStore(t, dir)
	events, err := restarted.ReadAll()
	if err != nil || len(events) != 1 || events[0] != first {
		t.Fatalf("ReadAll before retry = (%v, %v), want first committed event", events, err)
	}
	appendNew(t, restarted, second)
	events, err = restarted.ReadAll()
	if err != nil || len(events) != 2 || events[1] != second {
		t.Fatalf("ReadAll after retry = (%v, %v), want both events", events, err)
	}
}

func TestStageInterruptionProcess(t *testing.T) {
	dir := os.Getenv("FACTORY_STAGE_TEST_DIR")
	if dir == "" {
		return
	}
	store := openStore(t, dir)
	lock, err := store.lockDirectory(syscall.LOCK_EX)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	// Model a process interruption while writing its unpublished snapshot.
	if err := os.WriteFile(filepath.Join(dir, ".stage-events-abandoned"), []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	fmt.Println("staged")
	time.Sleep(30 * time.Second)
	t.Fatal("interruption test process did not stop")
}

func TestMalformedCommittedStoreFailsClosed(t *testing.T) {
	cases := map[string]func([]byte) []byte{
		"empty":           func(_ []byte) []byte { return nil },
		"truncated":       func(valid []byte) []byte { return append(valid, []byte("{\"event_id\":")...) },
		"missing newline": func(valid []byte) []byte { return valid[:len(valid)-1] },
		"malformed":       func(valid []byte) []byte { return append(valid, []byte("{bad}\n")...) },
		"invalid fields":  func(valid []byte) []byte { return append(valid, []byte("{}\n")...) },
		"duplicate lines": func(valid []byte) []byte { return append(valid, valid...) },
		"unknown field": func(valid []byte) []byte {
			return append([]byte("{\"additional\":true,"), valid[1:]...)
		},
		"duplicate field": func(valid []byte) []byte {
			return append([]byte("{\"event_id\":\"ignored\","), valid[1:]...)
		},
	}
	for name, damage := range cases {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			store := openStore(t, dir)
			appendNew(t, store, sampleEvent("event-1"))
			logPath := filepath.Join(dir, "stage-events-v1.jsonl")
			valid, err := os.ReadFile(logPath)
			if err != nil {
				t.Fatal(err)
			}
			bad := damage(valid)
			if err := os.WriteFile(logPath, bad, 0600); err != nil {
				t.Fatal(err)
			}

			if events, err := store.ReadAll(); !errors.Is(err, ErrCorrupt) || len(events) != 0 {
				t.Fatalf("ReadAll = (%v, %v), want no partial history and ErrCorrupt", events, err)
			}
			if added, err := store.Append(sampleEvent("event-2")); !errors.Is(err, ErrCorrupt) || added {
				t.Fatalf("Append = (%t, %v), want (false, ErrCorrupt)", added, err)
			}
			after, err := os.ReadFile(logPath)
			if err != nil || !bytes.Equal(bad, after) {
				t.Fatalf("damaged log changed: %v", err)
			}
		})
	}
}

func TestReadAllRefusesSymlinkAtPublishedPath(t *testing.T) {
	source := t.TempDir()
	appendNew(t, openStore(t, source), sampleEvent("event-1"))
	dir := t.TempDir()
	if err := os.Symlink(filepath.Join(source, logName), filepath.Join(dir, logName)); err != nil {
		t.Fatal(err)
	}
	events, err := openStore(t, dir).ReadAll()
	if !errors.Is(err, ErrCorrupt) || len(events) != 0 {
		t.Fatalf("ReadAll through symlink = (%v, %v), want ErrCorrupt", events, err)
	}
}

func TestStoreEnforcesSizeLimitWithoutDiscardingEvents(t *testing.T) {
	dir := t.TempDir()
	store := openStore(t, dir)
	last := sampleEvent("last-at-limit")
	lastLine, err := json.Marshal(last)
	if err != nil {
		t.Fatal(err)
	}
	history, count := makeSizedStageHistory(t, maxLogBytes-len(lastLine)-1)
	path := filepath.Join(dir, logName)
	if err := os.WriteFile(path, history, 0600); err != nil {
		t.Fatal(err)
	}

	appendNew(t, store, last)
	if added, err := store.Append(last); err != nil || added {
		t.Fatalf("identical append at the limit = (%t, %v), want (false, nil)", added, err)
	}
	if added, err := store.Append(sampleEvent("over-limit")); !errors.Is(err, ErrLogTooLarge) || added {
		t.Fatalf("append past the limit = (%t, %v), want (false, ErrLogTooLarge)", added, err)
	}
	events, err := store.ReadAll()
	if err != nil || len(events) != count+1 || events[len(events)-1] != last {
		t.Fatalf("replay at the limit got %d events, error %v, want %d including the last event", len(events), err, count+1)
	}
	expected := append(append(history, lastLine...), '\n')
	committed, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(committed, expected) {
		t.Fatalf("log length after refused append = %d, error %v, want original %d bytes", len(committed), err, len(expected))
	}
}

func TestOversizedPublishedLogReturnsNoHistory(t *testing.T) {
	dir := t.TempDir()
	store := openStore(t, dir)
	appendNew(t, store, sampleEvent("first-record"))
	path := filepath.Join(dir, logName)
	if err := os.Truncate(path, maxLogBytes+1); err != nil {
		t.Fatal(err)
	}

	if events, err := store.ReadAll(); !errors.Is(err, ErrLogTooLarge) || events != nil {
		t.Fatalf("oversized log replay returned %d events, error %v, want no events and ErrLogTooLarge", len(events), err)
	}
	if added, err := store.Append(sampleEvent("ignored-record")); !errors.Is(err, ErrLogTooLarge) || added {
		t.Fatalf("append to oversized log = (%t, %v), want (false, ErrLogTooLarge)", added, err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Size() != maxLogBytes+1 {
		t.Fatalf("oversized log modified: %v, error %v", info, err)
	}
}

func makeSizedStageHistory(t *testing.T, size int) ([]byte, int) {
	t.Helper()
	event := sampleEvent("filler-000000")
	event.EvidenceRef = "x"
	small, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	maxLine := len(small) + maxEvidenceBytes
	count := (size + maxLine - 1) / maxLine
	history := make([]byte, 0, size)
	for i := range count {
		lineSize := size / count
		if i < size%count {
			lineSize++
		}
		event.EventID = fmt.Sprintf("filler-%06d", i)
		event.EvidenceRef = strings.Repeat("x", lineSize-len(small))
		line, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		history = append(append(history, line...), '\n')
	}
	if len(history) != size {
		t.Fatalf("fixture has %d bytes, want %d", len(history), size)
	}
	return history, count
}

func TestConcurrentAppendExcludesDuplicatesWithoutLosingEvents(t *testing.T) {
	dir := t.TempDir()
	const count = 24
	stores := make([]*Store, count)
	for i := range stores {
		stores[i] = openStore(t, dir)
	}
	type result struct {
		added bool
		err   error
	}
	results := make(chan result, count*2)
	start := make(chan struct{})
	var waiting sync.WaitGroup
	for i, store := range stores {
		waiting.Add(1)
		go func() {
			defer waiting.Done()
			<-start
			added, err := store.Append(sampleEvent("shared"))
			results <- result{added, err}
			added, err = store.Append(sampleEvent(fmt.Sprintf("unique-%d", i)))
			results <- result{added, err}
		}()
	}
	close(start)
	waiting.Wait()
	close(results)

	written := 0
	for res := range results {
		if res.err != nil {
			t.Fatal(res.err)
		}
		if res.added {
			written++
		}
	}
	events, err := openStore(t, dir).ReadAll()
	if err != nil || len(events) != count+1 || written != count+1 {
		t.Fatalf("ReadAll returned %d events, %d appends and error %v, want %d events and appends", len(events), written, err, count+1)
	}
}
