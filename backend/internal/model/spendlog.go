package model

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"math/big"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// SpendLogError: the spend log or the thresholds file is missing, corrupt or refused.
type SpendLogError struct{ Msg string }

func (e *SpendLogError) Error() string { return e.Msg }

// MonthlyCapError: a reservation would take month-to-date spend above the cap.
// It unwraps to its *SpendLogError, as the Python subclass is a SpendLogError.
type MonthlyCapError struct{ Base *SpendLogError }

func (e *MonthlyCapError) Error() string { return e.Base.Msg }
func (e *MonthlyCapError) Unwrap() error { return e.Base }

func slErr(format string, a ...any) error { return &SpendLogError{fmt.Sprintf(format, a...)} }

// LoadMonthlyCap reads guardrails.api_spend_usd_per_month.max.
func LoadMonthlyCap(path string) (float64, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, slErr("thresholds file %s not found", path)
	} else if err != nil {
		return 0, slErr("cannot read thresholds file %s: %s", path, osDetail(err))
	}
	data, err := policy.ParseJSON(raw)
	if err != nil || !utf8.Valid(raw) {
		return 0, slErr("thresholds file %s is not valid JSON", path)
	}
	v := data
	for _, k := range []string{"guardrails", "api_spend_usd_per_month", "max"} {
		var ok bool
		if v, ok = v.Get(k); !ok {
			return 0, slErr("thresholds file %s lacks guardrails.api_spend_usd_per_month.max", path)
		}
	}
	f, ok := price(v)
	if !ok {
		return 0, slErr("thresholds file %s: api_spend_usd_per_month.max must be a finite number > 0", path)
	}
	return f, nil
}

// SpendLogOptions injects the clock and the id source. Nil means time.Now and
// a random UUID v4 as 32 lowercase hex characters.
type SpendLogOptions struct {
	Clock func() time.Time
	NewID func() string
}

func (o SpendLogOptions) withDefaults() SpendLogOptions {
	if o.Clock == nil {
		o.Clock = time.Now
	}
	if o.NewID == nil {
		o.NewID = uuid4Hex
	}
	return o
}

func uuid4Hex() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // crypto/rand never fails on Linux
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return hex.EncodeToString(b[:])
}

// fsync is the one internal seam: tests count appends' fsyncs through it.
var fsync = func(f *os.File) error { return f.Sync() }

func checkPath(repoRoot, path string) error {
	if route.CheckTracePath(repoRoot, path) == nil {
		return nil
	}
	root, err := route.ResolvePath(repoRoot)
	if err != nil {
		return slErr("cannot resolve repository %s: %s", repoRoot, err)
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	return slErr("spend log path %s is inside the repository %s; keep it outside", abs, root)
}

// isoformat is Python's datetime.astimezone(utc).isoformat() for t.
func isoformat(t time.Time) string {
	t = t.UTC()
	s := fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d", t.Year(), int(t.Month()), t.Day(), t.Hour(), t.Minute(), t.Second())
	if us := t.Nanosecond() / 1000; us != 0 {
		s += fmt.Sprintf(".%06d", us)
	}
	return s + "+00:00"
}

func eventLine(name, id string, at time.Time, fields map[string]any) (string, error) {
	rec := map[string]any{"event": name, "id": id, "at": isoformat(at)}
	for k, v := range fields {
		rec[k] = v
	}
	s, err := route.PyJSON(rec)
	if err != nil {
		return "", slErr("cannot encode spend log line: %s", err)
	}
	return s + "\n", nil
}

// CreateSpendLog writes the `open` line to a new log. It refuses an existing file.
func CreateSpendLog(repoRoot, path string, opts SpendLogOptions) error {
	opts = opts.withDefaults()
	if err := checkPath(repoRoot, path); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, fs.ErrExist) {
		return slErr("spend log %s already exists; refusing to recreate it", path)
	} else if err != nil {
		return slErr("cannot create spend log %s: %s", path, osDetail(err))
	}
	defer f.Close()
	line, err := eventLine("open", opts.NewID(), opts.Clock(), nil)
	if err != nil {
		return err
	}
	if err := writeAll(f, line); err != nil {
		return slErr("cannot write spend log %s: %s", path, osDetail(err))
	}
	if err := fsync(f); err != nil {
		return slErr("cannot write spend log %s: %s", path, osDetail(err))
	}
	return nil
}

func writeAll(f *os.File, s string) error {
	b := []byte(s)
	for len(b) > 0 {
		n, err := f.Write(b)
		if err != nil {
			return err
		}
		b = b[n:]
	}
	return nil
}

// SpendLog is an open handle on a log path; every method locks the file.
type SpendLog struct {
	path string
	cap  float64
	opts SpendLogOptions
}

