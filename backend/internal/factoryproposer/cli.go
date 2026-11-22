package factoryproposer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const maxStreamSize = 256 << 10
const maxCodexStartupWarnings = 8

type cli struct {
	name, path, model, home string
	unavailable             bool
}

func (c cli) run(ctx context.Context, prompt string) (string, Attempt) {
	attempt := Attempt{Provider: c.name, Model: c.model, Phase: PhaseSetup}
	stop := func(disposition Disposition, failure Failure) (string, Attempt) {
		attempt.Disposition, attempt.Failure = disposition, failure
		return "", attempt
	}
	if c.unavailable {
		return stop(Unavailable, "")
	}
	if !c.safeProfile() {
		return stop(UnsafeProfile, "")
	}
	bin, err := filepath.EvalSymlinks(c.path)
	if err != nil {
		return stop(Unavailable, "")
	}
	info, err := os.Stat(bin)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0111 == 0 {
		return stop(Unavailable, "")
	}
	// Keep the CLI outside worktrees even if the launcher's TMPDIR points into one.
	dir, err := os.MkdirTemp("/tmp", "factory-proposer-")
	if err != nil {
		return stop(Failed, FailureRunner)
	}
	defer os.RemoveAll(dir)

	env := c.environment(dir)
	authArgs := []string{"--restricted", "--safe-mode", "--setting-sources", "", "auth", "status", "--json"}
	if c.name == "codex" {
		authArgs = []string{"-c", `model_provider="openai"`, "-c", `forced_login_method="chatgpt"`, "login", "status"}
	}
	attempt.Phase = PhaseLogin
	login := runCommand(ctx, bin, dir, env, authArgs, "")
	if login.disposition != Accepted {
		if login.disposition == Failed && (login.failure == FailureAuth || login.failure == FailureNativeExit) {
			return stop(AuthFailed, login.failure)
		}
		return stop(login.disposition, login.failure)
	}
	if !requiredLoginVerified(c.name, login.stdout, login.stderr) {
		return stop(AuthFailed, FailureAuth)
	}
	if !c.safeProfile() {
		return stop(UnsafeProfile, "")
	}

	args := claudeArgs(c.model)
	if c.name == "codex" {
		args = codexArgs(c.model)
	}
	attempt.Phase = PhaseModel
	native := runCommand(ctx, bin, dir, env, args, prompt)
	if native.disposition != Accepted {
		return stop(native.disposition, native.failure)
	}
	attempt.Phase = PhaseDecision
	var result string
	if c.name == "claude" {
		result = claudeDecision(native.stdout)
	} else {
		result = codexDecision(native.stdout)
	}
	if result == "" {
		return stop(Invalid, "")
	}
	attempt.Disposition = Accepted
	return result, attempt
}

func (c cli) environment(dir string) []string {
	env := []string{
		"HOME=" + c.home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR=" + dir, "GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_GLOBAL=" + os.DevNull, "GIT_TERMINAL_PROMPT=0",
	}
	if c.name == "claude" {
		env = append(env, "CLAUDE_CONFIG_DIR="+c.configDir())
	} else {
		env = append(env, "CODEX_HOME="+c.configDir())
	}
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if key == "LANG" || strings.HasPrefix(key, "LC_") {
			env = append(env, entry)
		}
	}
	return env
}

// Claude reports its subscription category. The Codex command establishes
// ChatGPT authentication, and does not report the plan tier.
func requiredLoginVerified(provider string, stdout, stderr []byte) bool {
	if provider == "claude" {
		auth := jsonObject(stdout)
		return string(auth["loggedIn"]) == "true" && jsonText(auth["authMethod"]) == "claude.ai" &&
			jsonText(auth["apiProvider"]) == "firstParty" &&
			slices.Contains([]string{"pro", "max", "ultra", "team", "enterprise"}, jsonText(auth["subscriptionType"]))
	}
	found := false
	for _, line := range strings.Split(string(stdout)+"\n"+string(stderr), "\n") {
		switch line = strings.TrimSpace(line); {
		case line == "Logged in using ChatGPT":
			found = true
		case line == "", strings.HasPrefix(line, "WARNING: proceeding, even though we could not create PATH aliases:"):
		default:
			return false
		}
	}
	return found
}

func claudeArgs(model string) []string {
	return []string{
		"--restricted", "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
		"--mcp-config", `{"mcpServers":{}}`,
		"--settings", `{"disableAllHooks":true,"disableClaudeAiConnectors":true}`,
		"--tools", "", "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
		"--permission-mode", "dontAsk", "--permission-prompts", "none", "--output-format", "json",
		"--model", model, "--print",
	}
}

