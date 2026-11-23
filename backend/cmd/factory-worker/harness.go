package main

import (
	"bytes"
	"context"
	"debug/buildinfo"
	"encoding/json"
	"errors"
	"fmt"
	"go/version"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
)

type identity struct {
	binary     string
	CLIVersion string
	AuthMode   string
}

// Launch the installed native CLI with the user's own local login. Inherit
// neither API keys nor routing/proxy variables, even from the other provider.
func workerEnv(tmp string) []string {
	env := make([]string, 0, 16)
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		switch key {
		case "HOME", "USER", "LOGNAME", "PATH", "LANG", "TERM", "CLAUDE_CONFIG_DIR", "CODEX_HOME":
			env = append(env, entry)
		default:
			if strings.HasPrefix(key, "LC_") {
				env = append(env, entry)
			}
		}
	}
	env = append(env, "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL="+os.DevNull, "GIT_TERMINAL_PROMPT=0")
	if tmp != "" {
		env = append(env, "TMPDIR="+tmp, "GOCACHE="+filepath.Join(tmp, "go-cache"),
			"npm_config_cache="+filepath.Join(tmp, "npm-cache"))
	}
	return env
}

func preflight(ctx context.Context, harness, cwd string) (identity, error) {
	var check []string
	switch harness {
	case "claude":
		check = []string{"auth", "status", "--json"}
	case "codex":
		check = []string{"-c", `model_provider="openai"`, "login", "status"}
	default:
		return identity{}, fmt.Errorf("unknown harness %q", harness)
	}
	bin, err := exec.LookPath(harness)
	if err != nil {
		return identity{}, fmt.Errorf("%s CLI unavailable", harness)
	}
	bin, err = filepath.EvalSymlinks(bin)
	if err != nil {
		return identity{}, err
	}
	out, stderr, err := runNative(ctx, bin, cwd, workerEnv(""), check, "")
	authMode := "Claude first-party subscription"
	if harness == "claude" {
		var auth struct {
			LoggedIn         bool   `json:"loggedIn"`
			AuthMethod       string `json:"authMethod"`
			APIProvider      string `json:"apiProvider"`
			SubscriptionType string `json:"subscriptionType"`
		}
		if err != nil || json.Unmarshal(out, &auth) != nil || !auth.LoggedIn ||
			auth.AuthMethod != "claude.ai" || auth.APIProvider != "firstParty" ||
			!strings.Contains(" pro max ultra team enterprise ", " "+auth.SubscriptionType+" ") {
			return identity{}, fmt.Errorf("Claude first-party paid subscription login could not be verified")
		}
	} else {
		authMode = "ChatGPT login"
		if err != nil || !chatGPTLogin(out, stderr) {
			return identity{}, fmt.Errorf("Codex ChatGPT login could not be verified")
		}
	}
	out, _, err = runNative(ctx, bin, cwd, workerEnv(""), []string{"--version"}, "")
	version := strings.TrimSpace(string(out))
	if err != nil || version == "" || len(version) > 200 || strings.ContainsAny(version, "\r\n") {
		return identity{}, fmt.Errorf("could not verify %s CLI version", harness)
	}
	return identity{bin, version, authMode}, nil
}

