package admin

import (
	"bytes"
	"encoding/json"
	"io"
	"math"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/report"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// traceMaxBytes caps how much of a trace file is read; a var so tests can
// lower it.
var traceMaxBytes int64 = report.MaxFileBytes

// NoTrace is the reason given when a synthetic run has no usable trace.
const NoTrace = "no usable trace file"

const traceFile = "trace.jsonl"

var labelTiers = []string{"cheap", "larger", "agent"}

// thread is one trace record, reduced to what the console may serve.
type thread struct {
	id     uint64
	codes  []string
	topics []string
	final  map[string]any
	tiers  []any
}

// parseThreadID accepts only a canonical decimal uint64: digits, no sign, no
// leading zero unless the id is 0.
func parseThreadID(s string) (uint64, bool) {
	if s == "" || (len(s) > 1 && s[0] == '0') {
		return 0, false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return 0, false
		}
	}
	n, err := strconv.ParseUint(s, 10, 64)
	return n, err == nil
}

// readTraceBytes opens <dir>/trace.jsonl without following a link at the run
// folder or the file, refuses anything but a regular file, and reads at most
// traceMaxBytes from that same handle.
func readTraceBytes(dir string) ([]byte, bool) {
	f, err := openTraceFile(dir)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return nil, false
	}
	b, err := io.ReadAll(io.LimitReader(f, traceMaxBytes+1))
	if err != nil || int64(len(b)) > traceMaxBytes {
		return nil, false
	}
	return b, true
}

// normalise turns json.Number values into int64 or float64. ok is false for
// a number neither can hold.
func normalise(v any) (any, bool) {
	switch x := v.(type) {
	case json.Number:
		s := string(x)
		if !strings.ContainsAny(s, ".eE") {
			n, err := strconv.ParseInt(s, 10, 64)
			return n, err == nil
		}
		f, err := strconv.ParseFloat(s, 64)
		return f, err == nil && !math.IsInf(f, 0)
	case []any:
		for i, item := range x {
			var ok bool
			if x[i], ok = normalise(item); !ok {
				return nil, false
			}
		}
	case map[string]any:
		for k, item := range x {
			var ok bool
			if x[k], ok = normalise(item); !ok {
				return nil, false
			}
		}
	}
	return v, true
}

// decodeLine decodes one trace line into a record whose thread_id is a uint64.
func decodeLine(line []byte) (map[string]any, bool) {
	dec := json.NewDecoder(bytes.NewReader(line))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, false
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, false
	}
	rec, isMap := v.(map[string]any)
	if !isMap {
		return nil, false
	}
	num, isNum := rec["thread_id"].(json.Number)
	if !isNum {
		return nil, false
	}
	id, ok := parseThreadID(string(num))
	if !ok {
		return nil, false
	}
	delete(rec, "thread_id")
	if _, ok := normalise(rec); !ok {
		return nil, false
	}
	rec["thread_id"] = id
	return rec, true
}

func stringIn(v any, vocab []string) bool {
	s, ok := v.(string)
	return ok && slices.Contains(vocab, s)
}

func stringList(v any) ([]string, bool) {
	list, ok := v.([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, len(list))
	for i, item := range list {
		if out[i], ok = item.(string); !ok {
			return nil, false
		}
	}
	return out, true
}

func finiteUnit(v any) bool {
	var f float64
	switch x := v.(type) {
	case int64:
		f = float64(x)
	case float64:
		f = x
	default:
		return false
	}
	return !math.IsNaN(f) && !math.IsInf(f, 0) && f >= 0 && f <= 1
}

// checkRecord applies the console's narrow checks after ValidateRecord.
func checkRecord(rec map[string]any) (thread, bool) {
	if route.ValidateRecord(rec) != nil || rec["split"] != "synthetic" {
		return thread{}, false
	}
	final, _ := rec["final"].(map[string]any)
	if !stringIn(final["intent"], route.Intents) || !stringIn(final["urgency"], route.Urgencies) ||
		!stringIn(final["action"], route.Actions) || !finiteUnit(final["confidence"]) {
		return thread{}, false
	}
	codes, ok := stringList(rec["reason_codes"])
	if !ok {
		return thread{}, false
	}
	topics, ok := stringList(rec["topic_id"])
	if !ok {
		return thread{}, false
	}
	tiers, _ := rec["tiers"].([]any)
	return thread{id: rec["thread_id"].(uint64), codes: codes, topics: topics, final: final, tiers: tiers}, true
}

// readTrace returns the run's threads in file order; ok is false when the
// trace is unusable.
func readTrace(dir string) ([]thread, bool) {
	b, ok := readTraceBytes(dir)
	if !ok || !utf8.Valid(b) {
		return nil, false
	}
	out := []thread{}
	if len(b) == 0 {
		return out, true
	}
	seen := map[uint64]bool{}
	for _, line := range bytes.Split(bytes.TrimSuffix(b, []byte("\n")), []byte("\n")) {
		rec, ok := decodeLine(line)
		if !ok {
			return nil, false
		}
		th, ok := checkRecord(rec)
		if !ok || seen[th.id] {
			return nil, false
		}
		seen[th.id] = true
		out = append(out, th)
	}
	return out, true
}

type step struct{ tier, modelID, action string }

func steps(tiers []any) ([]step, bool) {
	out := make([]step, len(tiers))
	for i, t := range tiers {
		m, _ := t.(map[string]any)
		tier, _ := m["tier"].(string)
		model, _ := m["model_id"].(string)
		action, _ := m["action"].(string)
		if !slices.Contains(labelTiers, tier) || model == "" || !slices.Contains(route.Actions, action) {
			return nil, false
		}
		out[i] = step{tier, model, action}
	}
	return out, true
}

func tierNames(ss []step) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = s.tier
	}
	return out
}

