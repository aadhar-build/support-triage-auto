package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func holdoutConfig(t *testing.T, path string) config {
	t.Helper()
	data, err := json.Marshal(map[string]any{"base_branch": "main", "eval_holdout_paths": []string{path}})
	if err != nil {
		t.Fatal(err)
	}
	var cfg config
	if err := json.Unmarshal(data, &cfg); err != nil {
		t.Fatal(err)
	}
	return cfg
}

func TestHistoricalHoldoutRefusesAllWorkerSnapshots(t *testing.T) {
	root, job, _ := buildFixture(t)
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(job, "build.md"), validBuild)
	if err := os.Mkdir(filepath.Join(job, "round-1"), 0700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(job, "round-1", "checks.md"), "GATE: PASS\n")
	dir := filepath.Join(root, "hidden", "cases")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(dir, "sample.txt"), "synthetic holdout\n")
	gitTest(t, root, "add", "hidden")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic fixture")
	gitTest(t, root, "mv", "hidden/cases/sample.txt", "renamed-sample.txt")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "move synthetic fixture")
	gitTest(t, root, "update-ref", "refs/remotes/origin/main", "HEAD")
	// Confirm the fixture has the historical object without reading its contents.
	gitTest(t, root, "cat-file", "-e", "HEAD^:hidden/cases/sample.txt")
	state := jobState{ID: "001-fixture", Branch: "factory/001-fixture", Worktree: ".worktrees/001-fixture", Round: 1}
	for name, r := range roles {
		t.Run(name, func(t *testing.T) {
			s, err := makeSnapshot(context.Background(), root, job, holdoutConfig(t, "hidden/cases/"), state, r)
			if err == nil {
				defer os.RemoveAll(s.temp)
				t.Fatal("received snapshot of repository containing historical holdout")
			}
			if s.checkout != "" || !strings.Contains(err.Error(), "tracked eval holdout") {
				t.Fatalf("expected a holdout refusal before handing off a checkout, got %v", err)
			}
		})
	}
}

func TestConfiguredHoldoutHistory(t *testing.T) {
	for _, tc := range []struct {
		name, path, configPath, reason string
		link                           bool
	}{
		{"nonportable filename", "private/ḱ/input.txt", "private/K", "portable", false},
		{"tracked file", "private/eval.txt", "private/eval.txt", "tracked eval holdout", false},
		{"case variant", "Private/Cases/input.txt", "private/cases", "tracked eval holdout", false},
		{"symlink path", "private/cases", "private/cases", "tracked eval holdout", true},
		{"symlink ancestor", "private/link", "private/link/cases", "tracked eval holdout", true},
		{"unrelated sibling", "private/training/sample.txt", "private/cases", "", false},
		{"similar prefix", "private/cases-public/input.txt", "private/cases", "", false},
		{"ambiguous config", "private/input.txt", "../private/cases", "eval_holdout_paths", false},
		{"glob config", "private/input.txt", "private/*", "eval_holdout_paths", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root, job := fixture(t, "")
			leaf := filepath.Join(root, tc.path)
			if err := os.MkdirAll(filepath.Dir(leaf), 0700); err != nil {
				t.Fatal(err)
			}
			if tc.link {
				if err := os.Symlink("public", leaf); err != nil {
					t.Fatal(err)
				}
			} else {
				write(t, leaf, "synthetic\n")
			}
			gitTest(t, root, "add", "--", tc.path)
			gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic fixture")
			state := jobState{ID: "001-fixture", Branch: "factory/001-fixture"}
			s, err := makeSnapshot(context.Background(), root, job, holdoutConfig(t, tc.configPath), state, roles["intake"])
			if err == nil {
				defer os.RemoveAll(s.temp)
			}
			if tc.reason != "" {
				if err == nil || !strings.Contains(err.Error(), tc.reason) || s.checkout != "" {
					t.Fatalf("expected refusal %q, got %v", tc.reason, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := gitTest(t, s.checkout, "show", "HEAD:"+tc.path); got != "synthetic" {
				t.Fatalf("public input absent from Git snapshot")
			}
		})
	}
}

func TestConfiguredHoldoutRejectsUnaccountedObjects(t *testing.T) {
	s := reviewSnapshotFixture(t, "public fixture\n")
	file := filepath.Join(s.checkout, "untracked.txt")
	write(t, file, "unreachable synthetic blob\n")
	gitTest(t, s.checkout, "hash-object", "-w", "--", file)
	if err := s.checkEvalHoldouts(context.Background(), []string{"private/eval"}); err == nil || !strings.Contains(err.Error(), "account for all Git objects") {
		t.Fatalf("expected unknown-object refusal, got %v", err)
	}
}

func TestConfiguredHoldoutChecksMergedSideHistory(t *testing.T) {
	root, job := fixture(t, "")
	gitTest(t, root, "checkout", "-qb", "side")
	write(t, filepath.Join(root, "fixture.eval"), "temporary synthetic eval\n")
	gitTest(t, root, "add", "fixture.eval")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "add fixture")
	gitTest(t, root, "mv", "fixture.eval", "training.txt")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "rename fixture")
	gitTest(t, root, "checkout", "-q", "main")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "merge", "--no-ff", "--no-edit", "side")
	state := jobState{ID: "001-fixture", Branch: "factory/001-fixture"}
	for _, name := range []string{"missing.eval", "fixture.eval"} {
		s, err := makeSnapshot(context.Background(), root, job, holdoutConfig(t, name), state, roles["intake"])
		if err == nil {
			defer os.RemoveAll(s.temp)
		}
		if name == "missing.eval" && err != nil {
			t.Fatalf("clean history: %v", err)
		}
		if name == "fixture.eval" && (err == nil || !strings.Contains(err.Error(), "tracked eval holdout")) {
			t.Fatalf("merged holdout: %v", err)
		}
	}
}