func codexArgs(model string) []string {
	return []string{
		"-a", "never", "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
		"--ephemeral", "--skip-git-repo-check", "--json", "--color", "never", "--model", model,
		"-c", `model_provider="openai"`, "-c", `forced_login_method="chatgpt"`,
		"-c", `web_search="disabled"`, "-c", "mcp_servers={}",
		"-c", "project_root_markers=[]", "-c", "project_doc_max_bytes=0",
		"-c", "features.skip_host_skill_discovery=true", "-c", "features.memories=false",
		"-c", `default_permissions="factory_proposer"`,
		"-c", `permissions.factory_proposer.filesystem={":minimal"="read"}`,
		"-c", "permissions.factory_proposer.network.enabled=false",
		"-c", "allow_login_shell=false", "-c", `shell_environment_policy.inherit="none"`,
		"-c", "features.shell_tool=false", "-c", "features.unified_exec=false", "-c", "features.shell_snapshot=false",
		"-c", "features.hooks=false", "-c", "features.plugins=false", "-c", "features.apps=false",
		"-c", "features.browser_use=false", "-c", "features.in_app_browser=false", "-c", "features.computer_use=false",
		"-c", "features.image_generation=false", "-c", "features.view_image=false", "-c", "features.workspace_dependencies=false",
		"-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false", "-c", "features.goals=false", "-",
	}
}

// Keep the process-group leader alive through normal completion so cleanup
// cannot signal a group ID reused after the native executable exits. The
// supervisor takes positional arguments, and closes its status pipe to the CLI.
const nativeSupervisor = `
"$@" 3>&-
provider_status=$?
printf '%d\n' "$provider_status" >&3
kill -KILL 0
exit 126
`

type commandResult struct {
	stdout, stderr []byte
	disposition    Disposition
	failure        Failure
}

func runCommand(ctx context.Context, bin, dir string, env, args []string, input string) commandResult {
	cmdCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	statusRead, statusWrite, err := os.Pipe()
	if err != nil {
		return commandResult{disposition: Failed, failure: FailureRunner}
	}
	defer statusRead.Close()
	defer statusWrite.Close()

	command := append([]string{"-c", nativeSupervisor, "factory-proposer", bin}, args...)
	cmd := exec.CommandContext(cmdCtx, "/bin/sh", command...)
	cmd.Dir, cmd.Env, cmd.Stdin = dir, env, strings.NewReader(input)
	cmd.ExtraFiles = []*os.File{statusWrite}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); !errors.Is(err, syscall.ESRCH) {
			return err
		}
		return os.ErrProcessDone
	}
	cmd.WaitDelay = 250 * time.Millisecond
	stdout, stderr := limitedBuffer{cancel: cancel}, limitedBuffer{cancel: cancel}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Start(); err != nil {
		return commandFailure(ctx)
	}
	statusWrite.Close()
	waitErr := cmd.Wait()
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return commandResult{disposition: TimedOut}
	case ctx.Err() != nil:
		return commandResult{disposition: Canceled}
	case stdout.exceeded || stderr.exceeded:
		return commandResult{disposition: OutputTooLarge}
	}
	status, err := io.ReadAll(io.LimitReader(statusRead, 5))
	var exit *exec.ExitError
	exitCode, exitErr := strconv.ParseUint(strings.TrimSuffix(string(status), "\n"), 10, 8)
	if err != nil || exitErr != nil || !bytes.HasSuffix(status, []byte{'\n'}) || !errors.As(waitErr, &exit) ||
		exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
		return commandResult{disposition: Failed, failure: FailureRunner}
	}
	// A POSIX shell uses these codes when it cannot execute the command.
	if exitCode == 126 || exitCode == 127 {
		return commandResult{disposition: Failed, failure: FailureRunner}
	}
	if exitCode != 0 {
		return commandResult{disposition: Failed, failure: nativeFailure(stdout.data.Bytes(), stderr.data.Bytes())}
	}
	return commandResult{stdout: stdout.data.Bytes(), stderr: stderr.data.Bytes(), disposition: Accepted}
}

func commandFailure(ctx context.Context) commandResult {
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return commandResult{disposition: TimedOut}
	case ctx.Err() != nil:
		return commandResult{disposition: Canceled}
	default:
		return commandResult{disposition: Failed, failure: FailureRunner}
	}
}

// Diagnostics are coarse hints from bounded native failure streams. Only
// recognized categories leave the process. Their contents never authorize an action.
func nativeFailure(stdout, stderr []byte) Failure {
	if failure := classifyDiagnostic(string(stderr)); failure != FailureNativeExit {
		return failure
	}
	for _, line := range bytes.Split(stdout, []byte{'\n'}) {
		event := jsonObject(line)
		var message string
		switch jsonText(event["type"]) {
		case "error":
			message = jsonText(event["message"])
		case "turn.failed":
			message = jsonText(jsonObject(event["error"])["message"])
		case "result":
			if string(event["is_error"]) == "true" {
				message = jsonText(event["result"])
			}
		}
		if failure := classifyDiagnostic(message); failure != FailureNativeExit {
			return failure
		}
	}
	return FailureNativeExit
}

