// Context signals: the Go port of src/triage/context_signals.py. It loads the
// owner's repeat_contact rule (failing closed, no defaults), builds a
// ContactRecord per real thread, counts a customer's recent contacts with a
// brand (ContactCounts) and says which brand a thread belongs to
// (BrandAttribution). Customer ids stay in memory; only counts leave.

package policy

import (
	"fmt"
	"math"
	"os"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
)

// RepeatContactError is any repeat_contact loader or validator failure. Its
// text is Python's RepeatContactError text.
type RepeatContactError struct{ Source, Problem string }

func (e *RepeatContactError) Error() string {
	return "repeat_contact rule in " + e.Source + ": " + e.Problem
}

// PyNumber is a JSON number as Python's json.loads parsed it.
type PyNumber struct {
	Lit   string  // the JSON literal
	Float float64 // its float value (Python float(int) for ints)
	IsInt bool    // Python parsed it as an int
}

// String is Python's str() of the parsed value: 7 -> "7", 7.0 -> "7.0".
func (n PyNumber) String() string { return reprNumber(Value{Kind: Number, Num: n.Lit}) }

// RepeatContactRule is the validated repeat_contact block.
type RepeatContactRule struct {
	WindowDays, ReportWindowHours PyNumber
	MinConversations              int
	Effect                        string
}

// LoadRepeatContactRule reads the rules file's top-level repeat_contact block.
// Errors are *RepeatContactError.
func LoadRepeatContactRule(path string) (RepeatContactRule, error) {
	source := pyPathString(path)
	b, err := os.ReadFile(source)
	if err != nil {
		return RepeatContactRule{}, &RepeatContactError{source, fmt.Sprintf("cannot read file (%v)", err)}
	}
	if !utf8.Valid(b) {
		return RepeatContactRule{}, &RepeatContactError{source, "not valid JSON"}
	}
	data, err := ParseJSON(b)
	if err != nil {
		return RepeatContactRule{}, &RepeatContactError{source, "not valid JSON"}
	}
	return RepeatContactFromData(data, source)
}

// RepeatContactFromData validates the repeat_contact block of an already
// parsed rules object, with Python's checks in Python's order. source is used
// verbatim in the message.
func RepeatContactFromData(data Value, source string) (RepeatContactRule, error) {
	fail := func(problem string) (RepeatContactRule, error) {
		return RepeatContactRule{}, &RepeatContactError{source, problem}
	}
	block, ok := data.Get("repeat_contact")
	if !ok || block.Kind != Object {
		return fail("'repeat_contact' is missing or is not an object")
	}
	var r RepeatContactRule
	for _, key := range []string{"window_days", "report_window_hours"} {
		v, ok := block.Get(key)
		if !ok {
			return fail("'" + key + "' is missing")
		}
		n, ok := positiveNumber(v)
		if !ok {
			return fail("'" + key + "' must be a finite number above 0")
		}
		if key == "window_days" {
			r.WindowDays = n
		} else {
			r.ReportWindowHours = n
		}
	}
	v, ok := block.Get("min_conversations")
	if !ok {
		return fail("'min_conversations' is missing")
	}
	n, err := strconv.Atoi(v.Num)
	if !v.IsInt() || err != nil || n < 2 {
		return fail("'min_conversations' must be an int of at least 2")
	}
	r.MinConversations = n
	v, ok = block.Get("effect")
	if !ok {
		return fail("'effect' is missing")
	}
	if v.Kind != String || v.Str != "escalate" {
		return fail(`'effect' must be "escalate"`)
	}
	r.Effect = "escalate"
	return r, nil
}

// positiveNumber is _positive_number: a finite JSON number above 0 (bools
// are refused by kind). An int too large for a float is refused, where
// Python raises OverflowError.
func positiveNumber(v Value) (PyNumber, bool) {
	if v.Kind != Number {
		return PyNumber{}, false
	}
	f, err := strconv.ParseFloat(v.Num, 64)
	if (err != nil && !math.IsInf(f, 0)) || math.IsInf(f, 0) || math.IsNaN(f) || !(f > 0) {
		return PyNumber{}, false
	}
	return PyNumber{Lit: v.Num, Float: f, IsInt: v.IsInt()}, true
}

var mentionRE = regexp.MustCompile(`@([A-Za-z0-9_]+)`)

// MentionHandles returns every @handle (ASCII [A-Za-z0-9_]+) in text, in
// order, as Python's _MENTION_RE.findall.
func MentionHandles(text string) []string {
	var out []string
	for _, m := range mentionRE.FindAllStringSubmatch(text, -1) {
		out = append(out, m[1])
	}
	return out
}

// BrandReply is one known-brand reply and its UTC time.
type BrandReply struct {
	Brand string
	At    time.Time
}