// OpenSpendLog checks the path and opens no file, as Python's constructor does.
func OpenSpendLog(repoRoot, path string, monthlyCapUSD float64, opts SpendLogOptions) (*SpendLog, error) {
	if err := checkPath(repoRoot, path); err != nil {
		return nil, err
	}
	return &SpendLog{path: path, cap: monthlyCapUSD, opts: opts.withDefaults()}, nil
}

// withLock opens the log O_RDWR|O_APPEND, takes flock(2) LOCK_EX (the lock
// Python's fcntl.flock takes; POSIX record locks would not exclude it), reads
// and validates the whole file, runs fn, then unlocks and closes.
func (l *SpendLog) withLock(fn func(f *os.File, events []event) error) error {
	f, err := os.OpenFile(l.path, os.O_RDWR|os.O_APPEND, 0)
	if errors.Is(err, fs.ErrNotExist) {
		return slErr("spend log %s is missing; a missing log is never an empty month (create one with --create-spend-log)", l.path)
	} else if err != nil {
		return slErr("cannot open spend log %s: %s", l.path, osDetail(err))
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		return slErr("cannot lock spend log %s: %s", l.path, err)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return slErr("cannot open spend log %s: %s", l.path, osDetail(err))
	}
	raw, err := io.ReadAll(f)
	if err != nil {
		return slErr("cannot open spend log %s: %s", l.path, osDetail(err))
	}
	events, err := validate(raw, l.path)
	if err != nil {
		return err
	}
	return fn(f, events)
}

func (l *SpendLog) appendLine(f *os.File, line string) error {
	if err := writeAll(f, line); err != nil {
		return slErr("cannot write spend log %s: %s", l.path, osDetail(err))
	}
	if err := fsync(f); err != nil {
		return slErr("cannot write spend log %s: %s", l.path, osDetail(err))
	}
	return nil
}

func (l *SpendLog) mtd(events []event) float64 {
	now := l.opts.Clock().UTC()
	var order []string
	reserved := map[string]num{}
	final := map[string]num{}
	for _, e := range events {
		switch e.name {
		case "reserve":
			if e.at.Year() == now.Year() && e.at.Month() == now.Month() {
				if _, seen := reserved[e.id]; !seen {
					order = append(order, e.id)
				}
				reserved[e.id] = e.usd
			}
		case "settle":
			final[e.id] = e.usd
		}
	}
	vals := make([]num, 0, len(order))
	for _, id := range order {
		if v, ok := final[id]; ok {
			vals = append(vals, v)
		} else {
			vals = append(vals, reserved[id])
		}
	}
	return pySum(vals)
}

// MonthToDate is the sum, in CPython's sum() order and arithmetic, of each
// reservation in the clock's UTC month at its settled or reserved amount.
func (l *SpendLog) MonthToDate() (float64, error) {
	var out float64
	err := l.withLock(func(_ *os.File, events []event) error {
		out = l.mtd(events)
		return nil
	})
	return out, err
}

func finiteNonNeg(f float64) bool { return !math.IsNaN(f) && !math.IsInf(f, 0) && f >= 0 }

// Reserve charges usd before a paid call and returns the reservation id.
func (l *SpendLog) Reserve(runID, model, split string, usd float64) (string, error) {
	if split != "dev" && split != "eval" {
		return "", slErr("split must be one of dev, eval")
	}
	if !finiteNonNeg(usd) {
		return "", slErr("reservation must be a finite number >= 0")
	}
	var rid string
	err := l.withLock(func(f *os.File, events []event) error {
		mtd := l.mtd(events)
		if mtd+usd > l.cap {
			return &MonthlyCapError{&SpendLogError{fmt.Sprintf("monthly cap %.4f reached: month to date %.4f plus %.4f", l.cap, mtd, usd)}}
		}
		id := l.opts.NewID()
		line, err := eventLine("reserve", id, l.opts.Clock(), map[string]any{"run_id": runID, "model": model, "split": split, "usd": usd})
		if err != nil {
			return err
		}
		if err := l.appendLine(f, line); err != nil {
			return err
		}
		rid = id
		return nil
	})
	return rid, err
}

func (l *SpendLog) close(rid, line string) error {
	return l.withLock(func(f *os.File, events []event) error {
		found, closed := false, false
		for _, e := range events {
			if e.id == rid && e.name == "reserve" {
				found = true
			}
			if e.id == rid && (e.name == "settle" || e.name == "unknown") {
				closed = true
			}
		}
		if !found {
			return slErr("no reservation with that id")
		}
		if closed {
			return slErr("reservation already settled or marked unknown")
		}
		return l.appendLine(f, line)
	})
}