func chatGPTLogin(out, stderr []byte) bool {
	found := false
	for _, line := range strings.Split(string(out)+"\n"+string(stderr), "\n") {
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

func pathWithin(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && filepath.IsLocal(rel)
}

func codexBuilderGoRoot(s snapshot) (string, error) {
	hasModule := false
	for _, name := range []string{"go.mod", filepath.Join("backend", "go.mod")} {
		if info, err := os.Lstat(filepath.Join(s.checkout, name)); err == nil && info.Mode().IsRegular() {
			hasModule = true
			break
		}
	}
	if !hasModule {
		return "", nil
	}

	installError := fmt.Errorf("Codex builder requires a standalone Go installation (Go 1.26 or later) on the operator's PATH")
	goPath, err := exec.LookPath("go")
	if err != nil || !filepath.IsAbs(goPath) {
		return "", installError
	}
	goPath, err = filepath.EvalSymlinks(goPath)
	goBin := filepath.Dir(goPath)
	goRoot := filepath.Dir(goBin)
	if err != nil || filepath.Base(goPath) != "go" || filepath.Base(goBin) != "bin" ||
		goRoot == filepath.Dir(goRoot) || strings.ContainsAny(goRoot, "*?[]") {
		return "", installError
	}
	home := os.Getenv("HOME")
	if physical, err := filepath.EvalSymlinks(home); err == nil {
		home = physical
	}
	if pathWithin(goRoot, home) || pathWithin(goRoot, s.source) || pathWithin(s.source, goRoot) ||
		pathWithin(goRoot, s.temp) || pathWithin(s.temp, goRoot) {
		return "", installError
	}
	// A launcher in a shared bin directory must not grant access to its parent.
	versionFile, err := os.Lstat(filepath.Join(goRoot, "VERSION"))
	if err != nil || !versionFile.Mode().IsRegular() {
		return "", installError
	}
	compiler, err := os.Lstat(filepath.Join(goRoot, "pkg", "tool", runtime.GOOS+"_"+runtime.GOARCH, "compile"))
	if err != nil || !compiler.Mode().IsRegular() || compiler.Mode().Perm()&0111 == 0 {
		return "", installError
	}
	// Inspect the selected executable without launching a host process from PATH.
	info, err := buildinfo.ReadFile(goPath)
	if err != nil || version.Compare(info.GoVersion, "go1.26.0") < 0 {
		return "", fmt.Errorf("Codex builder requires Go 1.26 or later from the selected Go executable")
	}
	return goRoot, nil
}

var errClaudeIncompleteResult = errors.New("Claude did not report a successful completed result")

func execute(ctx context.Context, bin string, chosen selection, s snapshot, prompt string, r role) ([]byte, error) {
	reviewing := r.stage == "review" || r.stage == "approve"
	tmp := filepath.Join(s.temp, "scratch")
	if err := os.Mkdir(tmp, 0700); err != nil {
		return nil, err
	}
	var args []string
	env := workerEnv(tmp)
	output := filepath.Join(s.temp, "response.md")
	if chosen.Harness == "codex" {
		access := "read"
		if r.stage == "build" {
			access = "write"
		}
		fs := `permissions.factory_worker.filesystem={":minimal"="read",` +
			strconv.Quote(s.checkout) + "=" + strconv.Quote(access) + "," + strconv.Quote(bin) + `="read"`
		if r.stage == "build" {
			fs += "," + strconv.Quote(tmp) + `="write",` +
				strconv.Quote(filepath.Join(s.checkout, ".git")) + `="read"`
			goRoot, err := codexBuilderGoRoot(s)
			if err != nil {
				return nil, err
			}
			if goRoot != "" {
				fs += "," + strconv.Quote(goRoot) + `="read"`
				// The original PATH may reach Go through a symlink outside the profile.
				path := filepath.Join(goRoot, "bin") + string(os.PathListSeparator) + os.Getenv("PATH")
				env = append(env, "PATH="+path, "GOTOOLCHAIN=local")
			}
		}
		args = []string{"-a", "never", "exec", "--ignore-user-config", "--ephemeral",
			"--model", chosen.Model, "--color", "never",
			"-c", `model_provider="openai"`, "-c", `web_search="disabled"`,
			"-c", `default_permissions="factory_worker"`, "-c", fs + "}",
			"-c", "permissions.factory_worker.network.enabled=false"}
		if r.stage == "build" || reviewing {
			// Login startup and its saved environment can both replace the worker's PATH.
			args = append(args, "-c", "allow_login_shell=false", "-c", "features.shell_snapshot=false")
		}
		if reviewing {
			args = append(args, "--json", "-c", `shell_environment_policy.inherit="none"`)
		} else {
			args = append(args, "--output-last-message", output)
		}
		if chosen.Effort != "" {
			args = append(args, "-c", "model_reasoning_effort="+strconv.Quote(chosen.Effort))
		}
	} else {
		settings, tools, err := claudeSettings(s, r.stage)
		if err != nil {
			return nil, err
		}
		format := "json"
		if reviewing {
			format = "stream-json"
		}
		args = []string{"--restricted", "--setting-sources", "", "--strict-mcp-config",
			"--mcp-config", `{"mcpServers":{}}`, "--settings", settings, "--tools", tools,
			"--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--permission-mode", "dontAsk",
			"--permission-prompts", "none", "--output-format", format, "--model", chosen.Model}
		if reviewing {
			args = append(args, "--verbose")
		}
		if chosen.Effort != "" {
			args = append(args, "--effort", chosen.Effort)
		}
		args = append(args, "--print")
	}
	if chosen.Harness == "codex" {
		args = append(args, "-")
	}
	if reviewing {
		return executeReview(ctx, bin, chosen.Harness, s, args, prompt, tmp)
	}
	out, _, err := runNative(ctx, bin, s.checkout, env, args, prompt)
	var result struct {
		Type    string `json:"type"`
		Subtype string `json:"subtype"`
		IsError *bool  `json:"is_error"`
		Result  string `json:"result"`
	}
	var decodeErr error
	if chosen.Harness == "claude" {
		decodeErr = json.Unmarshal(bytes.TrimSpace(out), &result)
	}
	if err != nil {
		return nil, fmt.Errorf("%s worker failed (%w); no alternate harness or credentials tried", chosen.Harness, err)
	}
	if chosen.Harness == "codex" {
		content, err := readRegular(output, maxArtifact)
		if err != nil {
			return nil, err
		}
		return content, nil
	}
	if decodeErr != nil || result.Type != "result" ||
		result.Subtype != "success" || result.IsError == nil || *result.IsError {
		return nil, errClaudeIncompleteResult
	}
	return []byte(result.Result), nil
}

func executeReview(ctx context.Context, bin, harness string, s snapshot, args []string, prompt, tmp string) ([]byte, error) {
	expected := make(map[string][]byte, len(s.reviewReads))
	for _, read := range s.reviewReads {
		expected[reviewReadKey(read.path, harness)] = read.data
	}
	events, err := runNativeReview(ctx, bin, s.checkout, workerEnv(tmp), args, prompt+reviewReadInstructions(s.reviewReads, harness))
	if err != nil {
		return nil, err
	}
	// A successful check establishes observable tool activity before the verdict.
	// It cannot establish provider delivery or model understanding. The stream
	// remains in bounded memory and is never included in an artifact or error.
	supplemental := &supplementalReviewReads{ctx: ctx, checkout: s.checkout, head: s.head}
	if harness == "codex" {
		return validateCodexReadEvents(events, expected, supplemental)
	}
	return validateClaudeReadEvents(events, expected, supplemental)
}

// The controller creates this directory with os.MkdirTemp inside the checkout.
// Only a literal name can be interpolated into a native permission pattern.
var privateInputName = regexp.MustCompile(`^\.factory-job-[A-Za-z0-9]+$`)

func claudeSettings(s snapshot, stage string) (string, string, error) {
	tools := "Read,Grep,Glob,Bash"
	allow := []string{"Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git status *)", "Bash(git rev-parse *)"}
	var deny []string
	denyWrite := []string{s.checkout}
	switch stage {
	case "build":
		jobName := filepath.Base(s.job)
		if !filepath.IsAbs(s.checkout) || filepath.Dir(s.job) != s.checkout || !privateInputName.MatchString(jobName) {
			return "", "", fmt.Errorf("Claude builder requires its private snapshot input directory")
		}
		tools += ",Edit,Write"
		// Native Edit and Write both match Edit(path) rules. The shell's OS
		// sandbox uses physical paths and does not govern those native tools.
		allow = []string{"Bash(*)", "Edit(./**)"}
		denyWrite = nil
		for _, name := range []string{".git", ".claude", jobName} {
			deny = append(deny, "Edit(./"+name+")", "Edit(./"+name+"/**)")
			denyWrite = append(denyWrite, filepath.Join(s.checkout, name))
		}
	case "review", "approve":
		tools = "Read"
		allow = []string{}
	}
	permissions := map[string]any{
		"allow": allow, "blockReadsOutsideWorkingDirectories": true, "blockWritesOutsideWorkingDirectories": true,
	}
	if len(deny) > 0 {
		permissions["deny"] = deny
	}
	settings := map[string]any{
		"disableAllHooks":           true,
		"disableClaudeAiConnectors": true,
		"permissions":               permissions,
		"sandbox": map[string]any{
			"enabled": true, "failIfUnavailable": true, "autoAllowBashIfSandboxed": false,
			"allowUnsandboxedCommands": false,
			"filesystem":               map[string]any{"denyWrite": denyWrite},
			"network":                  map[string]any{"allowedDomains": []string{}, "strictAllowlist": true},
		},
	}
	out, err := json.Marshal(settings)
	return string(out), tools, err
}
