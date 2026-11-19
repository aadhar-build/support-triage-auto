package model

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// pyReserve builds SpendLog(path, 1.0), says ready, then reserves 0.25 (real clock).
const pyReserve = `import sys
from triage.spend_log import SpendLog
sl = SpendLog(sys.argv[1], 1.0)
print("ready", flush=True)
print(sl.reserve("py", "m", "dev", 0.25), flush=True)
`

// TestLockSharedWithPython: Python's fcntl.flock waits on model's lock.
func TestLockSharedWithPython(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Fatal("python3 is required: ", err)
	}
	e := newEnv(t)
	e.create(time.Now())
	// Hold the lock through the model's own withLock, so the test fails if
	// model stops taking the flock(2) lock Python's fcntl.flock takes.
	held, release, done := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	go func() {
		done <- e.log(1.0, time.Now()).withLock(func(*os.File, []event) error {
			close(held)
			<-release
			return nil
		})
	}()
	released := false
	unlock := func() {
		if !released {
			released = true
			close(release)
			if err := <-done; err != nil {
				t.Errorf("withLock: %v", err)
			}
		}
	}
	t.Cleanup(unlock)
	select {
	case <-held:
	case err := <-done:
		t.Fatalf("withLock returned before holding: %v", err)
	}
	cmd := exec.Command("python3", "-c", pyReserve, e.path)
	cmd.Env = append(os.Environ(), "PYTHONPATH="+filepath.Join(e.repo, "src"), "PYTHONDONTWRITEBYTECODE=1")
	lines, exited := startChild(t, cmd)
	expectLine(t, lines, "ready")
	stillRunning(t, exited)
	unlock()
	select {
	case err := <-exited:
		if err != nil {
			t.Fatalf("python child: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("python child did not exit within 10s of the release")
	}
	if mtd, err := e.log(1.0, time.Now()).MonthToDate(); err != nil || mtd != 0.25 {
		t.Errorf("Go month to date %v %v, want 0.25 (the Python reservation)", mtd, err)
	}
}
