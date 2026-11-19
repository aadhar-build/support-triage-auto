package policy

import (
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync/atomic"
	"unicode/utf8"
)

// Violation is one policy hit. Start and End are code-point offsets into the
// draft, and Text is the draft's code points [Start, End).
type Violation struct {
	RuleID     string
	Start, End int
	Text       string
}

const curlyApostrophe = '’'

// compiled counts phrase patterns compiled, so a test can prove they are built
// once per ruleset.
var compiled atomic.Int64

// phrasePattern is Python's
//
//	(?<!\w) word0 \s+ word1 ... \s+ wordN (?!\w)   with re.IGNORECASE
//
// for phrase.replace("’", "'").split(" "), run by a small backtracking matcher
// over code points (Go regexp has no lookaround and other \s, \w and case rules).
type phrasePattern struct {
	words [][]charMatcher
}

func compilePhrases(phrases []string) []*phrasePattern {
	out := make([]*phrasePattern, len(phrases))
	for i, p := range phrases {
		out[i] = compilePhrase(p)
	}
	return out
}

func compilePhrase(phrase string) *phrasePattern {
	compiled.Add(1)
	phrase = strings.ReplaceAll(phrase, string(curlyApostrophe), "'")
	parts := strings.Split(phrase, " ")
	p := &phrasePattern{words: make([][]charMatcher, len(parts))}
	for i, w := range parts {
		for _, r := range w {
			p.words[i] = append(p.words[i], newCharMatcher(r))
		}
	}
	return p
}

// matchAt returns the end of a match starting at start, trying each \s+
// longest first, the way Python's backtracking does.
func (p *phrasePattern) matchAt(text []rune, start int) (int, bool) {
	if start > 0 && isPyWord(text[start-1]) {
		return 0, false
	}
	return p.matchWord(text, start, 0)
}

func (p *phrasePattern) matchWord(text []rune, pos, wi int) (int, bool) {
	for _, m := range p.words[wi] {
		if pos >= len(text) || !m.match(text[pos]) {
			return 0, false
		}
		pos++
	}
	if wi == len(p.words)-1 {
		if pos < len(text) && isPyWord(text[pos]) {
			return 0, false
		}
		return pos, true
	}
	run := 0
	for pos+run < len(text) && isPySpace(text[pos+run]) {
		run++
	}
	for k := run; k >= 1; k-- {
		if end, ok := p.matchWord(text, pos+k, wi+1); ok {
			return end, true
		}
	}
	return 0, false
}

// findAll is finditer: leftmost, non-overlapping, restarting at each match end.
// Every phrase is non-empty, so every match is too.
func (p *phrasePattern) findAll(text []rune) [][2]int {
	var spans [][2]int
	for start := 0; start <= len(text); {
		if end, ok := p.matchAt(text, start); ok && end > start {
			spans = append(spans, [2]int{start, end})
			start = end
			continue
		}
		start++
	}
	return spans
}

func (p *phrasePattern) search(text []rune) bool {
	for start := 0; start <= len(text); start++ {
		if _, ok := p.matchAt(text, start); ok {
			return true
		}
	}
	return false
}

func swapApostrophes(s string) []rune {
	r := []rune(s)
	for i, c := range r {
		if c == curlyApostrophe {
			r[i] = '\''
		}
	}
	return r
}

// phrasePatterns returns the compiled patterns for the rule's current
// Phrases. Patterns from RulesFromData are reused only while Phrases still
// equals what they were compiled from; otherwise (a hand-built or edited
// rule) Phrases are compiled now, as Python's check does on every call, so a
// rule never silently matches nothing.
func (r Rule) phrasePatterns() []*phrasePattern {
	if len(r.patterns) == len(r.Phrases) && slices.Equal(r.compiledFrom, r.Phrases) {
		return r.patterns
	}
	return compilePhrases(r.Phrases)
}

// phrasePatterns is Rule.phrasePatterns for a Topic: patterns from
// TopicsFromData are reused only while Phrases still equals what they were
// compiled from; a hand-built or edited topic is compiled now, so it never
// silently matches nothing.
func (t Topic) phrasePatterns() []*phrasePattern {
	if len(t.patterns) == len(t.Phrases) && slices.Equal(t.compiledFrom, t.Phrases) {
		return t.patterns
	}
	return compilePhrases(t.Phrases)
}

type hit struct {
	start, end int
	ruleID     string
}

// Check returns every policy violation in draft, sorted by (start, end,
// rule id). An empty rule list gives an empty, non-nil slice.
func Check(draft string, customerTurns []string, rules []Rule) []Violation {
	seen := map[hit]bool{}
	var hits []hit
	add := func(s [2]int, id string) {
		h := hit{s[0], s[1], id}
		if !seen[h] {
			seen[h] = true
			hits = append(hits, h)
		}
	}
	for _, rule := range rules {
		if rule.Type == "banned_phrase" {
			text := swapApostrophes(draft)
			for _, p := range rule.phrasePatterns() {
				for _, s := range p.findAll(text) {
					add(s, rule.ID)
				}
			}
			continue
		}
		for _, s := range piiSpans(draft, customerTurns, rule.Detect) {
			add(s, rule.ID)
		}
	}
	sort.Slice(hits, func(i, j int) bool {
		a, b := hits[i], hits[j]
		if a.start != b.start {
			return a.start < b.start
		}
		if a.end != b.end {
			return a.end < b.end
		}
		return a.ruleID < b.ruleID
	})
	runes := []rune(draft)
	out := make([]Violation, 0, len(hits))
	for _, h := range hits {
		out = append(out, Violation{h.ruleID, h.start, h.end, string(runes[h.start:h.end])})
	}
	return out
}

