//go:build linux

package ingest

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestSweepRemovesLeasedOrphan(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "support-triage-ingest-orphan")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }})
	if _, err := os.Lstat(dir); !os.IsNotExist(err) {
		t.Fatalf("orphan remains: %v", err)
	}
}

func TestSweepCutoffAndFresh(t *testing.T) {
	base := t.TempDir()
	now := time.Now()
	for _, tc := range []struct {
		name string
		age  time.Duration
	}{{"cutoff", 24 * time.Hour}, {"fresh", time.Hour}} {
		dir := filepath.Join(base, scratchPrefix+tc.name)
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
			t.Fatal(err)
		}
		stamp := now.Add(-tc.age)
		if err := os.Chtimes(dir, stamp, stamp); err != nil {
			t.Fatal(err)
		}
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }})
	for _, name := range []string{"cutoff", "fresh"} {
		if _, err := os.Stat(filepath.Join(base, scratchPrefix+name)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSweepLegacyAttributed(t *testing.T) {
	// This private base stands for one historically exclusive to the ingester,
	// with no old ingester using or able to start against it.
	base := t.TempDir()
	dir := filepath.Join(base, "ingest-123")
	now := time.Now()
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"run-1", "kept-2"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0600); err != nil {
			t.Fatal(err)
		}
	}
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	cfg := sweepConfig{now: func() time.Time { return now }}
	sweepTemp(base, cfg)
	if _, err := os.Stat(dir); err != nil {
		t.Fatal(err)
	}
	cfg.legacy = true
	sweepTemp(base, cfg)
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("legacy remains: %v", err)
	}
}

func TestSweepConcurrentReplacement(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, scratchPrefix+"candidate")
	original := filepath.Join(base, "original")
	now := time.Now()
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".lease"), []byte(leaseMarker), 0600); err != nil {
		t.Fatal(err)
	}
	old := now.Add(-25 * time.Hour)
	if err := os.Chtimes(dir, old, old); err != nil {
		t.Fatal(err)
	}
	sweepTemp(base, sweepConfig{now: func() time.Time { return now }, beforeMove: func() bool {
		if err := os.Rename(dir, original); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "valuable"), []byte("keep"), 0600); err != nil {
			t.Fatal(err)
		}
		return false
	}})
	if _, err := os.Stat(filepath.Join(original, ".lease")); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(base)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".support-triage-ingest-sweep-") {
			data, err := os.ReadFile(filepath.Join(base, e.Name(), "victim", "valuable"))
			if err != nil || string(data) != "keep" {
				t.Fatalf("replacement lost: %q %v", data, err)
			}
			found = true
		}
	}
	if !found {
		t.Fatal("replacement not preserved in staging")
	}
}

func testOrphan(t *testing.T, base, name string, marker *string) string {
	t.Helper()
	dir := filepath.Join(base, name)
	must(t, os.Mkdir(dir, 0700))
	if marker != nil {
		must(t, os.WriteFile(filepath.Join(dir, ".lease"), []byte(*marker), 0600))
	}
	old := time.Now().Add(-25 * time.Hour)
	must(t, os.Chtimes(dir, old, old))
	return dir
}
func testExists(t *testing.T, p string) {
	t.Helper()
	if _, err := os.Lstat(p); err != nil {
		t.Fatalf("%s: %v", p, err)
	}
}
func testLive(t *testing.T, b string) string {
	t.Helper()
	es, err := os.ReadDir(b)
	must(t, err)
	for _, e := range es {
		if strings.HasPrefix(e.Name(), scratchPrefix) {
			return filepath.Join(b, e.Name())
		}
	}
	t.Fatal("no live directory")
	return ""
}
func testMarker(s string) *string { return &s }
func testCSV(t *testing.T) string { t.Helper(); return writeCSV(t, csvOf([]tweetSpec{cust(1, 0)})) }
func testFuture() time.Time       { return time.Now().Add(48 * time.Hour) }

