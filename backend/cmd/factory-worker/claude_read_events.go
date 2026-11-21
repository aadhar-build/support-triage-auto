package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"unicode/utf8"
)

type claudeReadEvent struct {
	Type      string          `json:"type"`
	Subtype   string          `json:"subtype"`
	SessionID string          `json:"session_id"`
	Event     string          `json:"event"`
	Parent    json.RawMessage `json:"parent_tool_use_id"`
	Message   struct {
		Role       string          `json:"role"`
		StopReason string          `json:"stop_reason"`
		Content    json.RawMessage `json:"content"`
	} `json:"message"`
	StructuredResult      json.RawMessage   `json:"tool_use_result"`
	Error                 json.RawMessage   `json:"error"`
	Errors                []json.RawMessage `json:"errors"`
	IsError               *bool             `json:"is_error"`
	IsMeta                bool              `json:"is_meta"`
	IsSynthetic           bool              `json:"isSynthetic"`
	IsVirtual             bool              `json:"is_virtual"`
	StoppedByPermission   bool              `json:"stopped_by_permission_check"`
	InterruptedByShutdown bool              `json:"interrupted_by_shutdown"`
	PermissionDenials     json.RawMessage   `json:"permission_denials"`
	StopReason            string            `json:"stop_reason"`
	Result                string            `json:"result"`
}

type claudeReadBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Input     json.RawMessage `json:"input"`
	ToolUseID string          `json:"tool_use_id"`
	Content   string          `json:"content"`
	IsError   json.RawMessage `json:"is_error"`
}

type claudeReadProof struct {
	path      string
	data      []byte
	mandatory bool
	completed bool
}

var errClaudeReadStream = errors.New("Claude returned an invalid review event stream")
var errClaudeEvidenceReads = errors.New("Claude review could not verify complete observable evidence reads; request human review")
var errClaudePermissionDenials = errors.New("Claude review reported tool permission denials or omitted valid permission denial metadata")

// These codes are launcher constants. Never include provider-supplied text in refusals.
type claudeEvidenceIssue string

const (
	noEvidenceIssue            claudeEvidenceIssue = ""
	invalidExpectedFrame       claudeEvidenceIssue = "invalid_expected_frame"
	invalidReadInput           claudeEvidenceIssue = "invalid_read_input"
	unsupportedReadWindow      claudeEvidenceIssue = "unsupported_read_window"
	supplementalReadUnverified claudeEvidenceIssue = "supplemental_read_unverified"
	duplicateRead              claudeEvidenceIssue = "duplicate_read"
	structuredResultMismatch   claudeEvidenceIssue = "structured_result_mismatch"
	renderedResultMismatch     claudeEvidenceIssue = "rendered_result_mismatch"
	incompleteMandatoryReads   claudeEvidenceIssue = "incomplete_mandatory_reads"
)

