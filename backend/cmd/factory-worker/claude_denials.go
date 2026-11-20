package main

import (
	"bytes"
	"encoding/json"
	"strings"
)

// Counts describe entries in the native CLI's result, not independently verified
// denials or the cause of a failed build. No tool input, ID or raw name leaves here.
type nativeDenialSummary struct {
	Status     string         `json:"status"`
	Count      *int           `json:"count,omitempty"`
	ByCategory map[string]int `json:"by_category,omitempty"`
}

const (
	maxDenialEnvelope = 1 << 20
	maxDenialField    = 256 << 10
	maxDenialEntries  = 256
)

func claudeNativeDenials(terminal []byte) *nativeDenialSummary {
	unavailable := &nativeDenialSummary{Status: "unavailable"}
	if len(terminal) > maxDenialEnvelope || !uniqueClaudeReadJSON(terminal) {
		return unavailable
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(terminal, &envelope); err != nil {
		return unavailable
	}
	rows := envelope["permission_denials"]
	if len(rows) > maxDenialField {
		return unavailable
	}
	decoder := json.NewDecoder(bytes.NewReader(rows))
	if opener, err := decoder.Token(); err != nil || opener != json.Delim('[') {
		return unavailable
	}
	count := 0
	categories := make(map[string]int)
	for decoder.More() {
		if count >= maxDenialEntries {
			return unavailable
		}
		var entry map[string]json.RawMessage
		var name string
		if err := decoder.Decode(&entry); err != nil {
			return unavailable
		}
		if err := json.Unmarshal(entry["tool_name"], &name); err != nil || strings.TrimSpace(name) == "" {
			return unavailable
		}
		category := "other"
		switch name {
		case "Edit":
			category = "edit"
		case "Write":
			category = "write"
		case "Bash":
			category = "shell"
		case "Read", "Glob", "Grep":
			category = "read"
		}
		categories[category]++
		count++
	}
	return &nativeDenialSummary{Status: "reported", Count: &count, ByCategory: categories}
}
