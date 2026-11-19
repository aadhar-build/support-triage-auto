package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const reviewEvidenceVersion = 2

func validateStageEvidence(ctx context.Context, jobDir string, state jobState, head, base string) error {
	if state.Stage != "review" && state.Stage != "approve" {
		return nil
	}
	round := fmt.Sprintf("round-%d", state.Round)
	checksPath := round + "/checks.md"
	checks, err := readLiveEvidence(jobDir, checksPath, maxJobInputBytes)
	lines := strings.SplitN(string(checks), "\n", 6)
	recovery := fmt.Sprintf("Run job re-review %s, then gate %s", state.ID, state.ID)
	if err != nil || lines[0] != "GATE: PASS" {
		return fmt.Errorf("current round does not have a passing gate. %s", recovery)
	}
	if len(lines) < 5 || lines[4] != "" {
		return fmt.Errorf("passing gate lacks a valid candidate, base, and spec digest header block. %s", recovery)
	}
	if !gitSHA.MatchString(head) || lines[1] != "CANDIDATE: "+head {
		return fmt.Errorf("passing gate is for a different candidate. %s", recovery)
	}
	if !gitSHA.MatchString(base) || lines[2] != "BASE: "+base {
		return fmt.Errorf("passing gate is for a different base. %s", recovery)
	}
	spec, err := readLiveEvidence(jobDir, "spec.md", maxJobInputBytes)
	if err != nil {
		return fmt.Errorf("cannot read spec.md to verify the passing gate. %s", recovery)
	}
	if lines[3] != "SPEC-DIGEST: "+contentDigest(spec) {
		return fmt.Errorf("passing gate is for different spec.md bytes. %s", recovery)
	}
	if state.Stage != "approve" {
		return nil
	}
	if len(state.Reviewers) == 0 {
		return errors.New("no reviewers are routed for this job")
	}
	for name := range state.Reviewers {
		role, ok := roles["reviewer-"+name]
		if !ok {
			return fmt.Errorf("unknown routed reviewer %q", name)
		}
		review, err := readLiveEvidence(jobDir, round+"/"+role.artifact, maxArtifact)
		verdict, _, terminated := strings.Cut(string(review), "\n")
		if err != nil || !terminated || strings.TrimSuffix(verdict, "\r") != "VERDICT: PASS" {
			return fmt.Errorf("current round does not have a passing %s", role.name)
		}
		audit := round + "/worker-" + role.name + ".json"
		if _, err := os.Lstat(filepath.Join(jobDir, audit+".pending")); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("%s replacement is pending", role.name)
		}
		raw, err := readLiveEvidence(jobDir, audit, maxArtifact)
		var record evidenceRecord
		if err != nil || json.Unmarshal(raw, &record) != nil || record.EvidenceVersion != reviewEvidenceVersion ||
			record.Role != role.name || record.CandidateSHA != head ||
			record.BaseSHA != base || !gitSHA.MatchString(record.BaseSHA) ||
			record.Inputs["spec.md"] == "" || record.Inputs[checksPath] == "" ||
			record.ArtifactSHA256 != contentDigest(review) {
			return fmt.Errorf("%s audit does not match the current candidate", role.name)
		}
		if err := verifyEvidenceInputs(ctx, jobDir, state, role, record.Inputs); err != nil {
			return fmt.Errorf("%s evidence is stale: %w", role.name, err)
		}
	}
	return nil
}

// jobIdentityDigest excludes stage and history, which change during transitions.
func jobIdentityDigest(state jobState) string {
	values := []string{state.ID, state.Branch, state.Worktree, strconv.Itoa(state.Round),
		state.StackedOnBranch, strconv.Itoa(state.PR), strconv.Itoa(state.StackedOnPR),
		state.Risk, strconv.Itoa(len(state.Surfaces))}
	values = append(values, state.Surfaces...)
	names := make([]string, 0, len(state.Reviewers))
	for name := range state.Reviewers {
		names = append(names, name)
	}
	sort.Strings(names)
	values = append(values, strconv.Itoa(len(names)))
	for _, name := range names {
		values = append(values, name, state.Reviewers[name])
	}
	hash := sha256.New()
	for _, value := range values {
		fmt.Fprintf(hash, "%d:%s", len([]byte(value)), value)
	}
	return hex.EncodeToString(hash.Sum(nil))
}

func contentDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

type evidenceRecord struct {
	EvidenceVersion int               `json:"evidence_version"`
	Role            string            `json:"role"`
	CandidateSHA    string            `json:"candidate_sha"`
	BaseSHA         string            `json:"base_sha"`
	Inputs          map[string]string `json:"inputs"`
	ArtifactSHA256  string            `json:"artifact_sha256"`
}

func evidenceInputs(ctx context.Context, dir string, state jobState, r role) (map[string]string, error) {
	inputs := map[string]string{"job.json": jobIdentityDigest(state)}
	err := scanJobInputs(ctx, dir, state, r, func(name string, data []byte) error {
		inputs[name] = contentDigest(data)
		return nil
	})
	return inputs, err
}

func verifyEvidenceInputs(ctx context.Context, dir string, state jobState, r role, inputs map[string]string) error {
	current, err := evidenceInputs(ctx, dir, state, r)
	if err != nil {
		return err
	}
	if !maps.Equal(current, inputs) {
		return errors.New("job evidence inputs changed, are incomplete, or belong to an earlier format")
	}
	return nil
}

// Open from a pinned directory and refuse non-regular inputs before reading.
func readLiveEvidence(dir, name string, limit int64) ([]byte, error) {
	if !fs.ValidPath(name) || name == "." {
		return nil, errors.New("invalid job evidence path")
	}
	before, err := os.Lstat(dir)
	if err != nil || !before.IsDir() {
		return nil, errors.New("invalid job evidence directory")
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(before, opened) {
		return nil, errors.New("job evidence directory changed")
	}
	folder, file := filepath.Split(name)
	if folder != "" {
		folder = strings.TrimSuffix(folder, "/")
		if strings.Contains(folder, "/") {
			return nil, errors.New("invalid job evidence path depth")
		}
		child, err := openJobRound(root, folder)
		if err != nil {
			return nil, err
		}
		defer child.Close()
		root = child
	}
	return readJobInput(root, file, limit)
}
