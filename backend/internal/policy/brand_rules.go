package policy

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"unicode/utf8"
)

// This file ports src/triage/brand_rules.py: the effective rules of a JSON
// rules file or of a brand file's single fenced ```rules block.

const (
	fenceOpen  = "```rules"
	fenceClose = "```"
)

// EffectiveRules is Python's EffectiveRules: everything a run takes from one
// rules source. RepeatContact, Version and Status are nil when absent (None).
type EffectiveRules struct {
	Rules         []Rule
	Topics        []Topic
	RepeatContact *RepeatContactRule
	Version       *string
	Status        *string
}

// EffectiveFromData validates a parsed rules object in Python's order: rules,
// topics, then repeat contact when the key is present. The first error wins.
func EffectiveFromData(data Value, source string) (EffectiveRules, error) {
	rules, err := RulesFromData(data, source)
	if err != nil {
		return EffectiveRules{}, err
	}
	topics, err := TopicsFromData(data, source)
	if err != nil {
		return EffectiveRules{}, err
	}
	eff := EffectiveRules{Rules: rules, Topics: topics}
	if _, ok := data.Get("repeat_contact"); ok {
		rc, err := RepeatContactFromData(data, source)
		if err != nil {
			var rce *RepeatContactError
			if errors.As(err, &rce) {
				// Python: raise RulesError(str(exc)), the same text.
				return EffectiveRules{}, &RulesError{Source: "repeat_contact rule in " + rce.Source, Problem: rce.Problem}
			}
			return EffectiveRules{}, err
		}
		eff.RepeatContact = &rc
	}
	eff.Version = optString(data, "version")
	eff.Status = optString(data, "status")
	return eff, nil
}

func optString(data Value, key string) *string {
	v, ok := data.Get(key)
	if !ok || v.Kind != String {
		return nil
	}
	s := v.Str
	return &s
}

// LoadEffective reads a JSON rules file and returns its effective rules.
func LoadEffective(path string) (EffectiveRules, error) {
	data, source, err := readRulesObject(path)
	if err != nil {
		return EffectiveRules{}, err
	}
	return EffectiveFromData(data, source)
}

// LoadBrandFile reads a brand guidelines file and returns the effective rules
// of its one ```rules block.
func LoadBrandFile(path string) (EffectiveRules, error) {
	data, source, err := brandObject(path)
	if err != nil {
		return EffectiveRules{}, err
	}
	return EffectiveFromData(data, source)
}

// Equal is Python's dataclass equality on the public fields.
func (e EffectiveRules) Equal(o EffectiveRules) bool {
	if !slices.EqualFunc(e.Rules, o.Rules, func(a, b Rule) bool {
		return a.ID == b.ID && a.Type == b.Type && slices.Equal(a.Phrases, b.Phrases) && slices.Equal(a.Detect, b.Detect)
	}) {
		return false
	}
	if !slices.EqualFunc(e.Topics, o.Topics, func(a, b Topic) bool {
		return a.ID == b.ID && slices.Equal(a.Phrases, b.Phrases)
	}) {
		return false
	}
	if (e.RepeatContact == nil) != (o.RepeatContact == nil) {
		return false
	}
	if a, b := e.RepeatContact, o.RepeatContact; a != nil {
		if !pyNumberEqual(a.WindowDays, b.WindowDays) || !pyNumberEqual(a.ReportWindowHours, b.ReportWindowHours) ||
			a.MinConversations != b.MinConversations || a.Effect != b.Effect {
			return false
		}
	}
	return optEqual(e.Version, o.Version) && optEqual(e.Status, o.Status)
}

func optEqual(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// pyLines is Python's read_text universal newlines followed by str.splitlines().
func pyLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	var lines []string
	start := 0
	for i, r := range text {
		switch r {
		case '\n', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
			lines = append(lines, text[start:i])
			start = i + utf8.RuneLen(r)
		}
	}
	if start < len(text) {
		lines = append(lines, text[start:])
	}
	return lines
}

func pyRstrip(s string) string { return strings.TrimRightFunc(s, isPySpace) }

func pyStrip(s string) string { return strings.TrimFunc(s, isPySpace) }

// rulesBlock is _rules_block: the text of the file's one rules block.
func rulesBlock(path string) (string, string, func(string) error, error) {
	source := pyPathString(path)
	fail := func(problem string) error { return &RulesError{source, problem} }
	b, err := os.ReadFile(source)
	if err != nil {
		return "", source, fail, fail(fmt.Sprintf("cannot read brand file (%v)", err))
	}
	if !utf8.Valid(b) {
		return "", source, fail, fail("cannot read brand file (invalid UTF-8)")
	}
	lines := pyLines(string(b))
	var blocks []string
	for i := 0; i < len(lines); {
		line := pyRstrip(lines[i])
		if !strings.HasPrefix(line, fenceOpen) {
			i++
			continue
		}
		if line != fenceOpen {
			return "", source, fail, fail(fmt.Sprintf("malformed rules fence on line %d; it must be exactly %s", i+1, fenceOpen))
		}
		end := i + 1
		for end < len(lines) && pyRstrip(lines[end]) != fenceClose {
			end++
		}
		if end == len(lines) {
			return "", source, fail, fail(fmt.Sprintf("unclosed ```rules block starting on line %d", i+1))
		}
		blocks = append(blocks, strings.Join(lines[i+1:end], "\n"))
		i = end + 1
	}
	switch {
	case len(blocks) == 0:
		return "", source, fail, fail("no ```rules block found")
	case len(blocks) > 1:
		return "", source, fail, fail(fmt.Sprintf("more than one ```rules block found (%d); exactly one is allowed", len(blocks)))
	}
	return blocks[0], source, fail, nil
}

// brandObject is _brand_object: the rules block's top-level JSON object.
func brandObject(path string) (Value, string, error) {
	block, source, fail, err := rulesBlock(path)
	if err != nil {
		return Value{}, source, err
	}
	if pyStrip(block) == "" {
		return Value{}, source, fail("the ```rules block is empty")
	}
	data, err := ParseJSON([]byte(block))
	if err != nil {
		return Value{}, source, fail(fmt.Sprintf("rules block is not valid JSON (line %d of the block: %v)", blockLine(block, err), err))
	}
	if data.Kind != Object {
		return Value{}, source, fail("rules block top level must be a JSON object")
	}
	return data, source, nil
}

// blockLine is the 1-based line, within the block, of the byte offset Go's
// decoder reports (the end of the block when it reports none).
func blockLine(block string, err error) int {
	off := len(block)
	var se *json.SyntaxError
	if errors.As(err, &se) && int(se.Offset) < off {
		off = int(se.Offset)
	}
	return 1 + strings.Count(block[:off], "\n")
}