func validateClaudeReadEvents(stream []byte, expected map[string][]byte, supplemental *supplementalReviewReads) ([]byte, error) {
	mandatoryRequested, delivered, pending := 0, 0, 0
	refuse := func(reason claudeEvidenceIssue) error {
		return fmt.Errorf(`%w; diagnostic={"reason":%q,"mandatory_expected":%d,"mandatory_requested":%d,"mandatory_completed":%d,"pending_reads":%d}`,
			errClaudeEvidenceReads, reason, len(expected), mandatoryRequested, delivered, pending)
	}
	for path, raw := range expected {
		if !filepath.IsAbs(path) || filepath.Clean(path) != path || len(raw) == 0 ||
			!utf8.Valid(raw) || bytes.HasPrefix(raw, []byte("\xef\xbb\xbf")) || hasUnsafeReviewControls(raw) {
			return nil, refuse(invalidExpectedFrame)
		}
	}
	var session, latestText, final string
	initialized := false
	calls := make(map[string]claudeReadProof, len(expected))
	requested := make(map[string]bool, len(expected))
	for line := range bytes.SplitSeq(stream, []byte{'\n'}) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var event claudeReadEvent
		if final != "" || !uniqueClaudeReadJSON(line) || json.Unmarshal(line, &event) != nil ||
			event.SessionID == "" || (session != "" && event.SessionID != session) {
			return nil, errClaudeReadStream
		}
		if event.Type == "result" {
			var denials []json.RawMessage
			if json.Unmarshal(event.PermissionDenials, &denials) != nil || denials == nil || len(denials) != 0 {
				return nil, errClaudePermissionDenials
			}
		}
		if (len(event.Error) != 0 && !bytes.Equal(event.Error, []byte("null"))) || len(event.Errors) > 0 ||
			(event.IsError != nil && *event.IsError) || event.StoppedByPermission || event.InterruptedByShutdown ||
			event.IsMeta || event.IsSynthetic || event.IsVirtual {
			return nil, errClaudeReadStream
		}
		if session == "" {
			session = event.SessionID
		}
		if event.Type == "system" && event.Subtype == "ui_invalidate" && event.Event == "ui.render" {
			continue
		}
		if !initialized {
			if event.Type != "system" || event.Subtype != "init" {
				return nil, errClaudeReadStream
			}
			initialized = true
			continue
		}
		switch event.Type {
		case "system":
			if event.Subtype != "thinking_tokens" || !validClaudeThinkingTokens(line) {
				return nil, errClaudeReadStream
			}
		case "rate_limit_event":
		case "assistant", "user":
			var blocks []claudeReadBlock
			if !bytes.Equal(event.Parent, []byte("null")) || event.Message.Role != event.Type ||
				json.Unmarshal(event.Message.Content, &blocks) != nil || len(blocks) == 0 {
				return nil, errClaudeReadStream
			}
			if event.Type == "assistant" {
				switch event.Message.StopReason {
				case "", "tool_use", "end_turn":
				default:
					return nil, errClaudeReadStream
				}
			}
			latestText = ""
			for _, block := range blocks {
				switch block.Type {
				case "text", "thinking", "redacted_thinking":
					if event.Type == "assistant" && block.Type == "text" {
						latestText += block.Text
					}
				case "tool_use":
					_, duplicate := calls[block.ID]
					if event.Type != "assistant" || block.Name != "Read" || block.ID == "" || duplicate {
						return nil, errClaudeReadStream
					}
					proof, issue := claudeEvidenceRead(block.Input, expected, supplemental)
					if issue != noEvidenceIssue {
						return nil, refuse(issue)
					}
					if requested[proof.path] {
						return nil, refuse(duplicateRead)
					}
					if proof.mandatory {
						mandatoryRequested++
					}
					requested[proof.path] = true
					calls[block.ID] = proof
					pending++
				case "tool_result":
					proof, ok := calls[block.ToolUseID]
					if event.Type != "user" || !ok || proof.completed ||
						(len(block.IsError) != 0 && string(block.IsError) != "false") {
						return nil, errClaudeReadStream
					}
					if issue := checkClaudeEvidenceResult(block.Content, event.StructuredResult, proof.path, proof.data); issue != noEvidenceIssue {
						return nil, refuse(issue)
					}
					proof.data = nil
					proof.completed = true
					calls[block.ToolUseID] = proof
					pending--
					if proof.mandatory {
						delivered++
					}
				default:
					return nil, errClaudeReadStream
				}
			}
		case "result":
			if event.Subtype != "success" || event.IsError == nil || (event.StopReason != "" && event.StopReason != "end_turn") ||
				pending != 0 || event.Result != latestText {
				return nil, errClaudeReadStream
			}
			line, _, _ := strings.Cut(event.Result, "\n")
			switch strings.TrimSuffix(line, "\r") {
			case "VERDICT: PASS", "APPROVE":
				if delivered == 0 || delivered != len(expected) {
					return nil, refuse(incompleteMandatoryReads)
				}
			case "VERDICT: CHANGES", "ESCALATE":
			default:
				return nil, errClaudeReadStream
			}
			final = event.Result
		default:
			return nil, errClaudeReadStream
		}
	}
	if final == "" {
		return nil, errClaudeReadStream
	}
	return []byte(final), nil
}

func validClaudeThinkingTokens(line []byte) bool {
	// The caller verifies the event type, subtype, and session. Require their
	// exact keys here because encoding/json matches struct fields case-insensitively.
	var fields map[string]json.RawMessage
	if json.Unmarshal(line, &fields) != nil || len(fields) != 6 ||
		fields["type"] == nil || fields["subtype"] == nil || fields["session_id"] == nil {
		return false
	}
	var id string
	var total, delta *int64
	return json.Unmarshal(fields["uuid"], &id) == nil && strings.TrimSpace(id) != "" &&
		json.Unmarshal(fields["estimated_tokens"], &total) == nil && total != nil &&
		json.Unmarshal(fields["estimated_tokens_delta"], &delta) == nil && delta != nil
}