func TestSweepSkipsActiveRun(t *testing.T) {
	b := t.TempDir()
	path := testCSV(t)
	called := false
	_, err := BuildThreads(path, Options{TempDir: b}, func(Thread) error {
		called = true
		dir := testLive(t, b)
		sweepTemp(b, sweepConfig{now: testFuture})
		testExists(t, dir)
		_, err := BuildThreads(path, Options{TempDir: b}, func(Thread) error { return nil })
		must(t, err)
		testExists(t, dir)
		return nil
	})
	must(t, err)
	if !called {
		t.Fatal("no emit")
	}
	assertEmptyDir(t, b)
}
func TestSweepLeaseGaps(t *testing.T) {
	b := t.TempDir()
	for i, m := range []*string{testMarker(""), testMarker("wrong"), nil} {
		dir := testOrphan(t, b, fmt.Sprintf("%s%d", scratchPrefix, i), m)
		sweepTemp(b, sweepConfig{})
		testExists(t, dir)
	}
	for _, gap := range []string{"create", "lock"} {
		t.Run(gap, func(t *testing.T) {
			b := t.TempDir()
			called := false
			hook := func() {
				called = true
				dir := testLive(t, b)
				sweepTemp(b, sweepConfig{now: testFuture})
				testExists(t, dir)
			}
			if gap == "create" {
				leaseHooks.afterLeaseCreate = hook
			} else {
				leaseHooks.afterLock = hook
			}
			defer func() { leaseHooks = sweepConfig{} }()
			_, err := BuildThreads(testCSV(t), Options{TempDir: b}, func(Thread) error { return nil })
			must(t, err)
			if !called {
				t.Fatal("hook not called")
			}
		})
	}
}
func TestLeaseFailureKeepsRunIneligible(t *testing.T) {
	path := testCSV(t)
	var want []Thread
	stats, err := BuildThreads(path, Options{TempDir: t.TempDir()}, func(th Thread) error { want = append(want, th); return nil })
	must(t, err)
	original := acquireLease
	defer func() { acquireLease = original }()
	acquireLease = func(dir string, _ sweepConfig) *os.File {
		must(t, os.WriteFile(filepath.Join(dir, ".lease"), nil, 0600))
		return nil
	}
	b := t.TempDir()
	var got []Thread
	gotStats, err := BuildThreads(path, Options{TempDir: b}, func(th Thread) error {
		got = append(got, th)
		dir := testLive(t, b)
		sweepTemp(b, sweepConfig{now: testFuture})
		testExists(t, dir)
		return nil
	})
	must(t, err)
	if !reflect.DeepEqual(got, want) || !reflect.DeepEqual(gotStats, stats) {
		t.Fatal("threads or stats changed")
	}
}
func TestSweepPreservesSymlinksAndUnrelated(t *testing.T) {
	b := t.TempDir()
	outside := t.TempDir()
	must(t, os.WriteFile(filepath.Join(outside, "valuable"), []byte("keep"), 0600))
	target := filepath.Join(b, "target")
	must(t, os.Mkdir(target, 0700))
	must(t, os.WriteFile(filepath.Join(target, "valuable"), []byte("keep"), 0600))
	for _, x := range []struct{ name, target string }{{"link", target}, {"outside", outside}} {
		must(t, os.Symlink(x.target, filepath.Join(b, scratchPrefix+x.name)))
	}
	file := filepath.Join(b, scratchPrefix+"file")
	must(t, os.WriteFile(file, []byte("keep"), 0600))
	unrelated := testOrphan(t, b, "unrelated", nil)
	orphan := testOrphan(t, b, scratchPrefix+"orphan", testMarker(leaseMarker))
	must(t, os.Symlink(outside, filepath.Join(orphan, "link")))
	old := time.Now().Add(-25 * time.Hour)
	must(t, os.Chtimes(orphan, old, old))
	sweepTemp(b, sweepConfig{})
	if _, err := os.Lstat(orphan); !os.IsNotExist(err) {
		t.Fatalf("orphan remains: %v", err)
	}
	for _, p := range []string{filepath.Join(b, scratchPrefix+"link"), filepath.Join(b, scratchPrefix+"outside"), file, unrelated, filepath.Join(target, "valuable"), filepath.Join(outside, "valuable")} {
		testExists(t, p)
	}
}
func TestSweepLegacyAmbiguousKept(t *testing.T) {
	b := t.TempDir()
	a := testOrphan(t, b, "ingest-abc", nil)
	sub := testOrphan(t, b, "ingest-7", nil)
	must(t, os.Mkdir(filepath.Join(sub, "nested"), 0700))
	bad := testOrphan(t, b, "ingest-8", nil)
	must(t, os.WriteFile(filepath.Join(bad, "other"), nil, 0600))
	old := time.Now().Add(-25 * time.Hour)
	for _, p := range []string{sub, bad} {
		must(t, os.Chtimes(p, old, old))
	}
	sweepTemp(b, sweepConfig{legacy: true})
	for _, p := range []string{a, sub, bad} {
		testExists(t, p)
	}
	private := testOrphan(t, b, "ingest-123", nil)
	sweepTemp(b, sweepConfig{})
	testExists(t, private)
	shared := t.TempDir()
	must(t, os.Chmod(shared, 0777))
	d := testOrphan(t, shared, "ingest-9", nil)
	sweepTemp(shared, sweepConfig{legacy: true})
	testExists(t, d)
}
func TestSweepCancellationBeforeMove(t *testing.T) {
	b := t.TempDir()
	dir := testOrphan(t, b, scratchPrefix+"cancel", testMarker(leaseMarker))
	sweepTemp(b, sweepConfig{beforeMove: func() bool { return true }})
	testExists(t, filepath.Join(dir, ".lease"))
	es, err := os.ReadDir(b)
	must(t, err)
	if len(es) != 1 {
		t.Fatalf("unexpected staging entries: %v", es)
	}
}

