package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func validateStageEvidence(jobDir string, state jobState, head string) error {
	if state.Stage != "review" && state.Stage != "approve" {
		return nil
	}
	round := filepath.Join(jobDir, fmt.Sprintf("round-%d", state.Round))
	checks, err := readRegular(filepath.Join(round, "checks.md"), 4*1024*1024)
	if err != nil || !strings.HasPrefix(string(checks), "GATE: PASS\n") {
		return errors.New("current round does not have a passing gate")
	}
	if candidate := recordedCandidate(string(checks)); candidate != "" && candidate != head {
		return errors.New("passing gate is for a different candidate")
	}
	if state.Stage != "approve" {
		return nil
	}
	if len(state.Reviewers) == 0 {
		return errors.New("no reviewers are routed for this job")
	}
	for name := range state.Reviewers {
		role, ok := roles["reviewer-"+name]
		if !ok {
			return fmt.Errorf("unknown routed reviewer %q", name)
		}
		review, err := readRegular(filepath.Join(round, role.artifact), maxArtifact)
		if err != nil || !strings.HasPrefix(string(review), "VERDICT: PASS\n") {
			return fmt.Errorf("current round does not have a passing %s", role.name)
		}
		audit := filepath.Join(round, "worker-"+role.name+".json")
		var record struct {
			CandidateSHA string `json:"candidate_sha"`
		}
		if err := readJSON(audit, &record); !errors.Is(err, os.ErrNotExist) && (err != nil || record.CandidateSHA != head) {
			return fmt.Errorf("%s audit does not match the current candidate", role.name)
		}
	}
	return nil
}

func recordedCandidate(checks string) string {
	_, rest, _ := strings.Cut(checks, "\n")
	line, _, _ := strings.Cut(rest, "\n")
	value, _ := strings.CutPrefix(line, "CANDIDATE: ")
	if value == line {
		return ""
	}
	return value
}
