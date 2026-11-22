package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryproposer"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factoryshadow"
	"github.com/aadhar-build/support-triage-auto/backend/internal/factorystate"
)

// The test executable also acts as a fake native CLI. No provider credentials
// or network are used during the command's adapter integration tests.
func TestMain(m *testing.M) {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "--restricted":
			if slices.Contains(os.Args, "auth") {
				fmt.Println(`{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"pro"}`)
			} else {
				fmt.Println(`{"type":"result","subtype":"success","is_error":false,"num_turns":1,"permission_denials":[],"result":"{\"action\":\"no-action\"}"}`)
			}
			os.Exit(0)
		case "-c":
			fmt.Println("Logged in using ChatGPT")
			os.Exit(0)
		case "-a":
			fmt.Println(`{"type":"thread.started","thread_id":"test-thread"}`)
			fmt.Println(`{"type":"turn.started"}`)
			fmt.Println(`{"type":"item.completed","item":{"id":"test-item","type":"agent_message","text":"{\"action\":\"no-action\"}"}}`)
			fmt.Println(`{"type":"turn.completed","usage":{}}`)
			os.Exit(0)
		}
	}
	os.Exit(m.Run())
}

func TestCommandDefaultsToObservation(t *testing.T) {
	checkout := t.TempDir()
	observed := false
	var output, diagnostics bytes.Buffer
	status := run(context.Background(), []string{"-checkout", checkout}, &output, &diagnostics, factoryshadow.Runner{
		Observe: func(_ context.Context, path string) (factorystate.Observation, error) {
			observed = true
			if path != checkout {
				t.Fatalf("checkout = %q, want %q", path, checkout)
			}
			return factorystate.Observation{Snapshot: factorycontroller.Snapshot{Revision: "observation", BaseRevision: "base"}}, nil
		},
		NewProposer: func() (factoryshadow.Proposer, error) {
			t.Fatal("command initialized the model without opt-in")
			return nil, nil
		},
	})
	var result factoryshadow.Report
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	if !observed || status != 0 || diagnostics.Len() != 0 || result.Status != "observed" || result.Observation == nil {
		t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
	}
}

func TestCommandDoesNotEchoInvalidArguments(t *testing.T) {
	const privateText = "PRIVATE-ARGUMENT"
	var output, diagnostics bytes.Buffer
	status := run(context.Background(), []string{"-" + privateText}, &output, &diagnostics, factoryshadow.Runner{})
	if status != 2 || output.Len() != 0 || strings.Contains(diagnostics.String(), privateText) {
		t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
	}
}

func TestCommandRequiresOptInAndReportsModelConfigurationErrors(t *testing.T) {
	checkout := t.TempDir()
	var output, diagnostics bytes.Buffer
	status := run(context.Background(), []string{"-checkout", checkout, "-allow-model"}, &output, &diagnostics, factoryshadow.Runner{
		Observe: func(context.Context, string) (factorystate.Observation, error) {
			return factorystate.Observation{Snapshot: factorycontroller.Snapshot{Revision: "observation", BaseRevision: "base"}}, nil
		},
	})
	var result factoryshadow.Report
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	if status != 2 || diagnostics.Len() != 0 || result.Status != "model-configuration-invalid" || result.Observation == nil {
		t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
	}
}

func TestCommandReportsObservationErrorBeforeModelConfiguration(t *testing.T) {
	checkout := t.TempDir()
	var output, diagnostics bytes.Buffer
	status := run(context.Background(), []string{"-checkout", checkout, "-allow-model"}, &output, &diagnostics, factoryshadow.Runner{
		Observe: func(context.Context, string) (factorystate.Observation, error) {
			return factorystate.Observation{}, errors.New("PRIVATE-OBSERVATION-TEXT")
		},
	})
	var result factoryshadow.Report
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	if status != 1 || diagnostics.Len() != 0 || result.Status != "observation-failed" || strings.Contains(output.String(), "PRIVATE-OBSERVATION-TEXT") {
		t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
	}
}

type noActionProposer struct{}

func (noActionProposer) ProposeWithReport(_ context.Context, snapshot factorycontroller.Snapshot) (factorycontroller.Proposal, []factoryproposer.Attempt, error) {
	return factorycontroller.Proposal{Action: factorycontroller.ActionNoAction, StateRevision: snapshot.Revision}, nil, nil
}

