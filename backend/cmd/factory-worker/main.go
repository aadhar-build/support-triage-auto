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
	EvalHoldoutPaths []string          `json:"eval_holdout_paths"`
	BaseBranch       string            `json:"base_branch"`
	Models           map[string]string `json:"models"`
	Workers          struct {
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
	StackedOnPR     int               `json:"stacked_on_pr"`
	Risk            string            `json:"risk"`
	Surfaces        []string          `json:"surfaces"`
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
	jobDirectory, err := os.Open(jobDir)
	if err != nil {
		return err
	}
	defer jobDirectory.Close()
	var cfg config
	if err := readJSON(filepath.Join(root, "factory", "config.json"), &cfg); err != nil {
		return fmt.Errorf("read factory configuration: %w", err)
	}
	var state jobState
	stateBefore, err := readLiveEvidence(jobDir, "job.json", maxJobInputBytes)
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
	pending := auditPath + ".pending"
	reviewEvidenceStage := r.stage == "review" || r.stage == "approve"
	replacement := auditPath + replacementSuffix
	if reviewEvidenceStage {
		replacement = pending
	}
	replacing := len(args) == 3
	durableReplacement := replacing && !reviewEvidenceStage
	var recoveryPending string
	if recoverBuild {
		recoveryPending = pending
	}
	var unlockRole func()
	defer func() {
		if unlockRole != nil {
			unlockRole()
		}
	}()
	var recovery publication
	if err := withJobAdmission(ctx, jobDirectory, stateBefore, recoveryPending, func() error {
		if err := verifyArtifactDir(jobDir, artifactDir); err != nil {
			return err
		}
		var err error
		unlockRole, err = lockRole(auditPath)
		if err != nil {
			return err
		}
		if recoverBuild {
			recovery, err = readPendingPublication(jobDir, pending, state, stateBefore)
			return err
		}
		if !reviewEvidenceStage {
			if _, err := os.Lstat(pending); !errors.Is(err, os.ErrNotExist) {
				return fmt.Errorf("unexpected pending publication %s; inspect it before retrying", filepath.Base(pending))
			}
		}
		if info, err := os.Lstat(replacement); !errors.Is(err, os.ErrNotExist) {
			if err != nil || !info.Mode().IsRegular() || !replacing {
				if reviewEvidenceStage {
					return errors.New("review evidence replacement is pending. Retry with --replace or start a fresh review round")
				}
				return fmt.Errorf("worker evidence replacement is pending (%s); inspect it and retry with --replace", filepath.Base(replacement))
			}
		}
		if !replacing {
			for _, path := range []string{artifactPath, auditPath} {
				if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
					return fmt.Errorf("%s already exists or cannot be inspected; inspect it before using --replace", filepath.Base(path))
				}
			}
		}
		if replacing {
			intent := []byte("worker evidence replacement in progress\n")
			if reviewEvidenceStage {
				intent = []byte("review evidence replacement in progress\n")
			}
			if err := writeAtomicFile(replacement, intent, durableReplacement); err != nil {
				return fmt.Errorf("record worker evidence replacement: %w", err)
			}
			if durableReplacement {
				if err := jobDirectory.Sync(); err != nil {
					return fmt.Errorf("sync worker evidence replacement intent: %w", err)
				}
			}
		}
		return nil
	}); err != nil {
		return err
	}
	if recoverBuild {
		return recoverPublication(ctx, root, cfg, state, recovery, jobDirectory, pending, artifactPath, auditPath, stdout)
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
	if err := validateStageEvidence(ctx, jobDir, state, s.head, s.base); err != nil {
		return err
	}
	var inputs map[string]string
	if reviewEvidenceStage {
		if err := validateStageEvidence(ctx, s.job, state, s.head, s.base); err != nil {
			return err
		}
		inputs, err = evidenceInputs(ctx, s.job, state, r)
		if err != nil {
			return err
		}
		if err := verifyEvidenceInputs(ctx, jobDir, state, r, inputs); err != nil {
			return err
		}
		if err := s.writeReviewInputs(ctx); err != nil {
			return fmt.Errorf("prepare pinned review inputs: %w", err)
		}
		if err := s.prepareReviewReads(inputs); err != nil {
			return fmt.Errorf("prepare bounded review reads: %w", err)
		}
	}
	var spec []byte
	var specDigest string
	if r.stage == "build" {
		spec, err = readBuilderSpec(s.job, "")
		if err != nil {
			return err
		}
		specDigest = contentDigest(spec)
	}
	prompt := s.prompt(state, cfg, r, brief)
	content, err := execute(ctx, identity.binary, chosen, s, prompt, r)
	if err != nil {
		return err
	}
	if err := validateArtifact(r, content); err != nil {
		return err
	}
	if err := s.verifySource(ctx); err != nil {
		return err
	}
	admitPublication := func(publish func() error) error {
		return withJobAdmission(ctx, jobDirectory, stateBefore, "", func() error {
			if err := verifyArtifactDir(jobDir, artifactDir); err != nil {
				return err
			}
			if err := validateStageEvidence(ctx, jobDir, state, s.head, s.base); err != nil {
				return err
			}
			if inputs != nil {
				if err := verifyEvidenceInputs(ctx, jobDir, state, r, inputs); err != nil {
					return err
				}
			}
			if specDigest != "" {
				if _, err := readBuilderSpec(jobDir, specDigest); err != nil {
					return err
				}
			}
			return publish()
		})
	}
	candidate := s.head
	if r.stage == "build" {
		candidate, err = s.prepareCandidate(ctx, root, jobDir, spec)
		if errors.Is(err, errNoCandidate) {
			failurePath := auditPath + ".failure"
			hint := noCandidateDiagnostic(content)
			writeErr := admitPublication(func() error {
				if writeNoChangeFailure(failurePath, chosen.Harness, s.head, content) != nil {
					return fmt.Errorf("%w (%s). %s. The worker could not write %s", err, chosen.Harness, hint, filepath.Base(failurePath))
				}
				return nil
			})
			if writeErr != nil {
				return writeErr
			}
			return fmt.Errorf("%w (%s). %s. See %s", err, chosen.Harness, hint, filepath.Base(failurePath))
		}
		if err != nil {
			return err
		}
	}
	content = append(bytes.TrimRight(content, "\r\n"), '\n')
	record := struct {
		selection
		HoldoutDigest   string            `json:"eval_holdout_policy_sha256,omitempty"`
		HoldoutsChecked int               `json:"checked_eval_holdout_path_count"`
		CLIVersion      string            `json:"cli_version"`
		AuthMode        string            `json:"auth_mode"`
		Role            string            `json:"role"`
		InputSHA        string            `json:"input_sha"`
		CandidateSHA    string            `json:"candidate_sha"`
		BaseSHA         string            `json:"base_sha,omitempty"`
		At              string            `json:"at"`
		EvidenceVersion int               `json:"evidence_version,omitempty"`
		Inputs          map[string]string `json:"inputs,omitempty"`
		ArtifactSHA256  string            `json:"artifact_sha256,omitempty"`
	}{chosen, s.holdoutPolicyDigest, len(cfg.EvalHoldoutPaths), identity.CLIVersion, identity.AuthMode, r.name, s.head, candidate, s.base, time.Now().UTC().Format(time.RFC3339), 0, inputs, ""}
	if inputs != nil {
		record.EvidenceVersion = reviewEvidenceVersion
		record.ArtifactSHA256 = contentDigest(content)
	}
	encoded, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')
	if r.stage == "build" {
		transaction := publication{State: stateBefore, Input: s.head, Candidate: candidate,
			Base: s.base, BaseRef: s.baseRef, SpecSHA256: specDigest, Artifact: content, Audit: encoded}
		if err := admitPublication(func() error {
			return savePublication(jobDirectory, pending, transaction)
		}); err != nil {
			return err
		}
		if err := finishPublication(ctx, s, transaction, jobDirectory, pending, artifactPath, auditPath); err != nil {
			return err
		}
	} else {
		if err := admitPublication(func() error {
			if err := writeAtomicFile(auditPath, encoded, durableReplacement); err != nil {
				return fmt.Errorf("write worker audit (candidate %s): %w", candidate, err)
			}
			if err := writeAtomicFile(artifactPath, content, durableReplacement); err != nil {
				return fmt.Errorf("write worker artifact (candidate %s): %w", candidate, err)
			}
			if durableReplacement {
				if err := jobDirectory.Sync(); err != nil {
					return fmt.Errorf("sync replacement evidence: %w", err)
				}
			}
			if replacing {
				if err := os.Remove(replacement); err != nil {
					return fmt.Errorf("finish worker evidence replacement: %w", err)
				}
			}
			if durableReplacement {
				if err := jobDirectory.Sync(); err != nil {
					return fmt.Errorf("sync worker evidence replacement intent removal: %w", err)
				}
			}
			return nil
		}); err != nil {
			return err
		}
	}
	fmt.Fprintf(stdout, "%s %s | %s %s | %s | %s\n", state.ID, r.name, chosen.Harness, chosen.Model, candidate, r.artifact)
	return nil
}