func source(name string, modelID any) map[string]any {
	return map[string]any{"source": name, "model_id": modelID}
}

// decidedBy names the tier or policy that decided, following router.go. It
// returns nil when the trace evidence does not fit one of the router's paths.
func decidedBy(th thread) any {
	ss, ok := steps(th.tiers)
	if !ok {
		return nil
	}
	names := tierNames(ss)
	action, _ := th.final["action"].(string)
	urgency, _ := th.final["urgency"].(string)
	codes := th.codes
	if slices.Contains(codes, route.EscalateAlways) {
		if (slices.Equal(codes, []string{route.EscalateAlways}) || slices.Equal(codes, []string{route.EscalateAlways, route.RepeatContact})) &&
			slices.Equal(names, []string{"cheap", "agent"}) && action == "escalate" && len(th.topics) > 0 {
			return source(route.EscalateAlways, nil)
		}
		return nil
	}
	c := codes[0]
	if !slices.Contains(route.CascadeCodes, c) {
		return nil
	}
	want := []string{"cheap", "larger"}
	if c == route.CheapConfident {
		want = want[:1]
	}
	if len(ss) < len(want) || !slices.Equal(names[:len(want)], want) {
		return nil
	}
	last := ss[len(want)-1]
	switch {
	case slices.Equal(codes, []string{c, route.RepeatContact}):
		if len(ss) != len(want) || action != "escalate" || urgency != "high" {
			return nil
		}
		if last.action != "escalate" {
			return source(route.RepeatContact, nil)
		}
		return source(last.tier, last.modelID)
	case len(codes) == 1:
		if slices.Contains(route.ActionableActions, last.action) {
			if len(ss) != len(want)+1 || ss[len(want)].tier != "agent" || action != ss[len(want)].action {
				return nil
			}
			return source("agent", ss[len(want)].modelID)
		}
		if len(ss) != len(want) || action != last.action {
			return nil
		}
		return source(last.tier, last.modelID)
	}
	return nil
}

// syntheticRun finds the ok, synthetic run named by the escaped segment.
func (h handler) syntheticRun(seg string) (Run, error) {
	name, err := url.PathUnescape(seg)
	if err != nil {
		return Run{}, errNotFound
	}
	runs, err := ListRuns(h.cfg.Runs)
	if err != nil {
		return Run{}, err
	}
	for _, r := range runs {
		if r.Name == name && r.Status == StatusOK && r.Kind == "synthetic" {
			return r, nil
		}
	}
	return Run{}, errNotFound
}

func (h handler) apiThreads(seg string) (response, error) {
	r, err := h.syntheticRun(seg)
	if err != nil {
		return response{}, err
	}
	threads, ok := readTrace(r.Dir)
	if !ok {
		return jsonResponse(map[string]any{"name": r.Name, "status": StatusOK, "threads": nil, "reason": NoTrace})
	}
	out := make([]any, len(threads))
	for i, th := range threads {
		out[i] = map[string]any{
			"thread_id":    strconv.FormatUint(th.id, 10),
			"action":       th.final["action"],
			"reason_codes": th.codes,
		}
	}
	return jsonResponse(map[string]any{"name": r.Name, "status": StatusOK, "threads": out})
}

func (h handler) apiThread(seg, idSeg string) (response, error) {
	id, ok := parseThreadID(idSeg)
	if !ok {
		return response{}, errNotFound
	}
	r, err := h.syntheticRun(seg)
	if err != nil {
		return response{}, err
	}
	threads, ok := readTrace(r.Dir)
	if !ok {
		return response{}, errNotFound
	}
	for _, th := range threads {
		if th.id != id {
			continue
		}
		final := map[string]any{}
		for _, k := range []string{"intent", "urgency", "action", "confidence"} {
			final[k] = th.final[k]
		}
		return jsonResponse(map[string]any{
			"name":         r.Name,
			"thread_id":    strconv.FormatUint(th.id, 10),
			"reason_codes": th.codes,
			"topic_id":     th.topics,
			"final":        final,
			"decided_by":   decidedBy(th),
		})
	}
	return response{}, errNotFound
}

// threadsPath splits "<run>/threads" or "<run>/threads/<id>" (the part after
// a /api/runs/ or /runs/ prefix). id is "" for the list.
func threadsPath(rest string) (run, id string, ok bool) {
	parts := strings.Split(rest, "/")
	if len(parts) < 2 || len(parts) > 3 || parts[0] == "" || parts[1] != "threads" {
		return "", "", false
	}
	if len(parts) == 3 {
		return parts[0], parts[2], parts[2] != ""
	}
	return parts[0], "", true
}