func TestHoldoutConfigCannotSilentlyDisablePolicy(t *testing.T) {
	for _, doc := range []string{
		`{"eval_holdout_paths":null}`,
		`{"eval_holdout_paths":["hidden"],"eval_holdout_paths":[]}`,
		`{"eval_holdout_paths":["hidden"],"EVAL_HOLDOUT_PATHS":[]}`,
		`{"Eval_Holdout_Paths":[]}`,
	} {
		var c config
		if err := json.Unmarshal([]byte(doc), &c); err == nil {
			t.Fatalf("accepted ambiguous config %s", doc)
		}
	}
}

func TestHoldoutBoundedInputs(t *testing.T) {
	s := reviewSnapshotFixture(t, "public\n")
	var many []string
	for i := 0; i < 33; i++ {
		many = append(many, fmt.Sprintf("path%d", i))
	}
	var large []string
	for i := 0; i < 12; i++ {
		large = append(large, fmt.Sprintf("%03d%s", i, strings.Repeat("a", 200)))
	}
	for _, paths := range [][]string{many, large, {"a/b/c/d/e/f/g/h/i"}, {strings.Repeat("a", 257)}, {"Cases", "cases/"}} {
		if err := s.checkEvalHoldouts(context.Background(), paths); err == nil {
			t.Fatal("accepted excessive or duplicate holdout config")
		}
	}
}

func TestHoldoutRejectsUnicodeAliasInGitTree(t *testing.T) {
	root, job := fixture(t, "")
	blob := gitTest(t, root, "rev-parse", "HEAD:.claude/agents/intake.md")
	// Put the Unicode spelling directly into the synthetic tree so that the
	// filesystem running this test cannot normalize the fixture during setup.
	gitTest(t, root, "update-index", "--add", "--cacheinfo", "100644", blob, "private/\u212A/sample.txt")
	gitTest(t, root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "nonportable fixture name")
	s, err := makeSnapshot(context.Background(), root, job, holdoutConfig(t, "private/k"), jobState{ID: "001-fixture", Branch: "factory/001-fixture"}, roles["intake"])
	if err == nil {
		defer os.RemoveAll(s.temp)
	}
	if err == nil || !strings.Contains(err.Error(), "nonportable") {
		t.Fatalf("expected Unicode alias refusal, got %v", err)
	}
}

func TestHoldoutPolicyDigestBindsCanonicalPaths(t *testing.T) {
	s := reviewSnapshotFixture(t, "public\n")
	for _, tc := range []struct {
		paths         []string
		canonicalJSON string
	}{
		{[]string{"Private/B/", "private/a"}, `["private/a","private/b"]`},
		{[]string{"private/A", "PRIVATE/b"}, `["private/a","private/b"]`},
		{[]string{"private/a", "private/c"}, `["private/a","private/c"]`},
	} {
		if err := s.checkEvalHoldouts(context.Background(), tc.paths); err != nil {
			t.Fatal(err)
		}
		if s.holdoutPolicyDigest != contentDigest([]byte(tc.canonicalJSON)) {
			t.Fatal("wrong policy digest")
		}
	}
}