// The sidecar records the most recent no-change attempt for this round.
// A later success publishes its own audit and leaves this attempt's record in place.
func writeNoChangeFailure(path, harness, head string, report []byte) error {
	// Persist only fixed values, the verified input SHA, and the report length.
	record := struct {
		Reason         string `json:"reason"`
		Harness        string `json:"harness"`
		InputSHA       string `json:"input_sha"`
		ReportBytes    int    `json:"report_bytes"`
		ReportedReason string `json:"reported_reason,omitempty"`
		At             string `json:"at"`
	}{"no_candidate_changes", harness, head, len(report), reportedNoChangeReason(report), time.Now().UTC().Format(time.RFC3339)}
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return atomicWrite(path, append(data, '\n'))
}

const noChangeReasonPrefix = "NO-CHANGE REASON: "

func reportedNoChangeReason(report []byte) string {
	var reason string
	for line := range strings.SplitSeq(string(report), "\n") {
		code, ok := strings.CutPrefix(strings.TrimSuffix(line, "\r"), noChangeReasonPrefix)
		if !ok {
			continue
		}
		if reason != "" {
			return ""
		}
		switch code {
		case "already-satisfied", "write-denied", "blocked", "unknown":
			reason = code
		default:
			return ""
		}
	}
	return reason
}

