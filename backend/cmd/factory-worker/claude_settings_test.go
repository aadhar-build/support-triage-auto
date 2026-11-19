package main

import (
	"encoding/json"
	"testing"
)

func TestClaudeSettingsFailClosedWhenSandboxIsUnavailable(t *testing.T) {
	for _, stage := range []string{"build", "review", "approve"} {
		t.Run(stage, func(t *testing.T) {
			settings, _, err := claudeSettings(t.TempDir(), stage)
			if err != nil {
				t.Fatal(err)
			}
			var config struct {
				Sandbox map[string]any `json:"sandbox"`
			}
			if err := json.Unmarshal([]byte(settings), &config); err != nil {
				t.Fatal(err)
			}
			if config.Sandbox["enabled"] != true || config.Sandbox["failIfUnavailable"] != true ||
				config.Sandbox["allowUnsandboxedCommands"] != false {
				t.Fatalf("expected fail-closed sandbox settings, got %v", config.Sandbox)
			}
			if _, ok := config.Sandbox["requireSandbox"]; ok {
				t.Fatal("settings contain the unrecognized requireSandbox key")
			}
		})
	}
}
