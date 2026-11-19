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

func TestApprovalChecksReviewVerdictAndExactAuditBytes(t *testing.T) {
	head, base := strings.Repeat("a", 40), strings.Repeat("b", 40)
	const spec = "## Files\n- approved.txt\n"
	const lfReview = "VERDICT: PASS\n\n## Summary\nThe candidate passes.\n"
	const crlfReview = "VERDICT: PASS\r\n\r\n## Summary\r\nThe candidate passes.\r\n"
	jobDir := t.TempDir()
	round := filepath.Join(jobDir, "round-1")
	if err := os.Mkdir(round, 0700); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	state := jobState{Stage: "approve", Round: 1, Reviewers: map[string]string{"code": "sonnet"}}
	write(t, filepath.Join(jobDir, "spec.md"), spec)
	write(t, filepath.Join(jobDir, "build.md"), "## Summary\nBuild evidence.\n")
	write(t, filepath.Join(round, "checks.md"), fmt.Sprintf("GATE: PASS\nCANDIDATE: %s\nBASE: %s\nSPEC-DIGEST: %s\n\n", head, base, contentDigest([]byte(spec))))
	inputs, err := evidenceInputs(ctx, jobDir, state, roles["reviewer-code"])
	if err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name, review, auditedReview, wantError string
	}{
		{name: "LF", review: lfReview},
		{name: "CRLF", review: crlfReview},
		{name: "normalized after publication", review: lfReview, auditedReview: crlfReview, wantError: "audit does not match"},
		{name: "extra carriage return", review: "VERDICT: PASS\r\r\n", wantError: "does not have a passing reviewer"},
		{name: "bare carriage return", review: "VERDICT: PASS\r", wantError: "does not have a passing reviewer"},
		{name: "suffix on first line", review: "VERDICT: PASS pending review\r\n", wantError: "does not have a passing reviewer"},
		{name: "pass only in body", review: "VERDICT: CHANGES\r\n\r\n" + crlfReview, wantError: "does not have a passing reviewer"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			audited := tc.auditedReview
			if audited == "" {
				audited = tc.review
			}
			record, err := json.Marshal(evidenceRecord{
				EvidenceVersion: reviewEvidenceVersion, Role: "reviewer-code", CandidateSHA: head, BaseSHA: base,
				Inputs: inputs, ArtifactSHA256: contentDigest([]byte(audited)),
			})
			if err != nil {
				t.Fatal(err)
			}
			write(t, filepath.Join(round, "review-code.md"), tc.review)
			write(t, filepath.Join(round, "worker-reviewer-code.json"), string(record))
			err = validateStageEvidence(ctx, jobDir, state, head, base)
			if tc.wantError == "" {
				if err != nil {
					t.Fatalf("rejected passing review: %v", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), tc.wantError) {
				t.Fatalf("wanted error containing %q, got %v", tc.wantError, err)
			}
		})
	}
}
