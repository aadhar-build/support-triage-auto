package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type snapshot struct {
	temp, checkout, job, source, head, headRef, base, baseRef, branch string
}

var gitSHA = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)

func git(ctx context.Context, dir string, args ...string) (string, error) {
	argv := append([]string{"-c", "core.hooksPath=" + os.DevNull, "-c", "commit.gpgSign=false"}, args...)
	out, _, err := runNative(ctx, "git", dir, workerEnv(""), argv, "")
	if err != nil {
		return "", fmt.Errorf("git %s in %s failed: %w", args[0], filepath.Base(dir), err)
	}
	return string(out), nil
}

func gitCommit(ctx context.Context, dir, ref string) (string, error) {
	resolved, err := git(ctx, dir, "rev-parse", "--verify", "--end-of-options", ref+"^{commit}")
	sha := strings.TrimSpace(resolved)
	if err != nil || !gitSHA.MatchString(sha) {
		return "", fmt.Errorf("cannot resolve commit %q", ref)
	}
	return sha, nil
}

func makeSnapshot(ctx context.Context, root, jobDir string, cfg config, state jobState, r role) (_ snapshot, err error) {
	s := snapshot{source: root, headRef: "HEAD"}
	if state.Branch != "factory/"+state.ID {
		return snapshot{}, errors.New("job branch does not match its ID")
	}
	if r.stage == "build" || r.stage == "review" || r.stage == "approve" {
		if state.Round < 1 || state.Worktree != filepath.Join(".worktrees", state.ID) {
			return snapshot{}, errors.New("job does not have a valid worktree and build round")
		}
		s.source, err = containedDir(root, state.Worktree)
		if err != nil {
			return snapshot{}, err
		}
		s.branch = "refs/heads/" + state.Branch
		if err := s.cleanWorktree(ctx); err != nil {
			return snapshot{}, err
		}
	}
	s.head, err = gitCommit(ctx, s.source, "HEAD")
	if err != nil {
		return snapshot{}, err
	}
	s.baseRef = "refs/remotes/origin/" + cfg.BaseBranch
	if state.StackedOnBranch != "" {
		if !strings.HasPrefix(state.StackedOnBranch, "factory/") ||
			!jobID.MatchString(strings.TrimPrefix(state.StackedOnBranch, "factory/")) {
			return snapshot{}, errors.New("invalid stacked branch")
		}
		s.baseRef = "refs/heads/" + state.StackedOnBranch
	}
	if _, err = git(ctx, root, "check-ref-format", s.baseRef); err != nil {
		return snapshot{}, errors.New("invalid base branch")
	}
	if r.stage != "intake" {
		s.base, err = gitCommit(ctx, root, s.baseRef)
		if err != nil {
			return snapshot{}, err
		}
	}
	if r.stage == "spec" {
		s.head = s.base
		s.headRef = s.baseRef
	}
	s.temp, err = os.MkdirTemp("", "factory-worker-")
	if err != nil {
		return snapshot{}, err
	}
	temp := s.temp
	defer func() {
		if err != nil {
			_ = os.RemoveAll(temp)
		}
	}()
	s.temp, err = filepath.EvalSymlinks(s.temp)
	if err != nil {
		return snapshot{}, err
	}
	s.checkout = filepath.Join(s.temp, "checkout")
	if _, err = git(ctx, s.temp, "init", "-q", "--template=", s.checkout); err != nil {
		return snapshot{}, err
	}
	fetch := []string{"fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--", root, s.head}
	if s.base != "" && s.base != s.head {
		fetch = append(fetch, s.base)
	}
	if _, err = git(ctx, s.checkout, fetch...); err != nil {
		return snapshot{}, err
	}
	if s.base != "" {
		if _, err = git(ctx, s.checkout, "update-ref", s.baseRef, s.base); err != nil {
			return snapshot{}, err
		}
	}
	if _, err = git(ctx, s.checkout, "checkout", "--quiet", "--detach", s.head); err != nil {
		return snapshot{}, err
	}
	s.job = filepath.Join(s.checkout, "factory", "jobs", state.ID)
	if err = copyJobInputs(jobDir, s.job); err != nil {
		return snapshot{}, err
	}
	return s, nil
}

func (s snapshot) cleanWorktree(ctx context.Context) error {
	branch, err := git(ctx, s.source, "symbolic-ref", "HEAD")
	if err != nil || strings.TrimSpace(branch) != s.branch {
		return errors.New("job worktree is not on its assigned branch")
	}
	status, err := git(ctx, s.source, "status", "--porcelain", "-z", "--untracked-files=all")
	if err != nil {
		return err
	}
	if status != "" {
		return errors.New("job worktree has uncommitted changes; inspect it before dispatch")
	}
	return nil
}