// Model reports can repeat issue text, credentials and command output.
// Emit only fixed categories, even when the report matches a known blocker.
func noCandidateDiagnostic(report []byte) string {
	summary := strings.ToLower(string(report))
	switch {
	case strings.Contains(summary, "permission denied"), strings.Contains(summary, "operation not permitted"),
		strings.Contains(summary, "read-only file system"), strings.Contains(summary, "sandbox denied"):
		return "The builder reported a permission or sandbox blocker. Inspect the worker's write and tool permissions"
	case strings.Contains(summary, "command not found"), strings.Contains(summary, "module not found"):
		return "The builder reported a missing command or module. Check the worker's local dependencies"
	case strings.Contains(summary, ": not met"):
		return "The builder reported unmet acceptance criteria. Review the spec and any previous round feedback"
	default:
		return "Check whether the spec requires a new edit and whether the worker can write the disposable checkout"
	}
}

func verifyArtifactDir(jobDir, artifactDir string) error {
	if artifactDir == jobDir {
		return nil
	}
	_, err := containedDir(jobDir, filepath.Base(artifactDir))
	return err
}

func lockRole(auditPath string) (func(), error) {
	lock := auditPath + ".lock"
	if err := os.Mkdir(lock, 0700); err != nil {
		return nil, fmt.Errorf("cannot acquire worker lock %s; inspect for an active or interrupted attempt: %w", filepath.Base(lock), err)
	}
	return func() { _ = os.Remove(lock) }, nil
}

func atomicWrite(path string, content []byte) error {
	return writeAtomicFile(path, content, false)
}

func writeAtomicFile(path string, content []byte, syncFile bool) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".worker-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	_, writeErr := f.Write(content)
	if syncFile && writeErr == nil {
		writeErr = f.Sync()
	}
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