func TestCommandReportsValidatedOptInDecision(t *testing.T) {
	checkout := t.TempDir()
	var output, diagnostics bytes.Buffer
	status := run(context.Background(), []string{"-checkout", checkout, "-allow-model"}, &output, &diagnostics, factoryshadow.Runner{
		Observe: func(context.Context, string) (factorystate.Observation, error) {
			return factorystate.Observation{Snapshot: factorycontroller.Snapshot{Revision: "observation", BaseRevision: "base"}}, nil
		},
		NewProposer: func() (factoryshadow.Proposer, error) { return noActionProposer{}, nil },
	})
	var result factoryshadow.Report
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	if status != 0 || diagnostics.Len() != 0 || result.Status != "accepted" || result.Action != "no-action" || result.Cost != "unknown" {
		t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
	}
}

func TestCommandKeepsOtherProviderAvailableWhenOneBinaryIsMissing(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, chosen, unavailable string
	}{
		{"missing primary", "codex", "claude"},
		{"missing fallback", "claude", "codex"},
	} {
		t.Run(test.name, func(t *testing.T) {
			checkout := t.TempDir()
			claudeHome, codexHome := t.TempDir(), t.TempDir()
			for _, home := range []string{claudeHome, codexHome} {
				if err := os.Chmod(home, 0700); err != nil {
					t.Fatal(err)
				}
			}
			args := []string{"-checkout", checkout, "-allow-model",
				"-" + test.chosen + "-bin", executable,
				"-" + test.unavailable + "-bin", filepath.Join(t.TempDir(), "missing"),
				"-claude-model", "claude-test", "-claude-home", claudeHome,
				"-codex-model", "codex-test", "-codex-home", codexHome,
			}
			var output, diagnostics bytes.Buffer
			status := run(context.Background(), args, &output, &diagnostics, factoryshadow.Runner{
				Observe: func(context.Context, string) (factorystate.Observation, error) {
					return factorystate.Observation{Snapshot: factorycontroller.Snapshot{Revision: "observation", BaseRevision: "base"}}, nil
				},
			})
			var result factoryshadow.Report
			if err := json.Unmarshal(output.Bytes(), &result); err != nil {
				t.Fatalf("decode report: %v", err)
			}
			if status != 0 || result.Status != "accepted" || result.Action != "no-action" || len(result.Attempts) == 0 {
				t.Fatalf("exit = %d, report = %s, diagnostics = %s", status, &output, &diagnostics)
			}
			accepted := result.Attempts[len(result.Attempts)-1]
			if accepted.Provider != test.chosen || accepted.Disposition != "accepted" || accepted.Phase != "decision" || accepted.Failure != "" {
				t.Fatalf("wrong provider accepted: %+v", result.Attempts)
			}
			if test.unavailable == "claude" && (len(result.Attempts) != 2 || result.Attempts[0] != (factoryshadow.ProviderAttempt{Provider: "claude", Disposition: "unavailable", Phase: "setup"})) {
				t.Fatalf("missing primary outcome not reported: %+v", result.Attempts)
			}
		})
	}
}

func TestPathCheckResolvesSymlinksBeforeRejectingCheckoutPaths(t *testing.T) {
	checkout := t.TempDir()
	insideCheckout := filepath.Join(checkout, "untrusted")
	if err := os.WriteFile(insideCheckout, nil, 0700); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	link := filepath.Join(outside, "link")
	if err := os.Symlink(insideCheckout, link); err != nil {
		t.Fatal(err)
	}
	if _, _, err := externalPath(checkout, link, true); err == nil {
		t.Fatal("checkout executable can be loaded through a symlink")
	}
	if _, _, err := externalPath(checkout, insideCheckout, true); err == nil {
		t.Fatal("checkout executable can be loaded directly")
	}
	if _, _, err := externalPath(checkout, filepath.Join(checkout, "missing"), true); err == nil {
		t.Fatal("an absent executable inside the checkout was accepted")
	}
	reverseLink := filepath.Join(checkout, "link-outside")
	if err := os.Symlink(outside, reverseLink); err != nil {
		t.Fatal(err)
	}
	if _, _, err := externalPath(checkout, filepath.Join(reverseLink, "missing"), true); err == nil {
		t.Fatal("a checkout symlink to an external directory was accepted")
	}
	if _, _, err := externalPath(checkout, outside, false); err != nil {
		t.Fatalf("valid external path rejected: %v", err)
	}
}
