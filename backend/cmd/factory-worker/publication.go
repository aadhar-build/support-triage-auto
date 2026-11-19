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

// The pending record is materialized before the branch moves. If either
// artifact cannot be written, recovery reuses the same commit and bytes.
type publication struct {
	State, Artifact, Audit []byte
	Input, Candidate       string
	Base, BaseRef          string
}

func savePublication(path string, p publication) error {
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return atomicWrite(path, data)
}

func finishPublication(ctx context.Context, s snapshot, p publication, pending, artifactPath, auditPath string) error {
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
	if err := atomicWrite(auditPath, p.Audit); err != nil {
		return err
	}
	if err := atomicWrite(artifactPath, p.Artifact); err != nil {
		return err
	}
	return os.Remove(pending)
}

func recoverPublication(ctx context.Context, root string, state jobState, currentState []byte, pending, artifactPath, auditPath string, stdout io.Writer) error {
	var p publication
	if err := readJSON(pending, &p); err != nil {
		return fmt.Errorf("cannot read pending builder publication: %w", err)
	}
	if !bytes.Equal(p.State, currentState) || !gitSHA.MatchString(p.Input) || !gitSHA.MatchString(p.Candidate) ||
		!gitSHA.MatchString(p.Base) || state.Branch != "factory/"+state.ID || state.Worktree != filepath.Join(".worktrees", state.ID) {
		return errors.New("pending publication no longer matches the job")
	}
	if err := validateArtifact(roles["builder"], p.Artifact); err != nil {
		return err
	}
	worktree, err := containedDir(root, state.Worktree)
	if err != nil {
		return err
	}
	var cfg config
	if err := readJSON(filepath.Join(root, "factory", "config.json"), &cfg); err != nil {
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
	if err := s.checkPaths(ctx, root, filepath.Dir(pending), p.Candidate); err != nil {
		return err
	}
	if err := finishPublication(ctx, s, p, pending, artifactPath, auditPath); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "%s builder | recovered %s | build.md\n", state.ID, p.Candidate)
	return nil
}