// lineID mirrors Python's `fields.pop("id") or uuid4().hex`: an empty id draws one.
func (l *SpendLog) lineID(rid string) string {
	if rid == "" {
		return l.opts.NewID()
	}
	return rid
}

// Settle records a reservation's real cost and token counts.
func (l *SpendLog) Settle(rid string, usd float64, tokens TokenUsage) error {
	if !finiteNonNeg(usd) {
		return slErr("settled usd must be a finite number >= 0")
	}
	if tokens.InputTokens < 0 || tokens.OutputTokens < 0 || tokens.CacheWriteTokens < 0 || tokens.CacheReadTokens < 0 {
		return slErr("token counts must be non-negative ints")
	}
	line, err := eventLine("settle", l.lineID(rid), l.opts.Clock(), map[string]any{
		"usd": usd, "input_tokens": tokens.InputTokens, "output_tokens": tokens.OutputTokens,
		"cache_write_tokens": tokens.CacheWriteTokens, "cache_read_tokens": tokens.CacheReadTokens,
	})
	if err != nil {
		return err
	}
	return l.close(rid, line)
}

// MarkUnknown records that a reservation's real cost is unknown; it stays charged.
func (l *SpendLog) MarkUnknown(rid string) error {
	line, err := eventLine("unknown", l.lineID(rid), l.opts.Clock(), nil)
	if err != nil {
		return err
	}
	return l.close(rid, line)
}

// num is a log amount as Python holds it: an int (within int64, Owner
// decision Q2) or a float.
type num struct {
	isInt bool
	i     int64
	f     float64
}

func (n num) float() float64 {
	if n.isInt {
		return float64(n.i)
	}
	return n.f
}

// pySum ports CPython 3.12's builtin_sum_impl (Python/bltinmodule.c) with
// start 0, branch by branch:
//   - int fast path: exact C-long accumulation while items are ints and the
//     sum doesn't overflow;
//   - on overflow, the generic path with exact (big) ints until a float turns
//     the result into a float, after which every addition is plain;
//   - on the first float, result = float(i_result) + x (plain), then the float
//     path: Neumaier compensation for floats, uncompensated `+= (double)v`
//     for ints, and `f_result += c` at the end when c is non-zero and finite.
//
// Precedent: ingest.pySum (split.go) and eval.pySum (score.go), which lack the
// exact-int prefix and the overflow branch.
func pySum(items []num) float64 {
	var iResult int64
	k := 0
	for ; k < len(items); k++ {
		it := items[k]
		if it.isInt && (iResult >= 0 && it.i <= math.MaxInt64-iResult || iResult < 0 && it.i >= math.MinInt64-iResult) {
			iResult += it.i
			continue
		}
		break
	}
	if k == len(items) {
		return float64(iResult)
	}
	if items[k].isInt { // overflow: generic path from an exact int
		acc := new(big.Int).SetInt64(iResult)
		for ; k < len(items) && items[k].isInt; k++ {
			acc.Add(acc, big.NewInt(items[k].i))
		}
		f, _ := new(big.Float).SetInt(acc).Float64()
		for ; k < len(items); k++ {
			f += items[k].float()
		}
		return f
	}
	fResult := float64(iResult) + items[k].f
	c := 0.0
	for k++; k < len(items); k++ {
		it := items[k]
		if !it.isInt {
			x := it.f
			t := fResult + x
			if math.Abs(fResult) >= math.Abs(x) {
				c += (fResult - t) + x
			} else {
				c += (x - t) + fResult
			}
			fResult = t
			continue
		}
		fResult += float64(it.i)
	}
	if c != 0 && !math.IsInf(c, 0) && !math.IsNaN(c) {
		fResult += c
	}
	return fResult
}

type event struct {
	name, id string
	at       time.Time
	usd      num
}