// ContactRecord is what was known about one thread when it started. Records
// may be built by hand. BrandsAtStart is a set, kept sorted without duplicates.
type ContactRecord struct {
	RootID        uint64
	Customer      string
	Start         time.Time
	BrandsAtStart []string
	BrandReplies  []BrandReply
}

// canonicalBrands maps pyLower(b) to b; when known brands collide, the
// smallest spelling in byte order wins.
func canonicalBrands(known []string) map[string]string {
	byLower := map[string]string{}
	for _, b := range known {
		k := pyLower(b)
		if cur, ok := byLower[k]; !ok || b < cur {
			byLower[k] = b
		}
	}
	return byLower
}

// ContactRecordOf is contact_record: ok is false when the thread has no
// turns, its first turn is outbound, or it has neither a known-brand mention
// in the first turn nor a known-brand reply.
func ContactRecordOf(th ingest.Thread, knownBrands []string) (ContactRecord, bool) {
	if len(th.Turns) == 0 || !th.Turns[0].Inbound {
		return ContactRecord{}, false
	}
	first := th.Turns[0]
	byLower := canonicalBrands(knownBrands)
	mentioned := []string{}
	for _, h := range MentionHandles(first.Text) {
		if b, ok := byLower[pyLower(h)]; ok && !slices.Contains(mentioned, b) {
			mentioned = append(mentioned, b)
		}
	}
	slices.Sort(mentioned)
	canonical := map[string]bool{}
	for _, b := range byLower {
		canonical[b] = true
	}
	replies := []BrandReply{}
	for _, t := range th.Turns {
		if !t.Inbound && canonical[t.AuthorID] {
			replies = append(replies, BrandReply{t.AuthorID, t.CreatedAt.UTC()})
		}
	}
	if len(mentioned) == 0 && len(replies) == 0 {
		return ContactRecord{}, false
	}
	return ContactRecord{RootID: th.RootID, Customer: first.AuthorID, Start: first.CreatedAt.UTC(),
		BrandsAtStart: mentioned, BrandReplies: replies}, true
}

// ContactCounts is contact_counts: for each record's root id, the largest
// count over its brands at start of the same customer's conversations with
// that brand started within window before it, up to and including it. A brand
// counts for an earlier record y when y mentioned it at start or y has its
// reply at or before this record's start. window must be at least 0.
func ContactCounts(records []ContactRecord, window time.Duration) map[uint64]int {
	if window < 0 {
		panic("policy.ContactCounts: negative window")
	}
	sorted := slices.Clone(records)
	sort.SliceStable(sorted, func(i, j int) bool {
		a, b := sorted[i], sorted[j]
		if a.Customer != b.Customer {
			return a.Customer < b.Customer
		}
		if !a.Start.Equal(b.Start) {
			return a.Start.Before(b.Start)
		}
		return a.RootID < b.RootID
	})
	result := map[uint64]int{}
	for g := 0; g < len(sorted); {
		end := g
		for end < len(sorted) && sorted[end].Customer == sorted[g].Customer {
			end++
		}
		lo := g
		for i := g; i < end; i++ {
			x := sorted[i]
			for sorted[lo].Start.Before(x.Start.Add(-window)) {
				lo++
			}
			result[x.RootID] = countFor(x, sorted[lo:i+1])
		}
		g = end
	}
	return result
}

func countFor(x ContactRecord, earlier []ContactRecord) int {
	best := 0
	for _, b := range x.BrandsAtStart {
		n := 0
		for _, y := range earlier {
			if slices.Contains(y.BrandsAtStart, b) || repliedBy(y, b, x.Start) {
				n++
			}
		}
		best = max(best, n)
	}
	return best
}

func repliedBy(y ContactRecord, brand string, by time.Time) bool {
	for _, r := range y.BrandReplies {
		if r.Brand == brand && !r.At.After(by) {
			return true
		}
	}
	return false
}

// BrandAttribution is brand_attribution: "brand", "other_brand" or
// "unattributed" for th at its decision moment, the UTC time of its last
// inbound turn in turn order. Replies after the moment are never used.
func BrandAttribution(th ingest.Thread, brand string, knownBrands []string) string {
	last := -1
	for i, t := range th.Turns {
		if t.Inbound {
			last = i
		}
	}
	if last < 0 {
		return "unattributed"
	}
	r, ok := ContactRecordOf(th, knownBrands)
	if !ok {
		return "unattributed"
	}
	moment := th.Turns[last].CreatedAt
	handles := map[string]bool{}
	for _, b := range knownBrands {
		if pyLower(b) == pyLower(brand) {
			handles[b] = true
		}
	}
	byThen := slices.Clone(r.BrandsAtStart)
	for _, rp := range r.BrandReplies {
		if !rp.At.After(moment) {
			byThen = append(byThen, rp.Brand)
		}
	}
	other := false
	for _, b := range byThen {
		if handles[b] {
			return "brand"
		}
		other = true
	}
	if other {
		return "other_brand"
	}
	return "unattributed"
}
