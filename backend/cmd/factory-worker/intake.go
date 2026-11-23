package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type intakeFailureReason string

func (r intakeFailureReason) Error() string { return string(r) }

var intakeHeaders = []struct {
	line   *regexp.Regexp
	reason intakeFailureReason
}{
	{regexp.MustCompile(`^DECISION: (ACCEPT|NEEDS-INFO|REJECT)$`), "decision_header"},
	{regexp.MustCompile(`^RISK: (low|medium|high)$`), "risk_header"},
	{regexp.MustCompile(`^SURFACES: (?:none|(?:security|eval|ui)(?:, (?:security|eval|ui))*)$`), "surfaces_header"},
	{regexp.MustCompile(`^RESCOPED: (yes|no)$`), "rescoped_header"},
}

func validateIntakeArtifact(raw []byte) error {
	switch {
	case len(raw) == 0:
		return fmt.Errorf("intake returned a missing, invalid or oversized artifact (%w)", intakeFailureReason("missing_artifact"))
	case len(raw) > maxArtifact:
		return fmt.Errorf("intake returned a missing, invalid or oversized artifact (%w)", intakeFailureReason("oversized_artifact"))
	case bytes.IndexByte(raw, 0) >= 0:
		return fmt.Errorf("intake returned a missing, invalid or oversized artifact (%w)", intakeFailureReason("invalid_artifact_text"))
	}
	s := strings.ReplaceAll(string(raw), "\r\n", "\n")
	remaining := s
	for _, header := range intakeHeaders {
		line, rest, _ := strings.Cut(remaining, "\n")
		if !header.line.MatchString(line) {
			return fmt.Errorf("intake returned an artifact that does not match its role format (%w)", header.reason)
		}
		remaining = rest
	}
	if !hasSection(remaining, "Summary") {
		return fmt.Errorf("intake returned an artifact that does not match its role format (%w)", intakeFailureReason("summary_heading"))
	}
	return nil
}

func intakeProviderFailure(err error) intakeFailureReason {
	var exit *exec.ExitError
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return "provider_timeout"
	case errors.As(err, &exit):
		return "provider_exit"
	case errors.Is(err, errClaudeIncompleteResult):
		return "provider_incomplete_result"
	default:
		return "worker_output_unavailable"
	}
}

// Keep the latest refusal separate from consumable intake evidence. Provider
// output and error text can contain secrets or private issue data.
func recordIntakeFailure(ctx context.Context, jobDirectory *os.File, state []byte, auditPath string,
	chosen selection, input string, reason intakeFailureReason, reportBytes int, cause error) error {
	path := auditPath + ".failure"
	model := chosen.Model
	if len(model) > 128 {
		model = ""
	}
	record := struct {
		Harness     string              `json:"harness"`
		Model       string              `json:"model,omitempty"`
		InputSHA    string              `json:"input_sha"`
		Reason      intakeFailureReason `json:"reason"`
		ReportBytes int                 `json:"report_bytes,omitempty"`
		At          string              `json:"at"`
	}{chosen.Harness, model, input, reason, reportBytes, time.Now().UTC().Format(time.RFC3339)}
	encoded, err := json.Marshal(record)
	if err == nil {
		// A model deadline must still allow a short, bounded admission check.
		ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		err = withJobAdmission(ctx, jobDirectory, state, "", func() error {
			if err := writeAtomicFile(path, append(encoded, '\n'), true); err != nil {
				return err
			}
			return jobDirectory.Sync()
		})
	}
	if err != nil {
		return errors.Join(cause, fmt.Errorf("record intake failure: %w", err))
	}
	return fmt.Errorf("%w. See %s", cause, filepath.Base(path))
}
