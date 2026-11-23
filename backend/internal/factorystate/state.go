// Package factorystate observes a controller-owned factory checkout for shadow decisions.
// It uses the local factory's cooperative admission locks and reads the local origin
// tracking ref. It cannot authenticate owner authorization, a remote base, or
// capacity leases. An observation ID identifies one read and cannot fence a write.
package factorystate

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
)

var (
	// ErrBusy means an admission lock or a publication marker blocked the observation.
	ErrBusy = errors.New("factory admission is busy or interrupted")

	// ErrStaleCheckout means the installed checkout and local tracking base disagree.
	ErrStaleCheckout = errors.New("factory checkout HEAD differs from the local tracking base")

	jobID      = regexp.MustCompile(`^[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*$`)
	stagedJob  = regexp.MustCompile(`^\.create-[a-z0-9_]{8}$`)
	baseBranch = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_./-]{0,254}$`)
	commitID   = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)
)

const (
	maxJobs          = 256
	maxJobEntries    = 1024
	maxConfigBytes   = 256 * 1024
	maxJobStateBytes = 4 * 1024 * 1024
)

// Observation contains the observed stage inventory and bounded ready-job diagnostics.
// Snapshot.ActiveBuilds counts build, gate, review, recovery, approve and merging
// jobs according to the supervised lane workflow. It is a stage count, not a
// verified lease count. Snapshot.BaseRevision is the local origin tracking ref.
type Observation struct {
	Snapshot  factorycontroller.Snapshot
	ReadyJobs []ReadyJobObservation
}

// Load returns the controller view of one read-only observation. Its revision
// belongs to this read only. There is no write side or factorycontroller.Store.
func Load(ctx context.Context, checkout string) (factorycontroller.Snapshot, error) {
	observed, err := Observe(ctx, checkout)
	return observed.Snapshot, err
}

// Observe reads the existing factory records from the supplied main checkout.
// It never fetches, starts workers or writes to the checkout. Callers must choose a
// trusted checkout and process environment, including the Git executable on PATH.
// A busy, incomplete or changing inventory is
// refused. Owner and verified spec fields remain zero because the native records
// cannot establish them. Legacy records without a native revision use a local
// digest to identify the bytes observed and have no pickup prerequisites.
func Observe(ctx context.Context, checkout string) (Observation, error) {
	if err := ctx.Err(); err != nil {
		return Observation{}, err
	}
	if checkout == "" {
		return Observation{}, errors.New("main factory checkout is required")
	}
	absolute, err := filepath.Abs(checkout)
	if err != nil {
		return Observation{}, errors.New("cannot resolve the factory checkout")
	}
	root, err := openCheckout(absolute)
	if err != nil {
		return Observation{}, err
	}
	defer root.Close()

	factory, err := openDirectory(root, "factory", false)
	if err != nil {
		return Observation{}, err
	}
	defer factory.close()
	git, err := openDirectory(root, ".git", false)
	if err != nil {
		return Observation{}, err
	}
	defer git.close()
	jobs, err := openDirectory(factory.root, "jobs", true)
	if err != nil {
		return Observation{}, err
	}
	defer jobs.close()

	rawConfig, err := readRegular(factory.root, "config.json", maxConfigBytes)
	if err != nil {
		return Observation{}, errors.New("missing or invalid factory configuration")
	}
	ref, capacity, err := readConfig(rawConfig)
	if err != nil {
		return Observation{}, err
	}
	base, err := readBase(ctx, absolute, ref)
	if err != nil {
		return Observation{}, err
	}
	inventory, err := listJobs(jobs.root)
	if err != nil {
		return Observation{}, err
	}
	locked := make([]*directory, 0, len(inventory))
	defer func() {
		for _, job := range locked {
			job.close()
		}
	}()
	for _, name := range inventory {
		if err := ctx.Err(); err != nil {
			return Observation{}, err
		}
		job, err := openDirectory(jobs.root, name, true)
		if err != nil {
			return Observation{}, err
		}
		locked = append(locked, job)
	}

	observed := Observation{
		Snapshot:  factorycontroller.Snapshot{BaseRevision: base, BuildCapacity: capacity, Jobs: make([]factorycontroller.Job, 0, len(locked))},
		ReadyJobs: []ReadyJobObservation{},
	}
	for _, job := range locked {
		if err := ctx.Err(); err != nil {
			return Observation{}, err
		}
		state, takingSlot, detail, err := readJob(job, base)
		if err != nil {
			return Observation{}, err
		}
		observed.Snapshot.Jobs = append(observed.Snapshot.Jobs, state)
		if takingSlot {
			observed.Snapshot.ActiveBuilds++
		}
		if state.Stage == factorycontroller.StageReady {
			observed.ReadyJobs = append(observed.ReadyJobs, detail)
		}
	}

	if err := recheckInventory(jobs.root, inventory, locked); err != nil {
		return Observation{}, err
	}
	currentConfig, err := readRegular(factory.root, "config.json", maxConfigBytes)
	if err != nil || !bytes.Equal(currentConfig, rawConfig) {
		return Observation{}, errors.New("factory configuration changed during observation")
	}
	currentBase, err := readBase(ctx, absolute, ref)
	if err != nil {
		return Observation{}, err
	}
	if currentBase != base {
		return Observation{}, errors.New("local factory base changed during observation")
	}
	currentRoot, err := os.Lstat(absolute)
	openedRoot, openedErr := root.Stat(".")
	if err != nil || openedErr != nil || !os.SameFile(currentRoot, openedRoot) ||
		!factory.matches() || !git.matches() || !jobs.matches() {
		return Observation{}, errors.New("factory checkout changed during observation")
	}
	if err := ctx.Err(); err != nil {
		return Observation{}, err
	}
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return Observation{}, errors.New("cannot identify the factory observation")
	}
	observed.Snapshot.Revision = "local-observation-" + hex.EncodeToString(id[:])
	return observed, nil
}

func readConfig(raw []byte) (string, int, error) {
	config, err := decodeObject(raw)
	if err != nil {
		return "", 0, errors.New("malformed factory configuration")
	}
	var branch string
	if !config.field("base_branch", &branch) || !baseBranch.MatchString(branch) {
		return "", 0, errors.New("invalid configured factory base branch")
	}
	lanes, err := decodeObject(config["lanes"])
	var capacity *int
	if err != nil || !lanes.field("build", &capacity) || capacity == nil || *capacity < 0 {
		return "", 0, errors.New("missing or invalid configured build capacity")
	}
	return "refs/remotes/origin/" + branch, *capacity, nil
}

func readJob(dir *directory, base string) (factorycontroller.Job, bool, ReadyJobObservation, error) {
	var job factorycontroller.Job
	var detail ReadyJobObservation
	raw, err := readRegular(dir.root, "job.json", maxJobStateBytes)
	if err != nil {
		return job, false, detail, fmt.Errorf("missing or invalid job state for %s", dir.name)
	}
	record, err := decodeObject(raw)
	var round *int
	if err != nil || !record.field("id", &job.ID) || job.ID != dir.name ||
		!record.field("stage", &job.Stage) || !record.field("round", &round) || round == nil || *round < 0 {
		return factorycontroller.Job{}, false, detail, fmt.Errorf("malformed job state for %s", dir.name)
	}
	job.Round = *round
	_, hasNativeRevision := record["revision"]
	if hasNativeRevision {
		var revision uint64
		if !record.field("revision", &revision) || revision == 0 {
			return factorycontroller.Job{}, false, detail, fmt.Errorf("malformed job state for %s", dir.name)
		}
		job.Revision = strconv.FormatUint(revision, 10)
	} else {
		job.Revision = fmt.Sprintf("legacy-observation-sha256-%x", sha256.Sum256(raw))
	}
	occupiesSlot, err := buildOccupancy(job)
	if err != nil {
		return factorycontroller.Job{}, false, detail, err
	}
	entries, err := directoryEntries(dir.root, maxJobEntries)
	if err != nil {
		return factorycontroller.Job{}, false, detail, fmt.Errorf("cannot inspect admission markers for %s", dir.name)
	}
	for _, entry := range entries {
		name := entry.Name()
		if name == ".factory-operation.json" || name == ".gate-publication.pending" ||
			(strings.HasPrefix(name, "worker-round-") && strings.HasSuffix(name, "-builder.json.pending")) {
			return factorycontroller.Job{}, false, detail, fmt.Errorf("%w for job %s", ErrBusy, dir.name)
		}
	}
	if job.Stage == factorycontroller.StageReady {
		detail = inspectReady(dir, entries, record, base)
		job.NextRoundAvailable = hasNativeRevision && nextRoundAvailable(entries, job.Round)
	}
	return job, occupiesSlot, detail, nil
}

func buildOccupancy(job factorycontroller.Job) (bool, error) {
	if job.Stage.OccupiesBuildLane() {
		if job.Round == 0 {
			return false, fmt.Errorf("build lane job %s has no current round", job.ID)
		}
		return true, nil
	}
	switch job.Stage {
	case "intake", "needs-info", "spec", "needs-input", factorycontroller.StageReady, "needs-human", "merged", "rejected":
		return false, nil
	default:
		return false, fmt.Errorf("unknown factory stage for job %s", job.ID)
	}
}

func listJobs(root *os.Root) ([]string, error) {
	entries, err := directoryEntries(root, maxJobs)
	if err != nil {
		return nil, errors.New("cannot read complete factory job inventory")
	}
	names := make([]string, 0, len(entries))
	issues := make(map[string]struct{}, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if !entry.IsDir() || len(name) > 255 {
			return nil, errors.New("factory job inventory has an invalid entry")
		}
		if stagedJob.MatchString(name) {
			// create publishes a complete directory under the inventory lock.
			continue
		}
		if !jobID.MatchString(name) {
			return nil, errors.New("factory job inventory has an unrecognized entry")
		}
		prefix, _, _ := strings.Cut(name, "-")
		issue := strings.TrimLeft(prefix, "0")
		if _, duplicate := issues[issue]; duplicate || issue == "" {
			return nil, errors.New("factory job inventory has an invalid or duplicate issue number")
		}
		issues[issue] = struct{}{}
		names = append(names, name)
	}
	slices.Sort(names)
	return names, nil
}

func hasEntry(entries []fs.DirEntry, name string) bool {
	for _, entry := range entries {
		if entry.Name() == name {
			return true
		}
	}
	return false
}
