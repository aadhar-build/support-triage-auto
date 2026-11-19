package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type selection struct {
	Harness string `json:"harness"`
	Model   string `json:"model"`
	Effort  string `json:"effort,omitempty"`
}

type config struct {
	BaseBranch string            `json:"base_branch"`
	Models     map[string]string `json:"models"`
	Workers    struct {
		Roles   map[string]selection `json:"roles"`
		Timeout int                  `json:"timeout_s"`
	} `json:"workers"`
}

type jobState struct {
	ID              string            `json:"id"`
	Stage           string            `json:"stage"`
	Branch          string            `json:"branch"`
	Worktree        string            `json:"worktree"`
	Round           int               `json:"round"`
	PR              int               `json:"pr"`
	Reviewers       map[string]string `json:"reviewers"`
	StackedOnBranch string            `json:"stacked_on_branch"`
}

type role struct {
	name, stage, artifact, legacyModel string
}

var roles = map[string]role{
	"intake":            {"intake", "intake", "intake.md", "intake"},
	"spec-writer":       {"spec-writer", "spec", "spec.md", "spec-writer"},
	"builder":           {"builder", "build", "build.md", "builder"},
	"reviewer-code":     {"reviewer-code", "review", "review-code.md", "code"},
	"reviewer-eval":     {"reviewer-eval", "review", "review-eval.md", "eval"},
	"reviewer-security": {"reviewer-security", "review", "review-security.md", "security"},
	"approver":          {"approver", "approve", "decision.md", "approver"},
}

