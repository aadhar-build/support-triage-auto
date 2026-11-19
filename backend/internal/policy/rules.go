// Package policy is the Go port of src/triage/policy_check.py (rules and
// always-escalate topics loaders, Check, IsSafeToSend, MatchedTopics) and
// src/triage/quoting.py (QuoteConversation). It matches the Python reference
// byte for byte on the goldens under testdata/golden/policy/.
package policy

import (
	"fmt"
	"os"
	"strings"
	"unicode/utf8"
)

// RulesError is any loader failure. Its message is "<source>: <problem>",
// exactly as Python formats it.
type RulesError struct {
	Source  string
	Problem string
}

func (e *RulesError) Error() string { return e.Source + ": " + e.Problem }

// Rule is one public reply rule.
type Rule struct {
	ID, Type string
	Phrases  []string
	Detect   []string
	patterns []*phrasePattern
}

// Topic is one always-escalate topic.
type Topic struct {
	ID       string
	Phrases  []string
	patterns []*phrasePattern
}

var (
	ruleTypes = []string{"banned_phrase", "pii_echo"}
	detectors = []string{"card_number", "email_from_customer", "phone_from_customer"}
)

// pyPathString renders a path as Python's str(Path(path)) does on POSIX.
func pyPathString(p string) string {
	root := ""
	switch {
	case strings.HasPrefix(p, "//") && !strings.HasPrefix(p, "///"):
		root = "//"
	case strings.HasPrefix(p, "/"):
		root = "/"
	}
	var parts []string
	for _, part := range strings.Split(p, "/") {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	s := root + strings.Join(parts, "/")
	if s == "" {
		return "."
	}
	return s
}

// readRulesObject is _read_rules_object: the file's top-level JSON object.
func readRulesObject(path string) (Value, string, error) {
	source := pyPathString(path)
	fail := func(format string, a ...any) error {
		return &RulesError{source, fmt.Sprintf(format, a...)}
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return Value{}, source, fail("cannot read rules file (%v)", err)
	}
	// Checked before decoding: encoding/json would swap bad bytes for U+FFFD.
	if !utf8.Valid(b) {
		return Value{}, source, fail("cannot read rules file (invalid UTF-8)")
	}
	data, err := ParseJSON(b)
	if err != nil {
		return Value{}, source, fail("not valid JSON (%v)", err)
	}
	if data.Kind != Object {
		return Value{}, source, fail("top level must be a JSON object")
	}
	return data, source, nil
}

// LoadRules reads the rules file at path and returns its public reply rules
// in file order. Any missing, unreadable, malformed or empty file is a
// *RulesError, never an empty ruleset.
func LoadRules(path string) ([]Rule, error) {
	data, source, err := readRulesObject(path)
	if err != nil {
		return nil, err
	}
	return RulesFromData(data, source)
}

// LoadEscalateTopics reads the rules file at path and returns its
// always-escalate topics in file order, failing closed like LoadRules.
func LoadEscalateTopics(path string) ([]Topic, error) {
	data, source, err := readRulesObject(path)
	if err != nil {
		return nil, err
	}
	return TopicsFromData(data, source)
}

// RulesVersion returns the file's top-level "version" string; ok is false when
// it is missing or not a string (Python None).
func RulesVersion(path string) (version string, ok bool, err error) {
	data, _, err := readRulesObject(path)
	if err != nil {
		return "", false, err
	}
	v, found := data.Get("version")
	if !found || v.Kind != String {
		return "", false, nil
	}
	return v.Str, true, nil
}

func isNonEmptyList(v Value, ok bool) bool { return ok && v.Kind == Array && len(v.Items) > 0 }

func isNonEmptyStr(v Value, ok bool) bool { return ok && v.Kind == String && v.Str != "" }

func oneOf(v Value, ok bool, names []string) bool {
	if !ok || v.Kind != String {
		return false
	}
	for _, n := range names {
		if v.Str == n {
			return true
		}
	}
	return false
}

// repr of a dict.get result: a missing key is None.
func reprGot(v Value, ok bool) string {
	if !ok {
		return "None"
	}
	return pyRepr(v)
}

// RulesFromData validates an already-parsed rules object and returns its
// public reply rules, with the same checks and messages as LoadRules. Every
// phrase pattern is compiled here, once.
func RulesFromData(data Value, source string) ([]Rule, error) {
	fail := func(format string, a ...any) error {
		return &RulesError{source, fmt.Sprintf(format, a...)}
	}
	if data.Kind != Object {
		return nil, fail("top level must be a JSON object")
	}
	raw, ok := data.Get("public_reply_rules")
	if !ok || raw.Kind != Array {
		return nil, fail("'public_reply_rules' is missing or is not a list")
	}
	if len(raw.Items) == 0 {
		return nil, fail("'public_reply_rules' is empty; at least one rule is required")
	}
	rules := []Rule{}
	seen := map[string]bool{}
	for index, item := range raw.Items {
		if item.Kind != Object {
			return nil, fail("rule at index %d is not an object", index)
		}
		idv, ok := item.Get("id")
		if !isNonEmptyStr(idv, ok) {
			return nil, fail("rule at index %d has no 'id' or its 'id' is not a non-empty string", index)
		}
		id := idv.Str
		if seen[id] {
			return nil, fail("duplicate rule id %s", reprStr(id))
		}
		seen[id] = true
		tv, tok := item.Get("type")
		if !oneOf(tv, tok, ruleTypes) {
			return nil, fail("rule %s has type %s; expected one of %s",
				reprStr(id), reprGot(tv, tok), strings.Join(ruleTypes, ", "))
		}
		if tv.Str == "banned_phrase" {
			pv, ok := item.Get("phrases")
			if !isNonEmptyList(pv, ok) {
				return nil, fail("rule %s needs a non-empty 'phrases' list", reprStr(id))
			}
			phrases := make([]string, 0, len(pv.Items))
			for _, p := range pv.Items {
				if !isNonEmptyStr(p, true) {
					return nil, fail("rule %s has a phrase that is not a non-empty string: %s",
						reprStr(id), pyRepr(p))
				}
				phrases = append(phrases, p.Str)
			}
			rules = append(rules, Rule{ID: id, Type: tv.Str, Phrases: phrases, Detect: []string{},
				patterns: compilePhrases(phrases)})
			continue
		}
		dv, ok := item.Get("detect")
		if !isNonEmptyList(dv, ok) {
			return nil, fail("rule %s needs a non-empty 'detect' list", reprStr(id))
		}
		detect := make([]string, 0, len(dv.Items))
		for _, d := range dv.Items {
			if !oneOf(d, true, detectors) {
				return nil, fail("rule %s has unknown detector %s; expected one of %s",
					reprStr(id), pyRepr(d), strings.Join(detectors, ", "))
			}
			detect = append(detect, d.Str)
		}
		rules = append(rules, Rule{ID: id, Type: tv.Str, Phrases: []string{}, Detect: detect})
	}
	return rules, nil
}

// isKebab is TOPIC_ID.fullmatch: [a-z0-9][a-z0-9-]*, ASCII.
func isKebab(s string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		ok := (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (c == '-' && i > 0)
		if !ok {
			return false
		}
	}
	return s != ""
}

// TopicsFromData validates an already-parsed rules object and returns its
// always-escalate topics, with the same checks and messages as
// LoadEscalateTopics. Every phrase pattern is compiled here, once.
func TopicsFromData(data Value, source string) ([]Topic, error) {
	fail := func(format string, a ...any) error {
		return &RulesError{source, fmt.Sprintf(format, a...)}
	}
	if data.Kind != Object {
		return nil, fail("top level must be a JSON object")
	}
	raw, ok := data.Get("escalate_always")
	if !ok || raw.Kind != Array {
		return nil, fail("'escalate_always' is missing or is not a list")
	}
	if len(raw.Items) == 0 {
		return nil, fail("'escalate_always' is empty; at least one topic is required")
	}
	topics := []Topic{}
	seen := map[string]bool{}
	for index, item := range raw.Items {
		if item.Kind != Object {
			return nil, fail("topic at index %d is not an object", index)
		}
		idv, ok := item.Get("id")
		if !isNonEmptyStr(idv, ok) {
			return nil, fail("topic at index %d has no 'id' or its 'id' is not a non-empty string", index)
		}
		id := idv.Str
		if !isKebab(id) {
			return nil, fail("topic id %s is not kebab-case ([a-z0-9][a-z0-9-]*)", reprStr(id))
		}
		if seen[id] {
			return nil, fail("duplicate topic id %s", reprStr(id))
		}
		seen[id] = true
		pv, ok := item.Get("phrases")
		if !isNonEmptyList(pv, ok) {
			return nil, fail("topic %s needs a non-empty 'phrases' list", reprStr(id))
		}
		phrases := make([]string, 0, len(pv.Items))
		for _, p := range pv.Items {
			if !isNonEmptyStr(p, true) {
				return nil, fail("topic %s has a phrase that is not a non-empty string: %s",
					reprStr(id), pyRepr(p))
			}
			phrases = append(phrases, p.Str)
		}
		topics = append(topics, Topic{ID: id, Phrases: phrases, patterns: compilePhrases(phrases)})
	}
	return topics, nil
}
