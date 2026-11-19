package main

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const workerEventDir = "worker-events-v1"
const workerEventTime = "2006-01-02T15:04:05.000000Z"

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

type pathRefusal struct{ kind, policyDigest string }

func (*pathRefusal) Error() string {
	return "candidate refused by the existing protected paths or spec scope rules"
}

// Records contain only controller identifiers, fixed decision labels and digests.
type workerEvent struct {
	Kind           string `json:"kind"`
	JobID          string `json:"job_id"`
	Origin         string `json:"origin"`
	Round          int    `json:"round"`
	Input          string `json:"input_sha"`
	Candidate      string `json:"candidate_sha"`
	Base           string `json:"base_sha"`
	Spec           string `json:"spec_digest"`
	Policy         string `json:"policy_digest"`
	Position       string `json:"worktree_position,omitempty"`
	ArtifactDigest string `json:"artifact_digest,omitempty"`
	AuditDigest    string `json:"audit_digest,omitempty"`
	At             string `json:"occurred_at"`
}

func newWorkerEvent(state jobState, kind, input, candidate, base, spec, policy string) workerEvent {
	origin := state.Origin
	if origin != "real" && origin != "controlled" {
		origin = "unknown"
	}
	return workerEvent{Kind: kind, JobID: state.ID, Origin: origin, Round: state.Round, Input: input, Candidate: candidate, Base: base, Spec: spec, Policy: policy}
}

func (e workerEvent) key() string {
	fields := []string{"worker-events-v1", e.Kind, e.JobID, e.Origin, strconv.Itoa(e.Round), e.Input, e.Candidate, e.Base, e.Spec}
	if e.Kind == "builder_publication_recovered" {
		fields = append(fields, e.AuditDigest, e.ArtifactDigest)
	} else {
		fields = append(fields, e.Policy, e.Position)
	}
	return contentDigest([]byte(strings.Join(fields, "\x00"))) + ".json"
}

func syncEventDir(root *os.Root) error {
	dir, err := root.Open(".")
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func workerSubdir(root *os.Root, name string) (*os.Root, error) {
	if err := root.Mkdir(name, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, err
	}
	return openJobRound(root, name)
}

func verifyWorkerEvent(store *os.Root, name string, event workerEvent) error {
	slot, err := openJobRound(store, name)
	if err != nil {
		return errors.New("invalid stored worker event directory")
	}
	defer slot.Close()
	before, err := slot.Lstat("receipt.json")
	if err != nil || !before.Mode().IsRegular() || before.Sys().(*syscall.Stat_t).Nlink != 1 {
		return errors.New("invalid stored worker event")
	}
	raw, err := readJobInput(slot, "receipt.json", 4096)
	if err != nil {
		return errors.New("cannot read stored worker event")
	}
	after, err := slot.Lstat("receipt.json")
	if err != nil || !os.SameFile(before, after) || after.Sys().(*syscall.Stat_t).Nlink != 1 {
		return errors.New("stored worker event changed")
	}
	var previous workerEvent
	if json.Unmarshal(raw, &previous) != nil {
		return errors.New("invalid stored worker event")
	}
	when, err := time.Parse(workerEventTime, previous.At)
	canonical, _ := json.Marshal(previous)
	if err != nil || when.Format(workerEventTime) != previous.At || !digestPattern.MatchString(previous.Policy) || !bytes.Equal(raw, canonical) {
		return errors.New("invalid stored worker event")
	}
	previous.At = event.At
	if event.Kind == "builder_publication_recovered" {
		previous.Policy = event.Policy
	}
	if previous != event {
		return errors.New("conflicting stored worker event")
	}
	return nil
}

// Root.Rename is unavailable in the supported Go 1.24 runtime. Use the
// existing Python runtime's descriptor-relative POSIX rename. A complete,
// nonempty directory cannot replace a previously published nonempty slot.
func publishEventDirectory(stage, store *os.Root, temporary, name string) error {
	source, err := stage.Open(".")
	if err != nil {
		return err
	}
	defer source.Close()
	target, err := store.Open(".")
	if err != nil {
		return err
	}
	defer target.Close()
	const rename = `
import errno, os, sys
try:
    os.rename(sys.argv[1], sys.argv[2], src_dir_fd=3, dst_dir_fd=4)
except OSError as err:
    if err.errno in (errno.ENOTEMPTY, errno.EEXIST):
        sys.exit(17)
    raise
`
	cmd := exec.Command("python3", "-I", "-S", "-c", rename, temporary, name)
	cmd.Env = workerEnv("")
	cmd.ExtraFiles = []*os.File{source, target}
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) && exit.ExitCode() == 17 {
			return os.ErrExist
		}
		return errors.New("cannot publish worker event")
	}
	return nil
}

func saveWorkerEvent(dir *os.File, event workerEvent) error {
	root, err := os.OpenRoot(dir.Name())
	if err != nil {
		return err
	}
	defer root.Close()
	held, err := dir.Stat()
	current, statErr := root.Stat(".")
	if err != nil || statErr != nil || !os.SameFile(held, current) {
		return errors.New("job directory changed before recording worker event")
	}
	store, err := workerSubdir(root, workerEventDir)
	if err != nil {
		return errors.New("cannot open worker event directory")
	}
	defer store.Close()
	if err := dir.Sync(); err != nil {
		return err
	}
	name := event.key()
	if _, err := store.Lstat(name); err == nil {
		if err := verifyWorkerEvent(store, name, event); err != nil {
			return err
		}
		return syncEventDir(store)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	stage, err := workerSubdir(store, ".staging")
	if err != nil {
		return err
	}
	defer stage.Close()
	temporary := rand.Text()
	if err := stage.Mkdir(temporary, 0700); err != nil {
		return err
	}
	defer stage.Remove(temporary)
	slot, err := openJobRound(stage, temporary)
	if err != nil {
		return err
	}
	defer slot.Close()
	event.At = time.Now().UTC().Format(workerEventTime)
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}
	file, err := slot.OpenFile("receipt.json", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	// Remove a failed or losing writer's staging file by its staging name.
	// The slot descriptor itself follows the inode across a successful rename.
	defer stage.Remove(temporary + "/receipt.json")
	_, writeErr := file.Write(data)
	if writeErr == nil {
		writeErr = file.Sync()
	}
	if err := errors.Join(writeErr, file.Close()); err != nil {
		return err
	}
	if err := syncEventDir(slot); err != nil {
		return err
	}
	if err := publishEventDirectory(stage, store, temporary, name); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		if err := verifyWorkerEvent(store, name, event); err != nil {
			return err
		}
	}
	if err := syncEventDir(store); err != nil {
		return err
	}
	return syncEventDir(stage)
}