var jobID = regexp.MustCompile(`^[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*$`)
var modelID = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:@+-]*$`)

func main() {
	root := flag.String("root", "", "main checkout")
	flag.Parse()
	if *root == "" {
		fmt.Fprintln(os.Stderr, "REFUSED: run through factory/bin/worker")
		os.Exit(2)
	}
	if err := run(*root, flag.Args(), os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "REFUSED: %s\n", err)
		os.Exit(1)
	}
}

func readJSON[T any](path string, value *T) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, value)
}

func (r role) resolve(cfg config, state jobState) (selection, error) {
	legacy := cfg.Models[r.legacyModel]
	if r.stage == "review" {
		legacy = state.Reviewers[r.legacyModel]
		if legacy == "" {
			return selection{}, fmt.Errorf("%s is not routed for this job", r.name)
		}
	}
	chosen, explicit := cfg.Workers.Roles[r.name]
	if !explicit {
		chosen = selection{Harness: "claude", Model: legacy}
	}
	if chosen.Harness != "claude" && chosen.Harness != "codex" {
		return selection{}, fmt.Errorf("unknown harness %q for %s", chosen.Harness, r.name)
	}
	if !modelID.MatchString(chosen.Model) {
		return selection{}, fmt.Errorf("an explicit valid model is required for %s", r.name)
	}
	if chosen.Effort != "" {
		levels := " low medium high xhigh "
		if chosen.Harness == "claude" {
			levels += "max "
		} else {
			levels += "none minimal "
		}
		if !modelID.MatchString(chosen.Effort) || !strings.Contains(levels, " "+chosen.Effort+" ") {
			return selection{}, fmt.Errorf("invalid %s effort %q", chosen.Harness, chosen.Effort)
		}
	}
	return chosen, nil
}

func run(root string, args []string, stdout io.Writer) error {
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	if len(args) == 2 && args[0] == "--preflight" {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		result, err := preflight(ctx, args[1], root)
		if err != nil {
			return err
		}
		fmt.Fprintf(stdout, "%s | %s\n", result.CLIVersion, result.AuthMode)
		return nil
	}
	recoverBuild := len(args) == 2 && args[0] == "--recover"
	if recoverBuild {
		args = []string{args[1], "builder"}
	}
	if (len(args) != 2 && len(args) != 3) || !jobID.MatchString(args[0]) ||
		(len(args) == 3 && args[2] != "--replace") {
		return errors.New("usage: worker <job-id> <role> [--replace] | worker --preflight <claude|codex> | worker --recover <job-id>")
	}
	r, ok := roles[args[1]]
	if !ok {
		return fmt.Errorf("unknown role %q", args[1])
	}
	jobDir, err := containedDir(root, filepath.Join("factory", "jobs", args[0]))
	if err != nil {
		return err
	}
	var cfg config
	if err := readJSON(filepath.Join(root, "factory", "config.json"), &cfg); err != nil {
		return fmt.Errorf("read factory configuration: %w", err)
	}
	var state jobState
	statePath := filepath.Join(jobDir, "job.json")
	stateBefore, err := os.ReadFile(statePath)
	if err != nil {
		return err
	}
	for name := range cfg.Workers.Roles {
		if _, ok := roles[name]; !ok {
			return fmt.Errorf("unknown configured worker role %q", name)
		}
	}
	if err := json.Unmarshal(stateBefore, &state); err != nil {
		return err
	}
	if state.ID != args[0] || state.Stage != r.stage {
		return fmt.Errorf("job %s has stage %q, expected %q for %s", args[0], state.Stage, r.stage, r.name)
	}
	chosen, err := r.resolve(cfg, state)
	if err != nil {
		return err
	}
	if cfg.Workers.Timeout < 0 || cfg.Workers.Timeout > 7200 {
		return errors.New("workers.timeout_s must be between 0 and 7200")
	}
	timeout := time.Duration(cfg.Workers.Timeout) * time.Second
	if timeout == 0 {
		timeout = 30 * time.Minute
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	artifactDir := jobDir
	if r.stage == "review" {
		if state.Round < 1 {
			return errors.New("review has no current round")
		}
		artifactDir, err = containedDir(root, filepath.Join("factory", "jobs", state.ID, fmt.Sprintf("round-%d", state.Round)))
		if err != nil {
			return err
		}
	}
	auditName := "worker-" + r.name + ".json"
	if r.stage == "build" || r.stage == "approve" {
		auditName = fmt.Sprintf("worker-round-%d-%s.json", state.Round, r.name)
	}
	artifactPath, auditPath := filepath.Join(artifactDir, r.artifact), filepath.Join(artifactDir, auditName)
	unlock, err := lockRole(auditPath)
	if err != nil {
		return err
	}
	defer unlock()
	pending := auditPath + ".pending"
	if recoverBuild {
		return recoverPublication(ctx, root, state, stateBefore, pending, artifactPath, auditPath, stdout)
	}
	if _, err := os.Lstat(pending); !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("an earlier builder publication is pending; run worker --recover %s", state.ID)
	}
	if len(args) != 3 {
		for _, p := range []string{artifactPath, auditPath} {
			if _, err := os.Lstat(p); !errors.Is(err, os.ErrNotExist) {
				return fmt.Errorf("%s already exists or cannot be inspected; inspect it before using --replace", filepath.Base(p))
			}
		}
	}
	brief, err := rolePrompt(root, r.name)
	if err != nil {
		return err
	}
	preCtx, preCancel := context.WithTimeout(ctx, 30*time.Second)
	identity, err := preflight(preCtx, chosen.Harness, root)
	preCancel()
	if err != nil {
		return err
	}
	s, err := makeSnapshot(ctx, root, jobDir, cfg, state, r)
	if err != nil {
		return err
	}
	defer os.RemoveAll(s.temp)
	if err := validateStageEvidence(jobDir, state, s.head); err != nil {
		return err
	}
	prompt := s.prompt(state, cfg, r, brief)
	content, err := execute(ctx, identity.binary, chosen, s, prompt, r)
	if err != nil {
		return err
	}
	if err := validateArtifact(r, content); err != nil {
		return err
	}
	currentState, err := os.ReadFile(statePath)
	if err != nil || !bytes.Equal(stateBefore, currentState) {
		return errors.New("job state changed during dispatch; inspect and rerun")
	}
	if err := s.verifySource(ctx); err != nil {
		return err
	}
	if err := validateStageEvidence(jobDir, state, s.head); err != nil {
		return err
	}
	candidate := s.head
	if r.stage == "build" {
		candidate, err = s.prepareCandidate(ctx, root, jobDir)
		if err != nil {
			return err
		}
	}
	record := struct {
		selection
		CLIVersion   string `json:"cli_version"`
		AuthMode     string `json:"auth_mode"`
		Role         string `json:"role"`
		InputSHA     string `json:"input_sha"`
		CandidateSHA string `json:"candidate_sha"`
		BaseSHA      string `json:"base_sha,omitempty"`
		At           string `json:"at"`
	}{chosen, identity.CLIVersion, identity.AuthMode, r.name, s.head, candidate, s.base, time.Now().UTC().Format(time.RFC3339)}
	encoded, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')
	content = append(bytes.TrimRight(content, "\r\n"), '\n')
	if r.stage == "build" {
		transaction := publication{State: stateBefore, Input: s.head, Candidate: candidate,
			Base: s.base, BaseRef: s.baseRef, Artifact: content, Audit: encoded}
		if err := savePublication(pending, transaction); err != nil {
			return err
		}
		if err := finishPublication(ctx, s, transaction, pending, artifactPath, auditPath); err != nil {
			return fmt.Errorf("candidate publication is pending; run worker --recover %s: %w", state.ID, err)
		}
	} else {
		if err := atomicWrite(auditPath, encoded); err != nil {
			return fmt.Errorf("write worker audit (candidate %s): %w", candidate, err)
		}
		if err := atomicWrite(artifactPath, content); err != nil {
			return fmt.Errorf("write worker artifact (candidate %s): %w", candidate, err)
		}
	}
	fmt.Fprintf(stdout, "%s %s | %s %s | %s | %s\n", state.ID, r.name, chosen.Harness, chosen.Model, candidate, r.artifact)
	return nil
}

func lockRole(auditPath string) (func(), error) {
	lock := auditPath + ".lock"
	if err := os.Mkdir(lock, 0700); err != nil {
		return nil, fmt.Errorf("cannot acquire worker lock %s; inspect for an active or interrupted attempt: %w", filepath.Base(lock), err)
	}
	return func() { _ = os.Remove(lock) }, nil
}

func atomicWrite(path string, content []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".worker-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	_, writeErr := f.Write(content)
	closeErr := f.Close()
	if err := errors.Join(writeErr, closeErr); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func containedDir(root, rel string) (string, error) {
	if !filepath.IsLocal(rel) {
		return "", fmt.Errorf("invalid repository directory %q", rel)
	}
	path := filepath.Join(root, rel)
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", err
	}
	fi, err := os.Stat(resolved)
	if err != nil || !fi.IsDir() || resolved != path {
		return "", fmt.Errorf("expected a real directory at %s", rel)
	}
	return resolved, nil
}

func rolePrompt(root, roleName string) (string, error) {
	raw, err := os.ReadFile(filepath.Join(root, ".claude", "agents", roleName+".md"))
	if err != nil {
		return "", err
	}
	_, front, valid := strings.Cut(string(raw), "---\n")
	_, prompt, validEnd := strings.Cut(front, "\n---\n")
	if !valid || !validEnd || strings.TrimSpace(prompt) == "" {
		return "", fmt.Errorf("invalid role definition for %s", roleName)
	}
	return strings.TrimSpace(prompt), nil
}

const maxArtifact = 128 * 1024

func validateArtifact(r role, raw []byte) error {
	if len(raw) == 0 || len(raw) > maxArtifact || bytes.IndexByte(raw, 0) >= 0 {
		return fmt.Errorf("%s returned a missing, invalid or oversized artifact", r.name)
	}
	s := strings.ReplaceAll(string(raw), "\r\n", "\n")
	line, _, _ := strings.Cut(s, "\n")
	valid := false
	switch r.stage {
	case "intake":
		labels := regexp.MustCompile(`^DECISION: (ACCEPT|NEEDS-INFO|REJECT)\nRISK: (low|medium|high)\nSURFACES: (?:none|(?:security|eval|ui)(?:, (?:security|eval|ui))*)\nRESCOPED: (yes|no)\n`)
		valid = labels.MatchString(s) && hasSection(s, "Summary")
	case "spec":
		valid = hasSection(s, "Product questions") && hasSection(s, "Files") && hasSection(s, "Acceptance criteria")
	case "build":
		valid = hasSection(s, "Summary") && hasSection(s, "Acceptance criteria") && hasSection(s, "Commands run")
	case "review":
		valid = (line == "VERDICT: PASS" || line == "VERDICT: CHANGES") && hasSection(s, "Summary") &&
			(line != "VERDICT: CHANGES" || hasSection(s, "Problems"))
	case "approve":
		valid = (line == "APPROVE" || line == "ESCALATE") && hasSection(s, "Summary") &&
			(line != "ESCALATE" || hasSection(s, "For the human"))
	}
	if !valid {
		return fmt.Errorf("%s returned an artifact that does not match its role format", r.name)
	}
	return nil
}

func hasSection(text, heading string) bool {
	return strings.Contains("\n"+text+"\n", "\n## "+heading+"\n")
}

func runNative(ctx context.Context, binary, cwd string, env, args []string, stdin string) ([]byte, []byte, error) {
	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Dir, cmd.Env = cwd, env
	cmd.Stdin = strings.NewReader(stdin)
	cmd.WaitDelay = 5 * time.Second
	var out, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return out.Bytes(), stderr.Bytes(), err
}
