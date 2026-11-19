package route

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"iter"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
)

// TraceSchema is the trace record schema version.
const TraceSchema = 2

// gitTimeout bounds each git call; a var so tests can shorten it.
var gitTimeout = 10 * time.Second

var (
	commitRE   = regexp.MustCompile(`^[0-9a-f]{40}$`)
	topicIDRE  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)
	recordKeys = []string{"trace_schema", "thread_id", "code_version", "code_dirty", "rules_version",
		"drift_config_version", "threshold", "split", "seed", "tiers", "final", "reason_codes", "topic_id"}
	nullableKeys = []string{"rules_version", "drift_config_version"}
	labelKeys    = []string{"intent", "urgency", "action", "confidence"}
	tierKeys     = append([]string{"tier", "model_id"}, labelKeys...)
)

// TraceError is a trace that could not be built, validated or written. The
// message is one line.
type TraceError struct{ Msg string }

func (e *TraceError) Error() string { return e.Msg }

func traceErr(format string, a ...any) error {
	return &TraceError{Msg: strings.ReplaceAll(fmt.Sprintf(format, a...), "\n", " ")}
}

// RunInfo is what produced a run's decisions.
type RunInfo struct {
	CodeVersion                      string
	CodeDirty                        bool
	RulesVersion, DriftConfigVersion *string
	Threshold                        float64
	Split                            string
	Seed                             int64
}

// Record is one audit record. Values are nil, bool, string, int/int64/uint64,
// float64, []any, []string or map[string]any.
type Record = map[string]any

func git(repoDir string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), gitTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", repoDir}, args...)...)
	cmd.WaitDelay = time.Second
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if errors.Is(err, exec.ErrNotFound) {
		return "", traceErr("git is not installed; cannot read the code version")
	}
	if ctx.Err() == context.DeadlineExceeded {
		return "", traceErr("git %s timed out in %s", args[0], repoDir)
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		lines := strings.Split(strings.TrimSpace(stderr.String()), "\n")
		first := strings.TrimSpace(lines[0])
		if first == "" {
			first = fmt.Sprintf("exit %d", exitErr.ExitCode())
		}
		return "", traceErr("git %s failed in %s: %s", args[0], repoDir, first)
	}
	if err != nil {
		return "", traceErr("cannot run git in %s: %s", repoDir, errDetail(err))
	}
	return stdout.String(), nil
}

// errDetail is the OS detail of an error (Python's e.strerror), Go's wording.
func errDetail(err error) string {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return errno.Error()
	}
	return err.Error()
}

// CodeVersion returns (commit, dirty) for repoDir from git.
func CodeVersion(repoDir string) (commit string, dirty bool, err error) {
	out, err := git(repoDir, "rev-parse", "HEAD")
	if err != nil {
		return "", false, err
	}
	commit = strings.TrimSpace(out)
	if !commitRE.MatchString(commit) {
		return "", false, traceErr("git rev-parse HEAD in %s did not return a 40-hex commit", repoDir)
	}
	status, err := git(repoDir, "status", "--porcelain", "--untracked-files=no")
	if err != nil {
		return "", false, err
	}
	return commit, strings.TrimSpace(status) != "", nil
}

func labelsMap(l Labels) map[string]any {
	return map[string]any{"intent": l.Intent, "urgency": l.Urgency, "action": l.Action, "confidence": l.Confidence}
}

