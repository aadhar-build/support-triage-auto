package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const replacementSuffix = ".replacing"

// The pending record is materialized before the branch moves. If either
// artifact cannot be written, recovery reuses the same commit and bytes.
type publication struct {
	State, Artifact, Audit []byte
	Input, Candidate       string
	Base, BaseRef          string
	SpecSHA256             string
}

func savePublication(dir *os.File, path string, p publication) error {
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	if err := writeAtomicFile(path, data, true); err != nil {
		return err
	}
	if err := dir.Sync(); err != nil {
		return fmt.Errorf("sync pending builder publication; inspect it before retrying: %w", err)
	}
	return nil
}

func finishPublication(ctx context.Context, s snapshot, p publication, dir *os.File, pending, artifactPath, auditPath string) (err error) {
	removed := false
	defer func() {
		if err != nil && !removed {
			err = fmt.Errorf("candidate publication is pending; run worker --recover %s: %w", filepath.Base(dir.Name()), err)
		}
	}()
	// The job mutex was released after the journal was synced. The journal
	// excludes other operations until the fast-forward and both outputs finish.
	if err := s.cleanWorktree(ctx); err != nil {
		return err
	}
	head, err := gitCommit(ctx, s.source, "HEAD")
	if err != nil {
		return err
	}
	if head != p.Candidate {
		if head != p.Input {
			return errors.New("worktree is at neither the recorded input nor candidate")
		}
		parent, err := git(ctx, s.source, "rev-list", "--parents", "-n", "1", p.Candidate)
		if err != nil || strings.TrimSpace(parent) != p.Candidate+" "+p.Input {
			return errors.New("recorded candidate is not a direct child of the recorded input")
		}
		if _, err := git(ctx, s.source, "merge", "--ff-only", "--quiet", p.Candidate); err != nil {
			return err
		}
	}
	if err := writeAtomicFile(auditPath, p.Audit, true); err != nil {
		return err
	}
	if err := writeAtomicFile(artifactPath, p.Artifact, true); err != nil {
		return err
	}
	if err := dir.Sync(); err != nil {
		return err
	}
	// Recovery may reach this step after a non-replacement attempt or
	// after a crash between removing the intent and removing the journal.
	if err := os.Remove(auditPath + replacementSuffix); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("finish builder evidence replacement: %w", err)
	}
	if err := os.Remove(pending); err != nil {
		return err
	}
	removed = true
	if err := dir.Sync(); err != nil {
		return fmt.Errorf("published builder evidence but could not sync journal removal: %w", err)
	}
	return nil
}

func readBuilderSpec(jobDir, expectedDigest string) ([]byte, error) {
	spec, err := readLiveEvidence(jobDir, "spec.md", maxJobInputBytes)
	if err != nil || !reviewText(spec) {
		return nil, errors.New("cannot read valid spec.md for builder publication")
	}
	if expectedDigest != "" && contentDigest(spec) != expectedDigest {
		return nil, errors.New("spec.md changed since builder dispatch; inspect the job and retry")
	}
	return spec, nil
}

func readPendingPublication(jobDir, pending string, state jobState, currentState []byte) (publication, error) {
	const maxPublicationBytes = 2*maxJobInputBytes + 4*maxArtifact
	raw, err := readLiveEvidence(jobDir, filepath.Base(pending), maxPublicationBytes)
	if err != nil {
		return publication{}, fmt.Errorf("cannot read pending builder publication: %w", err)
	}
	var p publication
	if err := json.Unmarshal(raw, &p); err != nil {
		return publication{}, fmt.Errorf("cannot decode pending builder publication: %w", err)
	}
	if !bytes.Equal(p.State, currentState) || !gitSHA.MatchString(p.Input) || !gitSHA.MatchString(p.Candidate) ||
		!gitSHA.MatchString(p.Base) || state.Round < 1 || state.Branch != "factory/"+state.ID || state.Worktree != filepath.Join(".worktrees", state.ID) {
		return publication{}, errors.New("pending publication no longer matches the job")
	}
	if p.SpecSHA256 == "" {
		return publication{}, errors.New("legacy builder publication has no spec digest; inspect it before recovery")
	}
	if _, err := readBuilderSpec(jobDir, p.SpecSHA256); err != nil {
		return publication{}, err
	}
	if err := validateArtifact(roles["builder"], p.Artifact); err != nil {
		return publication{}, err
	}
	return p, nil
}

func recoverPublication(ctx context.Context, root string, cfg config, state jobState, p publication, dir *os.File, pending, artifactPath, auditPath string, stdout io.Writer) error {
	worktree, err := containedDir(root, state.Worktree)
	if err != nil {
		return err
	}
	ref := "refs/remotes/origin/" + cfg.BaseBranch
	if state.StackedOnBranch != "" {
		ref = "refs/heads/" + state.StackedOnBranch
	}
	base, err := gitCommit(ctx, root, ref)
	if err != nil || ref != p.BaseRef || base != p.Base {
		return errors.New("base changed since the candidate was prepared; inspect pending publication")
	}
	s := snapshot{source: worktree, checkout: worktree, base: base, branch: "refs/heads/" + state.Branch}
	spec, err := readBuilderSpec(filepath.Dir(pending), p.SpecSHA256)
	if err != nil {
		return err
	}
	if err := s.checkPaths(ctx, root, p.Candidate, spec); err != nil {
		return err
	}
	if err := finishPublication(ctx, s, p, dir, pending, artifactPath, auditPath); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "%s builder | recovered %s | build.md\n", state.ID, p.Candidate)
	return nil
}
