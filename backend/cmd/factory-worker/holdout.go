package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"path"
	"slices"
	"strings"
)

const maxHoldoutDepth = 8

func (c *config) UnmarshalJSON(raw []byte) error {
	// encoding/json normally accepts duplicate names, case aliases and null.
	// These must not be allowed to overwrite an owner's holdout policy.
	fields := codexEventObject(raw)
	if fields == nil {
		return errors.New("factory config must be an object with unique field names")
	}
	const key = "eval_holdout_paths"
	for name, value := range fields {
		if strings.EqualFold(name, key) && (name != key || bytes.Equal(bytes.TrimSpace(value), []byte("null"))) {
			return errors.New("eval_holdout_paths must use its exact name and contain an array")
		}
	}
	type configFields config
	return json.Unmarshal(raw, (*configFields)(c))
}

func canonicalHoldoutPaths(paths []string) ([]string, error) {
	if len(paths) > 32 {
		return nil, errors.New("eval_holdout_paths exceeds 32 paths")
	}
	canonical := make([]string, 0, len(paths))
	total := 0
	for _, name := range paths {
		total += len(name)
		if total > 2048 || len(name) > 256 {
			return nil, errors.New("eval_holdout_paths exceeds its configured path size limit")
		}
		name = strings.TrimSuffix(name, "/")
		if !fs.ValidPath(name) || name == "." || !safeReadPath.MatchString("/"+name) ||
			strings.Count(name, "/") >= maxHoldoutDepth {
			return nil, errors.New("eval_holdout_paths requires clean literal repository-relative paths of at most eight components")
		}
		name = strings.ToLower(name)
		if strings.Contains("/"+name+"/", "/.git/") {
			return nil, errors.New("eval_holdout_paths cannot include .git")
		}
		canonical = append(canonical, name)
	}
	slices.Sort(canonical)
	if len(slices.Compact(canonical)) != len(canonical) {
		return nil, errors.New("eval_holdout_paths contains duplicate canonical paths")
	}
	return canonical, nil
}

// The disposable repository is newly initialized, has no alternates or remote,
// and has fetched only the pinned tips. Verify that its entire object store is
// accounted for by those tips before relying on their path histories.
func (s *snapshot) checkEvalHoldouts(ctx context.Context, paths []string) error {
	if len(paths) == 0 {
		return nil
	}
	canonical, err := canonicalHoldoutPaths(paths)
	if err != nil {
		return err
	}
	tips := []string{s.head}
	if s.base != "" && s.base != s.head {
		tips = append(tips, s.base)
	}
	shallow, err := git(ctx, s.checkout, "rev-parse", "--is-shallow-repository")
	if err != nil || strings.TrimSpace(shallow) != "false" {
		return errors.New("could not verify complete Git history for configured eval holdouts")
	}
	check := append([]string{"--no-replace-objects", "fsck", "--connectivity-only", "--no-reflogs", "--no-progress", "--unreachable"}, tips...)
	if out, err := limitedReviewGit(ctx, s.checkout, 0, check...); err != nil || len(out) != 0 {
		return errors.New("could not account for all Git objects in the eval holdout check")
	}
	log := append([]string{"--no-replace-objects", "log", "--full-history", "-m", "--no-renames", "--max-count=1", "--format=%H"}, tips...)
	log = append(log, "--")
	checkPaths := func(specs []string, refusal string) error {
		out, err := limitedReviewGit(ctx, s.checkout, 128, append(log, specs...)...)
		if err != nil {
			return errors.New("could not verify configured eval holdout history")
		}
		if len(out) != 0 {
			return errors.New(refusal)
		}
		return nil
	}
	// Git compares path bytes. Refuse historical names outside printable ASCII
	// because host filesystems may normalize or case-fold them to ASCII names.
	if err := checkPaths([]string{":(top,glob)**/*[! -~]*", ":(top,glob)**/*[! -~]*/**"},
		"worker snapshot contains nonportable Git paths; cannot verify configured eval holdouts"); err != nil {
		return err
	}

	var targets []string
	var ancestors [maxHoldoutDepth - 1][]string
	for _, name := range canonical {
		targets = append(targets, ":(top,icase,literal)"+name)
		for parent := path.Dir(name); parent != "."; parent = path.Dir(parent) {
			depth := strings.Count(parent, "/")
			ancestors[depth] = append(ancestors[depth], ":(top,icase,literal)"+parent, ":(top,icase,exclude,glob)"+parent+"/**")
		}
	}
	const refusal = "worker snapshot refused: tracked eval holdout or non-directory ancestor in Git history"
	if err := checkPaths(targets, refusal); err != nil {
		return err
	}
	// Git does not follow symlinks in path-limited history. Reject files,
	// symlinks and gitlinks at an ancestor. Group paths at the same depth so
	// excluding descendants to allow normal siblings cannot hide another check.
	for _, specs := range ancestors {
		if len(specs) > 0 {
			if err := checkPaths(specs, refusal); err != nil {
				return err
			}
		}
	}
	encoded, _ := json.Marshal(canonical)
	s.holdoutPolicyDigest = contentDigest(encoded)
	return nil
}