func strOrNil(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

// BuildRecord is trace.build_record: ids and labels only, never text.
func BuildRecord(r RoutedThread, run RunInfo) Record {
	tiers := make([]any, len(r.Steps))
	for i, s := range r.Steps {
		m := labelsMap(s.Labels)
		m["tier"], m["model_id"] = s.Tier, s.ModelID
		tiers[i] = m
	}
	codes := append([]string{}, r.ReasonCodes...)
	topicID := append([]string{}, r.TopicID...)
	return Record{
		"trace_schema":         TraceSchema,
		"thread_id":            r.ThreadID,
		"code_version":         run.CodeVersion,
		"code_dirty":           run.CodeDirty,
		"rules_version":        strOrNil(run.RulesVersion),
		"drift_config_version": strOrNil(run.DriftConfigVersion),
		"threshold":            run.Threshold,
		"split":                run.Split,
		"seed":                 run.Seed,
		"tiers":                tiers,
		"final":                labelsMap(r.Labels),
		"reason_codes":         codes,
		"topic_id":             topicID,
	}
}

// asList returns a list value as []any; ok is false for a non-list.
func asList(v any) ([]any, bool) {
	switch x := v.(type) {
	case []any:
		return x, true
	case []string:
		out := make([]any, len(x))
		for i, s := range x {
			out[i] = s
		}
		return out, true
	}
	return nil, false
}

func isSchema(v any) bool {
	switch x := v.(type) {
	case int:
		return x == TraceSchema
	case int64:
		return x == TraceSchema
	case uint64:
		return x == TraceSchema
	case float64:
		return x == TraceSchema
	}
	return false
}

// ValidateRecord is trace.validate_record: same checks, order and messages.
func ValidateRecord(rec any) error {
	m, ok := rec.(map[string]any)
	if !ok {
		return traceErr("trace record is not an object")
	}
	where := "trace record for thread " + pyStr(m["thread_id"])
	fail := func(key, problem string) error {
		return traceErr("%s: %s %s", where, reprStr(key), problem)
	}
	for _, key := range recordKeys {
		v, ok := m[key]
		if !ok {
			return fail(key, "is missing")
		}
		if v == nil && !slices.Contains(nullableKeys, key) {
			return fail(key, "is null")
		}
	}
	if !isSchema(m["trace_schema"]) {
		return fail("trace_schema", fmt.Sprintf("is not %d", TraceSchema))
	}
	tiers, ok := asList(m["tiers"])
	if !ok || len(tiers) == 0 {
		return fail("tiers", "is not a non-empty list")
	}
	for i, tier := range tiers {
		tm, ok := tier.(map[string]any)
		if !ok {
			return fail("tiers", fmt.Sprintf("entry %d is not an object", i))
		}
		for _, key := range tierKeys {
			if tm[key] == nil {
				return fail(key, fmt.Sprintf("is missing or null in tier entry %d", i))
			}
		}
	}
	final, ok := m["final"].(map[string]any)
	if !ok {
		return fail("final", "is not an object")
	}
	for _, key := range labelKeys {
		if final[key] == nil {
			return fail(key, "is missing or null in 'final'")
		}
	}
	codes, ok := asList(m["reason_codes"])
	if !ok || len(codes) == 0 {
		return fail("reason_codes", "is not a non-empty list")
	}
	hasAlways := false
	for _, c := range codes {
		if !IsKnownReasonCode(c, nil) {
			return fail("reason_codes", "holds an unregistered code "+pyRepr(c))
		}
		hasAlways = hasAlways || c == EscalateAlways
	}
	topicID, ok := asList(m["topic_id"])
	for _, tid := range topicID {
		s, isStr := tid.(string)
		ok = ok && isStr && topicIDRE.MatchString(s)
	}
	if !ok {
		return fail("topic_id", "is not a list of kebab-case topic ids")
	}
	if hasAlways && len(topicID) == 0 {
		return fail("topic_id", "is empty but reason_codes holds "+reprStr(EscalateAlways))
	}
	if len(topicID) > 0 && !hasAlways {
		return fail("topic_id", "is not empty but reason_codes lacks "+reprStr(EscalateAlways))
	}
	return nil
}

// resolvePython is Python's non-strict Path.resolve(): absolute against the
// working directory, each existing symlink resolved before a following `..`,
// missing parts appended unchanged. A symlink loop is an error, as Python's
// resolve raises on one.
func resolvePython(path string) (string, error) {
	if !filepath.IsAbs(path) {
		wd, err := os.Getwd()
		if err != nil {
			return "", err
		}
		path = wd + "/" + path
	}
	// seen maps a link path to its resolution; "" while it is being resolved.
	return joinReal("/", path, map[string]string{})
}

func joinReal(base, rest string, seen map[string]string) (string, error) {
	if strings.HasPrefix(rest, "/") {
		base = "/"
	}
	for _, name := range strings.Split(rest, "/") {
		switch name {
		case "", ".":
			continue
		case "..":
			base = filepath.Dir(base)
			continue
		}
		next := filepath.Join(base, name)
		fi, err := os.Lstat(next)
		if err != nil || fi.Mode()&os.ModeSymlink == 0 {
			base = next
			continue
		}
		if done, ok := seen[next]; ok {
			if done == "" {
				return "", errors.New("symlink loop at " + next)
			}
			base = done
			continue
		}
		target, err := os.Readlink(next)
		if err != nil {
			base = next
			continue
		}
		seen[next] = ""
		if base, err = joinReal(base, target, seen); err != nil {
			return "", err
		}
		seen[next] = base
	}
	return base, nil
}

func checkOutPath(repoRoot, path string) (string, error) {
	resolved, err := resolvePython(path)
	if err != nil {
		return "", traceErr("cannot resolve trace path %s: %s", path, err)
	}
	root, err := resolvePython(repoRoot)
	if err != nil {
		return "", traceErr("cannot resolve repository %s: %s", repoRoot, err)
	}
	if resolved == root || strings.HasPrefix(resolved, strings.TrimSuffix(root, "/")+"/") {
		return "", traceErr("trace path %s is inside the repository %s; write it outside", resolved, root)
	}
	return resolved, nil
}

// CheckTracePath refuses a path inside repoRoot. Opens nothing.
func CheckTracePath(repoRoot, path string) error {
	_, err := checkOutPath(repoRoot, path)
	return err
}

// WriteTrace validates records and writes them as JSON lines, atomically, to
// path outside repoRoot. It returns the record count.
func WriteTrace(repoRoot, path string, records iter.Seq[Record]) (int, error) {
	dest, err := checkOutPath(repoRoot, path)
	if err != nil {
		return 0, err
	}
	dir := filepath.Dir(dest)
	f, err := os.CreateTemp(dir, "."+filepath.Base(dest)+".*.tmp")
	if err != nil {
		return 0, traceErr("cannot write trace in %s: %s", dir, errDetail(err))
	}
	tmp := f.Name()
	count, err := writeRecords(f, records)
	if err == nil {
		err = os.Rename(tmp, dest)
	}
	if err != nil {
		os.Remove(tmp)
		var te *TraceError
		if errors.As(err, &te) {
			return 0, te
		}
		return 0, traceErr("cannot write trace %s: %s", dest, errDetail(err))
	}
	return count, nil
}

func writeRecords(f *os.File, records iter.Seq[Record]) (int, error) {
	w := bufio.NewWriter(f)
	count := 0
	var err error
	for rec := range records {
		if err = ValidateRecord(rec); err != nil {
			break
		}
		var line string
		if line, err = pyjson(rec); err != nil {
			break
		}
		if _, err = w.WriteString(line + "\n"); err != nil {
			break
		}
		count++
	}
	if err == nil {
		err = w.Flush()
	}
	if err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return count, err
}

// pyjson is json.dumps(v, sort_keys=True): ", " and ": " separators, keys in
// code-point order, ensure_ascii, Python float repr. NaN and Inf are refused.
func pyjson(v any) (string, error) {
	var b strings.Builder
	if err := writeJSON(&b, v); err != nil {
		return "", err
	}
	return b.String(), nil
}

func writeJSON(b *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint64:
		b.WriteString(strconv.FormatUint(x, 10))
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return traceErr("cannot encode %s in a trace", reprFloat(x))
		}
		b.WriteString(reprFloat(x))
	case string:
		writeJSONString(b, x)
	case []string:
		b.WriteByte('[')
		for i, s := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			writeJSONString(b, s)
		}
		b.WriteByte(']')
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			if err := writeJSON(b, item); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case map[string]any:
		b.WriteByte('{')
		for i, k := range sortedKeys(x) {
			if i > 0 {
				b.WriteString(", ")
			}
			writeJSONString(b, k)
			b.WriteString(": ")
			if err := writeJSON(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	default:
		return traceErr("cannot encode a %T in a trace", v)
	}
	return nil
}

func writeJSONString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r >= 0x20 && r < 0x7f:
			b.WriteRune(r)
		case r >= 0x10000:
			hi, lo := utf16.EncodeRune(r)
			fmt.Fprintf(b, `\u%04x\u%04x`, hi, lo)
		default:
			fmt.Fprintf(b, `\u%04x`, r)
		}
	}
	b.WriteByte('"')
}
