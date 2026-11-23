package factorystate

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

const maxSpecEvidenceBytes = 128 * 1024

type SpecStatus string

const (
	SpecMissing        SpecStatus = "missing-spec"
	SpecInvalid        SpecStatus = "invalid-spec"
	SpecBusy           SpecStatus = "worker-evidence-busy"
	SpecReceiptMissing SpecStatus = "missing-spec-receipt"
	SpecReceiptInvalid SpecStatus = "invalid-spec-receipt"
	SpecBaseUnverified SpecStatus = "unverified-spec-base"
	SpecBaseMismatch   SpecStatus = "stale-spec-base"
	SpecUnbound        SpecStatus = "receipt-lacks-spec-job-binding"
)

type QuestionStatus string

const (
	QuestionsUnknown QuestionStatus = "unknown"
	QuestionsOpen    QuestionStatus = "open"
	QuestionsClear   QuestionStatus = "no-open-markers-observed"
)

// ReadyJobObservation describes native local evidence. Owner authorization is
// unknown for every ready job, and these findings never establish SpecEvidence.
type ReadyJobObservation struct {
	JobID     string
	Spec      SpecStatus
	Questions QuestionStatus
}

var modelID = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:@+-]*$`)

func inspectReady(dir *directory, entries []fs.DirEntry, job object, base string) ReadyJobObservation {
	detail := ReadyJobObservation{JobID: dir.name, Questions: QuestionsUnknown}
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, "worker-") &&
			(strings.HasSuffix(name, ".json.lock") || strings.HasSuffix(name, ".json.pending") || strings.HasSuffix(name, ".json.replacing")) {
			detail.Spec = SpecBusy
			return detail
		}
	}
	spec, err := readRegular(dir.root, "spec.md", maxSpecEvidenceBytes)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		detail.Spec = SpecMissing
		return detail
	case err != nil || !validSpec(spec):
		detail.Spec = SpecInvalid
		return detail
	}
	detail.Questions = QuestionsClear
	if hasOpenQuestions(string(spec)) {
		detail.Questions = QuestionsOpen
	}

	var jobBase string
	if !job.field("spec_base", &jobBase) || !commitID.MatchString(jobBase) ||
		hasStackedBase(job) {
		detail.Spec = SpecBaseUnverified
		return detail
	}
	if jobBase != base {
		detail.Spec = SpecBaseMismatch
		return detail
	}
	audit, err := readRegular(dir.root, "worker-spec-writer.json", maxSpecEvidenceBytes)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		detail.Spec = SpecReceiptMissing
	case err != nil:
		detail.Spec = SpecReceiptInvalid
	default:
		detail.Spec = inspectSpecReceipt(audit, base)
	}
	return detail
}

func hasStackedBase(job object) bool {
	for _, key := range []string{"stacked_on", "stacked_on_branch", "stacked_on_pr"} {
		if val := job[key]; val != nil && !bytes.Equal(val, []byte("null")) {
			return true
		}
	}
	return false
}

func inspectSpecReceipt(raw []byte, base string) SpecStatus {
	evidence, err := decodeObject(raw)
	var role, input, candidate, receiptBase, harness, model, at string
	if err != nil || !evidence.field("role", &role) || role != "spec-writer" ||
		!evidence.field("base_sha", &receiptBase) || !commitID.MatchString(receiptBase) ||
		!evidence.field("input_sha", &input) || !evidence.field("candidate_sha", &candidate) ||
		input != receiptBase || candidate != receiptBase ||
		!evidence.field("harness", &harness) || (harness != "claude" && harness != "codex") ||
		!evidence.field("model", &model) || !modelID.MatchString(model) || !evidence.field("at", &at) {
		return SpecReceiptInvalid
	}
	if _, err := time.Parse(time.RFC3339, at); err != nil {
		return SpecReceiptInvalid
	}
	if receiptBase != base {
		return SpecBaseMismatch
	}
	// The native spec writer audit does not bind the artifact bytes or the
	// ready job revision. A matching local base does not fill either gap.
	return SpecUnbound
}

func nextRoundAvailable(entries []fs.DirEntry, previous int) bool {
	return previous < math.MaxInt && !hasEntry(entries, fmt.Sprintf("round-%d", previous+1))
}

func validSpec(spec []byte) bool {
	if len(spec) == 0 || !utf8.Valid(spec) || bytes.IndexByte(spec, 0) >= 0 {
		return false
	}
	text := "\n" + strings.ReplaceAll(string(spec), "\r\n", "\n") + "\n"
	return strings.Contains(text, "\n## Product questions\n") &&
		strings.Contains(text, "\n## Files\n") && strings.Contains(text, "\n## Acceptance criteria\n")
}

// Factory pickup looks for unchecked items in the Product questions section
// and PRODUCT DECISION markers anywhere outside Owner decisions.
func hasOpenQuestions(text string) bool {
	section := ""
	for _, line := range strings.FieldsFunc(text, isLineBreak) {
		if heading, ok := strings.CutPrefix(line, "##"); ok && startsWithSpace(heading) {
			section = questionSection(heading)
			continue
		}
		if section == "owner decisions" {
			continue
		}
		if strings.Contains(line, "PRODUCT DECISION") || (section == "product questions" && unchecked(line)) {
			return true
		}
	}
	return false
}

func questionSection(heading string) string {
	heading = strings.TrimFunc(heading, isSpace)
	for _, r := range heading {
		if r >= utf8.RuneSelf {
			// The native Unicode lowercase mapping cannot turn any non-ASCII
			// letter into a letter in either of the two recognized headings.
			return ""
		}
	}
	return strings.ToLower(heading)
}

func unchecked(line string) bool {
	line = strings.TrimLeftFunc(line, isSpace)
	if line == "" || (line[0] != '-' && line[0] != '*') {
		return false
	}
	line, ok := strings.CutPrefix(strings.TrimLeftFunc(line[1:], isSpace), "[")
	if !ok || !startsWithSpace(line) {
		return false
	}
	_, size := utf8.DecodeRuneInString(line)
	body, ok := strings.CutPrefix(line[size:], "]")
	return ok && body != ""
}

func startsWithSpace(text string) bool {
	first, _ := utf8.DecodeRuneInString(text)
	return isSpace(first)
}

func isSpace(r rune) bool {
	return unicode.IsSpace(r) || r == '\x1f'
}

func isLineBreak(r rune) bool {
	switch r {
	case '\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029':
		return true
	default:
		return false
	}
}