func (s snapshot) verifySource(ctx context.Context) error {
	if s.branch != "" {
		if err := s.cleanWorktree(ctx); err != nil {
			return err
		}
	}
	head, err := gitCommit(ctx, s.source, s.headRef)
	if err != nil || head != s.head {
		return errors.New("job candidate changed during dispatch; inspect and rerun")
	}
	if s.base != "" {
		base, err := gitCommit(ctx, s.source, s.baseRef)
		if err != nil || base != s.base {
			return errors.New("job base changed during dispatch; inspect and rerun")
		}
	}
	return nil
}

func copyJobInputs(source, target string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		dest := filepath.Join(target, rel)
		if entry.IsDir() {
			if rel != "." && !regexp.MustCompile(`^round-[0-9]+$`).MatchString(rel) {
				return filepath.SkipDir
			}
			return os.MkdirAll(dest, 0700)
		}
		if filepath.Ext(rel) != ".md" {
			return nil
		}
		data, err := readRegular(path, 4*1024*1024)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, data, 0600)
	})
}

func readRegular(file string, limit int64) ([]byte, error) {
	stat, err := os.Lstat(file)
	if err != nil {
		return nil, err
	}
	if !stat.Mode().IsRegular() || stat.Size() > limit {
		return nil, fmt.Errorf("expected a regular file of at most %d bytes at %s", limit, filepath.Base(file))
	}
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%s exceeded %d bytes", filepath.Base(file), limit)
	}
	return data, err
}

func (s snapshot) prompt(state jobState, cfg config, r role, brief string) string {
	pr := "none"
	if state.PR > 0 {
		pr = fmt.Sprint(state.PR)
	}
	base := strings.TrimPrefix(strings.TrimPrefix(s.baseRef, "refs/remotes/"), "refs/heads/")
	transport := fmt.Sprintf("Return the complete Markdown body of %s as your final response, without a surrounding code fence. The launcher writes that one artifact. Do not write it yourself. Your working directory and any job files are disposable copies. Never perform remote operations.", r.artifact)
	if r.stage == "build" {
		transport += " Make in-scope code changes in Worktree and run checks. Do not run git add or commit. The launcher checks candidate scope and commits eligible changes locally."
	}
	return fmt.Sprintf("Job: %s\nJob folder: %s\nWorktree: %s\nMain checkout: %s\nBranch: %s   Base: %s   Round: %d   PR: %s\nDiff base: %s\n\n%s\n\n%s\n",
		state.ID, s.job, s.checkout, s.checkout, state.Branch, cfg.BaseBranch, state.Round, pr, base, brief, transport)
}

func (s snapshot) prepareCandidate(ctx context.Context, root, jobDir string) (string, error) {
	if head, err := gitCommit(ctx, s.checkout, "HEAD"); err != nil || head != s.head {
		return "", errors.New("builder changed the snapshot Git history")
	}
	if _, err := git(ctx, s.checkout, "add", "-A"); err != nil {
		return "", err
	}
	stage, err := git(ctx, s.checkout, "diff", "--cached", "--raw", "--no-renames")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(strings.TrimSpace(stage), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 1 && (fields[1] == "120000" || fields[1] == "160000") {
			return "", errors.New("worker cannot publish a new symlink or submodule")
		}
	}
	if stage == "" {
		return "", errors.New("builder produced no candidate changes")
	}
	if _, err := git(ctx, s.checkout, "-c", "user.name=Software Factory", "-c", "user.email=factory@localhost",
		"commit", "--quiet", "-m", "Implement "+filepath.Base(jobDir)); err != nil {
		return "", err
	}
	newHead, err := gitCommit(ctx, s.checkout, "HEAD")
	if err != nil {
		return "", err
	}
	if err := s.checkPaths(ctx, root, jobDir, newHead); err != nil {
		return "", err
	}
	if _, err := git(ctx, s.source, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--", s.checkout, newHead); err != nil {
		return "", err
	}
	if err := s.verifySource(ctx); err != nil {
		return "", err
	}
	return newHead, nil
}

func (s snapshot) checkPaths(ctx context.Context, root, jobDir, candidate string) error {
	paths, err := git(ctx, s.checkout, "diff", "--name-only", "--no-renames", "-z", s.base+"..."+candidate)
	if err != nil {
		return err
	}
	// Reuse the same rules as gate; worker dispatch never substitutes for gate.
	const check = `
import sys
from pathlib import Path
root, job = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "factory" / "lib"))
import factory as F
cfg = F.load_config(root)
spec = (job / "spec.md").read_text()
changed = [p for p in sys.stdin.read().split("\0") if p]
allowed = F.allowed_files(spec)
if (not changed or not allowed or F.open_product_questions(spec)
        or F.protected_violations(changed, cfg["protected_paths"])
        or F.scope_violations(changed, allowed + cfg.get("always_in_scope", []))):
    raise SystemExit(1)
`
	if _, _, err := runNative(ctx, "python3", root, workerEnv(""), []string{"-B", "-c", check, root, jobDir}, paths); err != nil {
		return errors.New("candidate refused by the existing protected paths, spec scope or product-question rules")
	}
	return nil
}