func claudeEvidenceRead(data json.RawMessage, expected map[string][]byte, supplemental *supplementalReviewReads) (claudeReadProof, claudeEvidenceIssue) {
	var input struct {
		Path   string `json:"file_path"`
		Offset *int   `json:"offset"`
		Limit  *int   `json:"limit"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil {
		return claudeReadProof{}, invalidReadInput
	}
	if input.Offset != nil && *input.Offset != 1 {
		return claudeReadProof{}, unsupportedReadWindow
	}
	content, mandatory := expected[input.Path]
	if !mandatory {
		var ok bool
		content, ok = supplemental.resolve(input.Path)
		if !ok {
			return claudeReadProof{}, supplementalReadUnverified
		}
	}
	limit := 2000 // Claude 2.1.325's default Read line limit.
	if input.Limit != nil {
		limit = *input.Limit
	}
	if limit <= bytes.Count(content, []byte{'\n'}) {
		return claudeReadProof{}, unsupportedReadWindow
	}
	return claudeReadProof{path: input.Path, data: content, mandatory: mandatory}, noEvidenceIssue
}

func checkClaudeEvidenceResult(rendered string, metadata json.RawMessage, path string, raw []byte) claudeEvidenceIssue {
	lines := strings.Split(string(raw), "\n")
	if len(metadata) != 0 {
		var result struct {
			Type string `json:"type"`
			File struct {
				Path      string `json:"filePath"`
				Content   string `json:"content"`
				Start     int    `json:"startLine"`
				Count     int    `json:"numLines"`
				Total     int    `json:"totalLines"`
				Truncated bool   `json:"truncatedByTokenCap"`
			} `json:"file"`
		}
		if json.Unmarshal(metadata, &result) != nil || result.Type != "text" || result.File.Path != path ||
			result.File.Content != string(raw) || result.File.Start != 1 || result.File.Count != len(lines) ||
			result.File.Total != len(lines) || result.File.Truncated {
			return structuredResultMismatch
		}
	}
	// The native renderer preserves final LF as an empty numbered line. It uses
	// a colon instead of a tab in its tab-aware mode for tab-indented content.
	for _, sep := range []string{"\t", ":"} {
		if sep == ":" && !bytes.HasPrefix(raw, []byte{'\t'}) && !bytes.Contains(raw, []byte{'\n', '\t'}) {
			continue
		}
		var display strings.Builder
		for i, line := range lines {
			if i != 0 {
				display.WriteByte('\n')
			}
			display.WriteString(strconv.Itoa(i + 1))
			display.WriteString(sep)
			display.WriteString(line)
		}
		if rendered == display.String() {
			return noEvidenceIssue
		}
	}
	return renderedResultMismatch
}

// encoding/json accepts repeated keys by default. Evidence must have one
// interpretation, including IDs, verdicts, and permission-denial fields.
func uniqueClaudeReadJSON(raw []byte) bool {
	if !utf8.Valid(raw) {
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	return uniqueClaudeReadValue(decoder, 0) && !decoder.More()
}

func uniqueClaudeReadValue(decoder *json.Decoder, depth int) bool {
	if depth > 64 {
		return false
	}
	token, err := decoder.Token()
	if err != nil {
		return false
	}
	switch token {
	case json.Delim('{'):
		seen := make(map[string]bool)
		for decoder.More() {
			key, err := decoder.Token()
			name, ok := key.(string)
			if err != nil || !ok || seen[strings.ToLower(name)] || !uniqueClaudeReadValue(decoder, depth+1) {
				return false
			}
			seen[strings.ToLower(name)] = true
		}
	case json.Delim('['):
		for decoder.More() {
			if !uniqueClaudeReadValue(decoder, depth+1) {
				return false
			}
		}
	default:
		_, delimiter := token.(json.Delim)
		return !delimiter
	}
	_, err = decoder.Token()
	return err == nil
}