func TestSweepBaseRootGuard(t *testing.T) {
	b := t.TempDir()
	outside := t.TempDir()
	d := testOrphan(t, outside, scratchPrefix+"outside", testMarker(leaseMarker))
	link := filepath.Join(b, "link")
	must(t, os.Symlink(outside, link))
	sweepTemp(link, sweepConfig{})
	testExists(t, d)
	original := filepath.Join(b, "base")
	must(t, os.Mkdir(original, 0700))
	testOrphan(t, original, scratchPrefix+"candidate", testMarker(leaseMarker))
	moved := filepath.Join(b, "moved")
	sweepTemp(original, sweepConfig{beforeMove: func() bool { must(t, os.Rename(original, moved)); must(t, os.Symlink(outside, original)); return false }})
	testExists(t, d)
}
func TestSweepFailuresDoNotFailIngestion(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses permission checks")
	}
	b := t.TempDir()
	dir := testOrphan(t, b, scratchPrefix+"unreadable", testMarker(leaseMarker))
	must(t, os.Chmod(dir, 0))
	defer os.Chmod(dir, 0700)
	count := 0
	_, err := BuildThreads(testCSV(t), Options{TempDir: b}, func(Thread) error { count++; return nil })
	must(t, err)
	testExists(t, dir)
	if count != 1 {
		t.Fatalf("got %d threads", count)
	}
	must(t, os.Chmod(b, 0300))
	_, err = BuildThreads(testCSV(t), Options{TempDir: b}, func(Thread) error { return nil })
	must(t, os.Chmod(b, 0700))
	must(t, err)
	testExists(t, dir)
}
func TestBuildThreadsKeepsUnrelatedEntries(t *testing.T) {
	b := t.TempDir()
	p := filepath.Join(b, "unrelated")
	must(t, os.WriteFile(p, []byte("keep"), 0600))
	_, err := BuildThreads(testCSV(t), Options{TempDir: b}, func(Thread) error { return nil })
	must(t, err)
	data, err := os.ReadFile(p)
	must(t, err)
	if string(data) != "keep" {
		t.Fatal("content changed")
	}
}

func TestSweepMountRefusalPreservesContents(t *testing.T) {
	b := t.TempDir()
	dir := testOrphan(t, b, scratchPrefix+"mount", testMarker(leaseMarker))
	must(t, os.WriteFile(filepath.Join(dir, "valuable"), []byte("keep"), 0600))
	must(t, os.Mkdir(filepath.Join(dir, "nested"), 0700))
	old := time.Now().Add(-25 * time.Hour)
	must(t, os.Chtimes(dir, old, old))
	original := openNoMount
	defer func() { openNoMount = original }()
	openNoMount = func(fd int, name string, flags int) (int, error) {
		if name == "nested" {
			return -1, syscall.EXDEV
		}
		return original(fd, name, flags)
	}
	sweepTemp(b, sweepConfig{})
	testExists(t, filepath.Join(dir, "valuable"))
	testExists(t, filepath.Join(dir, "nested"))
}