func classifyDiagnostic(message string) Failure {
	message = strings.ToLower(message)
	switch {
	case containsDiagnostic(message, "unexpected argument", "unknown option", "unknown argument", "unrecognized option"):
		return FailureCLIArgs
	case containsDiagnostic(message, "error loading config", "error parsing config", "failed to load config", "invalid configuration"):
		return FailureCLIConfig
	case containsDiagnostic(message, "not logged in", "401 unauthorized", "authentication failed", "oauth token has expired"):
		return FailureAuth
	case containsDiagnostic(message, "connection refused", "connection reset", "failed to connect", "could not resolve host",
		"dns error", "tls handshake", "network is unreachable", "error sending request for url"):
		return FailureNetwork
	case containsDiagnostic(message, "model_not_found", "unsupported model", "model is not supported", "model is not available"):
		return FailureModel
	default:
		return FailureNativeExit
	}
}

func containsDiagnostic(message string, indicators ...string) bool {
	return slices.ContainsFunc(indicators, func(indicator string) bool {
		return strings.Contains(message, indicator)
	})
}

type limitedBuffer struct {
	data     bytes.Buffer
	cancel   context.CancelFunc
	exceeded bool
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if len(p) > maxStreamSize-b.data.Len() {
		b.exceeded = true
		b.cancel()
		return 0, errors.New("scheduling CLI output exceeded limit")
	}
	return b.data.Write(p)
}

func claudeDecision(stdout []byte) string {
	event := jsonObject(stdout)
	if jsonText(event["type"]) != "result" || jsonText(event["subtype"]) != "success" ||
		string(event["is_error"]) != "false" || string(event["num_turns"]) != "1" {
		return ""
	}
	var denials []json.RawMessage
	if json.Unmarshal(event["permission_denials"], &denials) != nil || denials == nil || len(denials) != 0 {
		return ""
	}
	return jsonText(event["result"])
}

func codexDecision(stdout []byte) string {
	lines := bytes.Split(stdout, []byte{'\n'})
	if len(lines) < 5 || len(lines[len(lines)-1]) != 0 {
		return ""
	}
	thread, end := jsonObject(lines[0]), jsonObject(lines[len(lines)-2])
	if len(thread) != 2 || jsonText(thread["type"]) != "thread.started" || jsonText(thread["thread_id"]) == "" ||
		len(end) != 2 || jsonText(end["type"]) != "turn.completed" ||
		jsonObject(end["usage"]) == nil {
		return ""
	}
	seen := make(map[string]bool)
	started := false
	startupWarnings := 0
	var result string
	for _, line := range lines[1 : len(lines)-2] {
		event := jsonObject(line)
		eventType := jsonText(event["type"])
		if !started && eventType == "turn.started" && len(event) == 1 {
			started = true
			continue
		}
		item := jsonObject(event["item"])
		id, itemType := jsonText(item["id"]), jsonText(item["type"])
		if len(event) != 2 || eventType != "item.completed" || len(item) != 3 || id == "" || seen[id] || result != "" {
			return ""
		}
		seen[id] = true
		if !started {
			// Codex emits nonfatal startup warnings as completed error items.
			// Their text is discarded and can never supply a scheduling decision.
			message := jsonText(item["message"])
			if itemType != "error" || message == "" || strings.HasPrefix(message, "model rerouted:") ||
				startupWarnings >= maxCodexStartupWarnings {
				return ""
			}
			startupWarnings++
			continue
		}
		switch itemType {
		case "agent_message":
			result = jsonText(item["text"])
			if result == "" {
				return ""
			}
		case "reasoning":
			if !bytes.HasPrefix(item["text"], []byte{'"'}) {
				return ""
			}
		default:
			return ""
		}
	}
	return result
}

// jsonObject rejects duplicate or case-ambiguous fields at each interpreted object boundary.
func jsonObject(raw []byte) map[string]json.RawMessage {
	if !utf8.Valid(raw) {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if token, err := dec.Token(); err != nil || token != json.Delim('{') {
		return nil
	}
	fields, seen := make(map[string]json.RawMessage), make(map[string]bool)
	for dec.More() {
		token, err := dec.Token()
		key, ok := token.(string)
		if err != nil || !ok {
			return nil
		}
		folded := strings.ToLower(key)
		if seen[folded] {
			return nil
		}
		seen[folded] = true
		var value json.RawMessage
		if dec.Decode(&value) != nil {
			return nil
		}
		fields[key] = value
	}
	if token, err := dec.Token(); err != nil || token != json.Delim('}') {
		return nil
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil
	}
	return fields
}

func jsonText(raw json.RawMessage) string {
	var value string
	if json.Unmarshal(raw, &value) != nil {
		return ""
	}
	return value
}
