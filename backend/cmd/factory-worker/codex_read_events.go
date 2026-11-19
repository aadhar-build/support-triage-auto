package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"unicode/utf8"
)

const maxCodexReadEventStream = 4 * 1024 * 1024

var codexReadCommand = regexp.MustCompile(`^/bin/cat -- /[a-zA-Z0-9._/-]+$`)
var errCodexReadEvents = errors.New("Codex did not verify complete evidence reads")

type codexReadPhase int

const (
	codexWaitForThread codexReadPhase = iota
	codexWaitForTurn
	codexReadEvidence
	codexWaitForCompletion
	codexReadDone
)

// validateCodexReadEvents inspects trusted native JSONL, never tool or model
// text as events. Its result establishes launcher-observed reads only.
func validateCodexReadEvents(stream []byte, expected map[string][]byte, supplemental *supplementalReviewReads) ([]byte, error) {
	if len(stream) > maxCodexReadEventStream || !utf8.Valid(stream) {
		return nil, errCodexReadEvents
	}
	for command, output := range expected {
		if !codexReadCommand.MatchString(command) || len(output) == 0 {
			return nil, errCodexReadEvents
		}
	}
	phase := codexWaitForThread
	reads := codexEvidenceReads{
		expected: expected, supplemental: supplemental, started: make(map[string]codexReadProof), commands: make(map[string]bool),
	}
	var verdict []byte
	for lineNum := 1; len(stream) != 0; lineNum++ {
		line, rest, terminated := bytes.Cut(stream, []byte{'\n'})
		stream = rest
		event := codexEventObject(line)
		if !terminated || event == nil || !validCodexReadEvent(event, &phase, &reads, &verdict) {
			return nil, fmt.Errorf("%w (event %d)", errCodexReadEvents, lineNum)
		}
	}
	if phase != codexReadDone {
		return nil, errCodexReadEvents
	}
	return verdict, nil
}

func validCodexReadEvent(event map[string]json.RawMessage, phase *codexReadPhase, reads *codexEvidenceReads, verdict *[]byte) bool {
	switch codexEventText(event["type"]) {
	case "thread.started":
		if *phase != codexWaitForThread || len(event) != 2 || codexEventText(event["thread_id"]) == "" {
			return false
		}
		*phase = codexWaitForTurn
	case "turn.started":
		if *phase != codexWaitForTurn || len(event) != 1 {
			return false
		}
		*phase = codexReadEvidence
	case "item.started", "item.completed":
		if *phase != codexReadEvidence || len(event) != 2 {
			return false
		}
		item := codexEventObject(event["item"])
		id := codexEventText(item["id"])
		if id == "" {
			return false
		}
		started := codexEventText(event["type"]) == "item.started"
		switch codexEventText(item["type"]) {
		case "command_execution":
			return reads.record(item, id, started)
		case "agent_message", "reasoning":
			_, reusedID := reads.started[id]
			textValue := item["text"]
			if started || reusedID || len(item) != 3 || len(textValue) == 0 || textValue[0] != '"' {
				return false
			}
			reads.started[id] = codexReadProof{}
			if codexEventText(item["type"]) == "reasoning" {
				return true
			}
			text := codexEventText(textValue)
			line, _, _ := strings.Cut(text, "\n")
			switch strings.TrimSuffix(line, "\r") {
			case "VERDICT: PASS", "APPROVE":
				if len(reads.expected) == 0 || reads.completed != len(reads.expected) || reads.pending != 0 {
					return false
				}
			case "VERDICT: CHANGES", "ESCALATE":
				if reads.pending > 0 {
					return false
				}
			default:
				// Commentary cannot become the verdict or follow a verdict.
				return true
			}
			*verdict = []byte(text)
			*phase = codexWaitForCompletion
		default:
			return false
		}
	case "turn.completed":
		if *phase != codexWaitForCompletion || len(event) != 2 || !validCodexReadUsage(event["usage"]) {
			return false
		}
		*phase = codexReadDone
	default:
		return false
	}
	return true
}

type codexReadProof struct {
	command   string
	data      []byte
	mandatory bool
}

type codexEvidenceReads struct {
	supplemental *supplementalReviewReads
	expected     map[string][]byte
	started      map[string]codexReadProof
	commands     map[string]bool
	completed    int
	pending      int
}

func (r *codexEvidenceReads) record(item map[string]json.RawMessage, id string, started bool) bool {
	if len(item) != 6 {
		return false
	}
	command := codexEventText(item["command"])
	canonical := canonicalCodexReadCommand(command)
	if !codexReadCommand.MatchString(canonical) {
		return false
	}
	outputRaw := item["aggregated_output"]
	if len(outputRaw) == 0 || outputRaw[0] != '"' {
		return false
	}
	output := codexEventText(outputRaw)
	status := codexEventText(item["status"])
	if started {
		_, reusedID := r.started[id]
		if reusedID || r.commands[canonical] || status != "in_progress" ||
			string(item["exit_code"]) != "null" || output != "" {
			return false
		}
		data, mandatory := r.expected[canonical]
		if !mandatory {
			var ok bool
			data, ok = r.supplemental.resolve(strings.TrimPrefix(canonical, "/bin/cat -- "))
			if !ok {
				return false
			}
		}
		r.started[id] = codexReadProof{command, data, mandatory}
		r.commands[canonical] = true
		r.pending++
		return true
	}
	proof := r.started[id]
	if proof.command != command || status != "completed" || string(item["exit_code"]) != "0" ||
		!bytes.Equal([]byte(output), proof.data) {
		return false
	}
	// Retain the ID to prevent its reuse, but it can no longer complete again.
	r.started[id] = codexReadProof{}
	if proof.mandatory {
		r.completed++
	}
	r.pending--
	return true
}

func canonicalCodexReadCommand(command string) string {
	for _, prefix := range []string{"/bin/sh -c '", "/bin/bash -c '", "/bin/zsh -c '", "/usr/bin/bash -c '", "/usr/bin/zsh -c '"} {
		if len(command) > len(prefix) && strings.HasPrefix(command, prefix) && strings.HasSuffix(command, "'") {
			return command[len(prefix) : len(command)-1]
		}
	}
	return command
}

func validCodexReadUsage(raw json.RawMessage) bool {
	usage := codexEventObject(raw)
	if usage["input_tokens"] == nil || usage["cached_input_tokens"] == nil || usage["output_tokens"] == nil {
		return false
	}
	for key, value := range usage {
		switch key {
		case "input_tokens", "cached_input_tokens", "output_tokens", "cache_write_input_tokens", "reasoning_output_tokens":
			var count uint64
			if string(value) == "null" || json.Unmarshal(value, &count) != nil {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func codexEventObject(raw []byte) map[string]json.RawMessage {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if token, err := dec.Token(); err != nil || token != json.Delim('{') {
		return nil
	}
	fields := make(map[string]json.RawMessage)
	for dec.More() {
		token, err := dec.Token()
		key, ok := token.(string)
		if err != nil || !ok || fields[key] != nil {
			return nil
		}
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

func codexEventText(raw json.RawMessage) string {
	var text string
	if json.Unmarshal(raw, &text) != nil {
		return ""
	}
	return text
}