var fields = map[string][]string{
	"open":    {"event", "id", "at"},
	"reserve": {"event", "id", "at", "run_id", "model", "split", "usd"},
	"settle":  {"event", "id", "at", "usd", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"},
	"unknown": {"event", "id", "at"},
}

var amountKeys = []string{"usd", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"}

// Owner decision Q1: YYYY-MM-DD[T| ]HH:MM:SS[.1-6 digits][Z|±HH:MM].
var tsRe = regexp.MustCompile(`^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})?$`)

// parseTS returns the time, whether it has a zone, and ok.
func parseTS(s string) (time.Time, bool, bool) {
	m := tsRe.FindStringSubmatch(s)
	if m == nil {
		return time.Time{}, false, false
	}
	atoi := func(x string) int { n, _ := strconv.Atoi(x); return n }
	y, mo, d, h, mi, se := atoi(m[1]), atoi(m[2]), atoi(m[3]), atoi(m[4]), atoi(m[5]), atoi(m[6])
	us := 0
	if m[7] != "" {
		frac := m[7]
		for len(frac) < 6 {
			frac += "0"
		}
		us = atoi(frac)
	}
	if y < 1 || mo < 1 || mo > 12 || h > 23 || mi > 59 || se > 59 {
		return time.Time{}, false, false
	}
	loc := time.UTC
	if m[8] != "" && m[8] != "Z" {
		oh, om := atoi(m[8][1:3]), atoi(m[8][4:6])
		if oh > 23 || om > 59 {
			return time.Time{}, false, false
		}
		off := oh*3600 + om*60
		if m[8][0] == '-' {
			off = -off
		}
		loc = time.FixedZone("", off)
	}
	t := time.Date(y, time.Month(mo), d, h, mi, se, us*1000, loc)
	if t.Day() != d { // Feb 30 and friends
		return time.Time{}, false, false
	}
	return t.UTC(), m[8] != "", true
}

// amount is Python's _amount for a log value: a non-bool finite number >= 0,
// with int literals outside int64 refused (Owner decision Q2).
func amount(v policy.Value) (num, bool) {
	f, isInt, ok := number(v)
	if !ok || math.IsInf(f, 0) || math.IsNaN(f) || f < 0 {
		return num{}, false
	}
	if isInt {
		n, _ := strconv.ParseInt(v.Num, 10, 64)
		return num{isInt: true, i: n}, true
	}
	return num{f: f}, true
}

// validate ports _validate: errors name the line number, never its content.
func validate(raw []byte, path string) ([]event, error) {
	fail := func(n int, problem string) error {
		return slErr("spend log %s line %d: %s", path, n, problem)
	}
	if len(raw) > 0 && raw[len(raw)-1] != '\n' {
		nl := 0
		for _, c := range raw {
			if c == '\n' {
				nl++
			}
		}
		return nil, fail(nl+1, "partial last line")
	}
	var lines [][]byte
	start := 0
	for i, c := range raw {
		if c == '\n' {
			lines = append(lines, raw[start:i])
			start = i + 1
		}
	}
	if len(lines) == 0 {
		return nil, fail(1, "missing open line")
	}
	var events []event
	reserved, closed := map[string]bool{}, map[string]bool{}
	for idx, b := range lines {
		n := idx + 1
		e, err := policy.ParseJSON(b)
		if err != nil || !utf8.Valid(b) { // Python decodes UTF-8 first; ParseJSON does not check it
			return nil, fail(n, "not valid JSON")
		}
		if e.Kind != policy.Object {
			return nil, fail(n, "not an object")
		}
		ev, _ := e.Get("event")
		keys, known := fields[ev.Str]
		if ev.Kind != policy.String || !known {
			return nil, fail(n, "unknown event")
		}
		name := ev.Str
		if (n == 1) != (name == "open") {
			if n == 1 {
				return nil, fail(n, "missing open line")
			}
			return nil, fail(n, "open event after line 1")
		}
		has := map[string]bool{}
		for _, k := range keys {
			if _, ok := e.Get(k); !ok {
				return nil, fail(n, "missing field "+k)
			}
			has[k] = true
		}
		id, _ := e.Get("id")
		if id.Kind != policy.String || id.Str == "" {
			return nil, fail(n, "bad id")
		}
		atV, _ := e.Get("at")
		if atV.Kind != policy.String {
			return nil, fail(n, "bad timestamp")
		}
		at, zoned, ok := parseTS(atV.Str)
		if !ok {
			return nil, fail(n, "bad timestamp")
		}
		if !zoned {
			return nil, fail(n, "timestamp has no time zone")
		}
		out := event{name: name, id: id.Str, at: at}
		for _, k := range amountKeys {
			if !has[k] {
				continue
			}
			v, _ := e.Get(k)
			a, ok := amount(v)
			if !ok {
				return nil, fail(n, k+" must be a finite number >= 0")
			}
			if k == "usd" {
				out.usd = a
			}
		}
		switch name {
		case "reserve":
			if reserved[out.id] {
				return nil, fail(n, "duplicate reservation id")
			}
			sp, _ := e.Get("split")
			if sp.Kind != policy.String || (sp.Str != "dev" && sp.Str != "eval") {
				return nil, fail(n, "bad split")
			}
			reserved[out.id] = true
		case "settle", "unknown":
			if !reserved[out.id] {
				return nil, fail(n, name+" without a reservation")
			}
			if closed[out.id] {
				return nil, fail(n, "second settle or unknown for one reservation")
			}
			closed[out.id] = true
		}
		events = append(events, out)
	}
	return events, nil
}