// IsSafeToSend is true only when Check finds no violation at all.
func IsSafeToSend(draft string, customerTurns []string, rules []Rule) bool {
	return len(Check(draft, customerTurns, rules)) == 0
}

// MatchedTopics returns the ids of the topics with a phrase hit in any
// customer turn, in topic order, once each.
func MatchedTopics(customerTurns []string, topics []Topic) []string {
	texts := make([][]rune, len(customerTurns))
	for i, t := range customerTurns {
		texts[i] = swapApostrophes(t)
	}
	out := []string{}
	for _, topic := range topics {
	search:
		for _, p := range topic.phrasePatterns() {
			for _, text := range texts {
				if p.search(text) {
					out = append(out, topic.ID)
					break search
				}
			}
		}
	}
	return out
}

var (
	emailRE   = regexp.MustCompile(`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`)
	cardRunRE = regexp.MustCompile(`[0-9](?:[ -]?[0-9])*`)
	phoneRE   = regexp.MustCompile(`\+?\(?[0-9](?:[ .()-]{0,2}[0-9])*`)
)

// runeOffsets maps each byte offset of s (and len(s)) to its code-point offset.
func runeOffsets(s string) []int {
	idx := make([]int, len(s)+1)
	n := 0
	for i := range s {
		for j := i; j < len(s) && (j == i || s[j]&0xC0 == 0x80); j++ {
			idx[j] = n
		}
		n++
	}
	idx[len(s)] = n
	return idx
}

// maskEmails replaces every email's bytes with "\x00". Emails are ASCII, so
// byte and code-point offsets both stay the same.
func maskEmails(s string) string {
	return emailRE.ReplaceAllStringFunc(s, func(m string) string { return strings.Repeat("\x00", len(m)) })
}

func has(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func piiSpans(draft string, customerTurns []string, detect []string) [][2]int {
	var spans [][2]int
	masked := maskEmails(draft)
	idx := runeOffsets(draft)
	if has(detect, "card_number") {
		for _, s := range cardSpans(masked) {
			spans = append(spans, [2]int{idx[s[0]], idx[s[1]]})
		}
	}
	if has(detect, "phone_from_customer") {
		posted := map[string]bool{}
		for _, turn := range customerTurns {
			for _, ph := range phones(maskEmails(turn)) {
				posted[ph.key] = true
			}
		}
		for _, ph := range phones(masked) {
			if posted[ph.key] {
				spans = append(spans, [2]int{idx[ph.span[0]], idx[ph.span[1]]})
			}
		}
	}
	if has(detect, "email_from_customer") {
		posted := map[string]bool{}
		for _, turn := range customerTurns {
			for _, m := range emailRE.FindAllString(turn, -1) {
				posted[strings.ToLower(m)] = true
			}
		}
		for _, loc := range emailRE.FindAllStringIndex(draft, -1) {
			if posted[strings.ToLower(draft[loc[0]:loc[1]])] {
				spans = append(spans, [2]int{idx[loc[0]], idx[loc[1]]})
			}
		}
	}
	return spans
}

// cardSpans returns byte spans of Luhn-valid 13-19 digit runs that no
// Python-alphanumeric code point touches.
func cardSpans(text string) [][2]int {
	var spans [][2]int
	for _, loc := range cardRunRE.FindAllStringIndex(text, -1) {
		start, end := loc[0], loc[1]
		if start > 0 {
			if r, _ := utf8.DecodeLastRuneInString(text[:start]); isPyAlnum(r) {
				continue
			}
		}
		if end < len(text) {
			if r, _ := utf8.DecodeRuneInString(text[end:]); isPyAlnum(r) {
				continue
			}
		}
		var digits []int
		for _, c := range text[start:end] {
			if c >= '0' && c <= '9' {
				digits = append(digits, int(c-'0'))
			}
		}
		if len(digits) >= 13 && len(digits) <= 19 && luhnOK(digits) {
			spans = append(spans, [2]int{start, end})
		}
	}
	return spans
}

type phone struct {
	span [2]int
	key  string
}

func phones(text string) []phone {
	var out []phone
	for _, loc := range phoneRE.FindAllStringIndex(text, -1) {
		var key strings.Builder
		for _, c := range text[loc[0]:loc[1]] {
			if c >= '0' && c <= '9' {
				key.WriteRune(c)
			}
		}
		if n := key.Len(); n >= 7 && n <= 15 {
			out = append(out, phone{[2]int{loc[0], loc[1]}, key.String()})
		}
	}
	return out
}

func luhnOK(digits []int) bool {
	total := 0
	for i := 0; i < len(digits); i++ {
		d := digits[len(digits)-1-i]
		if i%2 == 1 {
			d *= 2
			if d > 9 {
				d -= 9
			}
		}
		total += d
	}
	return total%10 == 0
}
