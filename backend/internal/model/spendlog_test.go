package model

import (
	"bufio"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

var nov = time.Date(2026, 11, 18, 10, 0, 0, 0, time.UTC)

// counterIDs returns the golden script's id source: %032x of 1, 2, 3, ...
func counterIDs() func() string {
	n := 0
	return func() string { n++; return fmt.Sprintf("%032x", n) }
}

// realTemp is a temp dir with symlinks resolved, outside the repository.
func realTemp(t *testing.T) string {
	t.Helper()
	d, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return d
}

type env struct {
	t    *testing.T
	repo string
	path string
	ids  func() string
}

func newEnv(t *testing.T) *env {
	return &env{t: t, repo: parity.RepoRoot(t), path: filepath.Join(realTemp(t), "spend.jsonl"), ids: counterIDs()}
}

func (e *env) create(at time.Time) {
	e.t.Helper()
	if err := CreateSpendLog(e.repo, e.path, SpendLogOptions{Clock: func() time.Time { return at }, NewID: e.ids}); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) log(cap float64, at time.Time) *SpendLog {
	e.t.Helper()
	l, err := OpenSpendLog(e.repo, e.path, cap, SpendLogOptions{Clock: func() time.Time { return at }, NewID: e.ids})
	if err != nil {
		e.t.Fatal(err)
	}
	return l
}

func (e *env) read() string {
	e.t.Helper()
	b, err := os.ReadFile(e.path)
	if err != nil {
		e.t.Fatal(err)
	}
	return string(b)
}

func (e *env) write(s string) {
	e.t.Helper()
	if err := os.WriteFile(e.path, []byte(s), 0o600); err != nil {
		e.t.Fatal(err)
	}
}

// TestWorkedExampleLog: the spec's worked example, Python 3.12.3's exact bytes.
func TestWorkedExampleLog(t *testing.T) {
	e := newEnv(t)
	oct31 := time.Date(2026, 10, 31, 23, 59, 0, 0, time.UTC)
	e.create(oct31)
	l := e.log(1.0, oct31)
	rid, err := l.Reserve("run-1", "claude-haiku-5-5", "dev", 0.1)
	if err != nil || rid != "00000000000000000000000000000002" {
		t.Fatalf("reserve: %q %v", rid, err)
	}
	if err := l.Settle(rid, 0.000125, TokenUsage{InputTokens: 1000, OutputTokens: 50}); err != nil {
		t.Fatal(err)
	}
	later := time.Date(2026, 12, 1, 1, 0, 0, 123456000, time.FixedZone("", 2*3600))
	l2 := e.log(1.0, later)
	rid2, err := l2.Reserve("run-1", "m", "eval", 0.2)
	if err != nil {
		t.Fatal(err)
	}
	if err := l2.MarkUnknown(rid2); err != nil {
		t.Fatal(err)
	}
	want := `{"at": "2026-10-31T23:59:00+00:00", "event": "open", "id": "00000000000000000000000000000001"}
{"at": "2026-10-31T23:59:00+00:00", "event": "reserve", "id": "00000000000000000000000000000002", "model": "claude-haiku-5-5", "run_id": "run-1", "split": "dev", "usd": 0.1}
{"at": "2026-10-31T23:59:00+00:00", "cache_read_tokens": 0, "cache_write_tokens": 0, "event": "settle", "id": "00000000000000000000000000000002", "input_tokens": 1000, "output_tokens": 50, "usd": 0.000125}
{"at": "2026-11-30T23:00:00.123456+00:00", "event": "reserve", "id": "00000000000000000000000000000003", "model": "m", "run_id": "run-1", "split": "eval", "usd": 0.2}
{"at": "2026-11-30T23:00:00.123456+00:00", "event": "unknown", "id": "00000000000000000000000000000003"}
`
	if got := e.read(); got != want {
		t.Errorf("log:\n%s\nwant:\n%s", got, want)
	}
	if mtd, err := l2.MonthToDate(); err != nil || mtd != 0.2 {
		t.Errorf("mtd %v %v", mtd, err)
	}
	_, err = l2.Reserve("run-1", "m", "eval", 0.9)
	if err == nil || err.Error() != "monthly cap 1.0000 reached: month to date 0.2000 plus 0.9000" {
		t.Errorf("cap: %v", err)
	}
}

func TestCreateAndReserveSettle(t *testing.T) {
	e := newEnv(t)
	e.create(nov)
	l := e.log(1.0, nov)
	rid, err := l.Reserve("run1", "m", "dev", 0.002)
	if err != nil || len(rid) != 32 {
		t.Fatalf("reserve %q %v", rid, err)
	}
	if mtd, err := l.MonthToDate(); err != nil || mtd != 0.002 {
		t.Errorf("mtd %v %v", mtd, err)
	}
	if err := l.Settle(rid, 0.001, TokenUsage{10, 2, 0, 0}); err != nil {
		t.Fatal(err)
	}
	if mtd, err := l.MonthToDate(); err != nil || mtd != 0.001 {
		t.Errorf("mtd after settle %v %v", mtd, err)
	}
	if n := strings.Count(e.read(), "\n"); n != 3 {
		t.Errorf("lines %d", n)
	}
}

func TestDefaultIDIs32Hex(t *testing.T) {
	e := newEnv(t)
	if err := CreateSpendLog(e.repo, e.path, SpendLogOptions{}); err != nil {
		t.Fatal(err)
	}
	l, err := OpenSpendLog(e.repo, e.path, 1.0, SpendLogOptions{})
	if err != nil {
		t.Fatal(err)
	}
	a, err1 := l.Reserve("r", "m", "dev", 0)
	b, err2 := l.Reserve("r", "m", "dev", 0)
	hex := func(s string) bool { return len(s) == 32 && strings.Trim(s, "0123456789abcdef") == "" }
	if err1 != nil || err2 != nil || !hex(a) || !hex(b) || a == b || a[12] != '4' {
		t.Errorf("ids %q %q %v %v", a, b, err1, err2)
	}
}

func TestMonthBoundary(t *testing.T) {
	e := newEnv(t)
	oct := time.Date(2026, 10, 31, 23, 59, 0, 0, time.UTC)
	e.create(oct)
	if _, err := e.log(1.0, oct).Reserve("r", "m", "dev", 0.5); err != nil {
		t.Fatal(err)
	}
	if mtd, err := e.log(1.0, time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)).MonthToDate(); err != nil || mtd != 0.0 {
		t.Errorf("november: %v %v", mtd, err)
	}
	// 2026-11-01 01:00 at +02:00 is still October in UTC.
	if mtd, err := e.log(1.0, time.Date(2026, 11, 1, 1, 0, 0, 0, time.FixedZone("", 7200))).MonthToDate(); err != nil || mtd != 0.5 {
		t.Errorf("october via offset: %v %v", mtd, err)
	}
}

func TestMonthlyCapRefusesWithoutWriting(t *testing.T) {
	e := newEnv(t)
	e.create(nov)
	before := e.read()
	_, err := e.log(0.01, nov).Reserve("r", "m", "dev", 0.02)
	var ce *MonthlyCapError
	var se *SpendLogError
	if !errors.As(err, &ce) || !errors.As(err, &se) {
		t.Fatalf("want MonthlyCapError and SpendLogError, got %T %v", err, err)
	}
	if err.Error() != "monthly cap 0.0100 reached: month to date 0.0000 plus 0.0200" {
		t.Errorf("msg %q", err)
	}
	if e.read() != before {
		t.Error("log changed")
	}
	// No id drawn: the next reservation gets id 2.
	if rid, err := e.log(1.0, nov).Reserve("r", "m", "dev", 0.02); err != nil || rid != fmt.Sprintf("%032x", 2) {
		t.Errorf("next id %q %v", rid, err)
	}
}

func TestUnsettledAndUnknownStayCharged(t *testing.T) {
	e := newEnv(t)
	e.create(nov)
	l := e.log(1.0, nov)
	a, _ := l.Reserve("r", "m", "dev", 0.3)
	b, _ := l.Reserve("r", "m", "dev", 0.2)
	if _, err := l.Reserve("r", "m", "dev", 0.1); err != nil {
		t.Fatal(err)
	}
	if err := l.Settle(a, 0.05, TokenUsage{1, 1, 0, 0}); err != nil {
		t.Fatal(err)
	}
	if err := l.MarkUnknown(b); err != nil {
		t.Fatal(err)
	}
	if mtd, err := l.MonthToDate(); err != nil || mtd != 0.35000000000000003 {
		t.Errorf("mtd %v %v", mtd, err)
	}
	if err := l.MarkUnknown(b); err == nil || err.Error() != "reservation already settled or marked unknown" {
		t.Errorf("second unknown: %v", err)
	}
	if err := l.Settle("nope", 0, TokenUsage{}); err == nil || err.Error() != "no reservation with that id" {
		t.Errorf("unknown id: %v", err)
	}
}

func TestArgumentErrors(t *testing.T) {
	e := newEnv(t)
	e.create(nov)
	l := e.log(1.0, nov)
	cases := []struct {
		err  error
		want string
	}{
		{func() error { _, err := l.Reserve("r", "m", "test", 0.1); return err }(), "split must be one of dev, eval"},
		{func() error { _, err := l.Reserve("r", "m", "dev", -0.1); return err }(), "reservation must be a finite number >= 0"},
		{func() error { _, err := l.Reserve("r", "m", "dev", math.NaN()); return err }(), "reservation must be a finite number >= 0"},
		{func() error { _, err := l.Reserve("r", "m", "dev", math.Inf(1)); return err }(), "reservation must be a finite number >= 0"},
		{l.Settle("x", -1, TokenUsage{}), "settled usd must be a finite number >= 0"},
		{l.Settle("x", 0, TokenUsage{OutputTokens: -1}), "token counts must be non-negative ints"},
	}
	for i, c := range cases {
		var se *SpendLogError
		if !errors.As(c.err, &se) || c.err.Error() != c.want {
			t.Errorf("%d: %v, want %s", i, c.err, c.want)
		}
	}
}

func TestCreateRefusesExisting(t *testing.T) {
	e := newEnv(t)
	e.write("keep\n")
	err := CreateSpendLog(e.repo, e.path, SpendLogOptions{})
	if err == nil || err.Error() != "spend log "+e.path+" already exists; refusing to recreate it" {
		t.Errorf("err %v", err)
	}
	if e.read() != "keep\n" {
		t.Error("file changed")
	}
}

const openLine = `{"at": "2026-11-01T00:00:00+00:00", "event": "open", "id": "o"}` + "\n"

func reserveLine(id, at, usd string) string {
	return `{"at": "` + at + `", "event": "reserve", "id": "` + id + `", "model": "m", "run_id": "r", "split": "dev", "usd": ` + usd + "}\n"
}

const settleTail = `"cache_read_tokens": 0, "cache_write_tokens": 0, "input_tokens": 1, "output_tokens": 1`

// TestCorruptLogFailsClosed: every refused log keeps its bytes for both
// Reserve and MonthToDate.
func TestCorruptLogFailsClosed(t *testing.T) {
	r := reserveLine("a", "2026-11-02T00:00:00+00:00", "0.1")
	cases := []struct{ name, content, problem string }{
		{"empty", "", "line 1: missing open line"},
		{"lone newline", "\n", "line 1: not valid JSON"},
		{"partial", openLine + `{"event"`, "line 2: partial last line"},
		{"bad json", openLine + "{x\n", "line 2: not valid JSON"},
		{"bad utf8", openLine + "\"\xff\"\n", "line 2: not valid JSON"},
		{"list", openLine + "[]\n", "line 2: not an object"},
		{"unknown event", openLine + `{"event": "spend"}` + "\n", "line 2: unknown event"},
		{"no open", r, "line 1: missing open line"},
		{"second open", openLine + openLine, "line 2: open event after line 1"},
		{"missing at", `{"event": "open", "id": "o"}` + "\n", "line 1: missing field at"},
		{"missing split", openLine + `{"at": "2026-11-02T00:00:00+00:00", "event": "reserve", "id": "a", "model": "m", "run_id": "r", "usd": 1}` + "\n", "line 2: missing field split"},
		{"missing token", openLine + r + `{"at": "2026-11-02T00:00:00+00:00", "cache_read_tokens": 0, "event": "settle", "id": "a", "input_tokens": 1, "output_tokens": 1, "usd": 0}` + "\n", "line 3: missing field cache_write_tokens"},
		{"bad id", `{"at": "2026-11-01T00:00:00+00:00", "event": "open", "id": ""}` + "\n", "line 1: bad id"},
		{"int id", `{"at": "2026-11-01T00:00:00+00:00", "event": "open", "id": 3}` + "\n", "line 1: bad id"},
		{"bad timestamp", `{"at": "yesterday", "event": "open", "id": "o"}` + "\n", "line 1: bad timestamp"},
		{"int timestamp", `{"at": 5, "event": "open", "id": "o"}` + "\n", "line 1: bad timestamp"},
		{"bad date", `{"at": "2026-02-30T00:00:00+00:00", "event": "open", "id": "o"}` + "\n", "line 1: bad timestamp"},
		{"naive", `{"at": "2026-11-01T00:00:00", "event": "open", "id": "o"}` + "\n", "line 1: timestamp has no time zone"},
		{"negative usd", openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", "-1"), "line 2: usd must be a finite number >= 0"},
		{"bool usd", openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", "true"), "line 2: usd must be a finite number >= 0"},
		{"huge float usd", openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", "1e400"), "line 2: usd must be a finite number >= 0"},
		{"negative token", openLine + r + `{"at": "2026-11-02T00:00:00+00:00", "cache_read_tokens": 0, "cache_write_tokens": 0, "event": "settle", "id": "a", "input_tokens": -1, "output_tokens": 1, "usd": 0}` + "\n", "line 3: input_tokens must be a finite number >= 0"},
		{"duplicate reservation", openLine + r + r, "line 3: duplicate reservation id"},
		{"bad split", openLine + `{"at": "2026-11-02T00:00:00+00:00", "event": "reserve", "id": "a", "model": "m", "run_id": "r", "split": "test", "usd": 1}` + "\n", "line 2: bad split"},
		{"orphan settle", openLine + `{"at": "2026-11-02T00:00:00+00:00", ` + settleTail + `, "event": "settle", "id": "z", "usd": 0}` + "\n", "line 2: settle without a reservation"},
		{"orphan unknown", openLine + `{"at": "2026-11-02T00:00:00+00:00", "event": "unknown", "id": "z"}` + "\n", "line 2: unknown without a reservation"},
		{"second close", openLine + r + `{"at": "2026-11-02T00:00:00+00:00", "event": "unknown", "id": "a"}` + "\n" + `{"at": "2026-11-02T00:00:00+00:00", "event": "unknown", "id": "a"}` + "\n", "line 4: second settle or unknown for one reservation"},
	}
	for _, c := range cases {
		e := newEnv(t)
		e.write(c.content)
		l := e.log(100, nov)
		want := "spend log " + e.path + " " + c.problem
		if _, err := l.MonthToDate(); err == nil || err.Error() != want {
			t.Errorf("%s: mtd %v; want %s", c.name, err, want)
		}
		if _, err := l.Reserve("r", "m", "dev", 0.001); err == nil || err.Error() != want {
			t.Errorf("%s: reserve %v; want %s", c.name, err, want)
		}
		if e.read() != c.content {
			t.Errorf("%s: file changed", c.name)
		}
	}
}

func TestMissingLogStaysMissing(t *testing.T) {
	e := newEnv(t)
	l := e.log(1, nov)
	want := "spend log " + e.path + " is missing; a missing log is never an empty month (create one with --create-spend-log)"
	if _, err := l.MonthToDate(); err == nil || err.Error() != want {
		t.Errorf("mtd: %v", err)
	}
	if _, err := l.Reserve("r", "m", "dev", 0.1); err == nil || err.Error() != want {
		t.Errorf("reserve: %v", err)
	}
	if _, err := os.Stat(e.path); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("file created: %v", err)
	}
}

// TestAcceptedLogForms: CRLF, extra and duplicate keys, int usd, float token
// counts, and every timestamp form Owner decision Q1 keeps.
func TestAcceptedLogForms(t *testing.T) {
	log := openLine +
		`{"at": "2026-11-02 00:00:00Z", "event": "reserve", "extra": [1], "id": "a", "model": 7, "run_id": null, "split": "dev", "usd": 1}` + "\r\n" +
		reserveLine("b", "2026-11-02T00:00:00.5+00:00", "0.25") +
		reserveLine("c", "2026-11-02T00:00:00.123456-05:30", "0.5") +
		`{"at": "2026-11-03T00:00:00+00:00", "cache_read_tokens": 0.0, "cache_write_tokens": 0, "event": "settle", "id": "c", "input_tokens": 1.5, "output_tokens": 1, "usd": 0.125, "usd": 0.0625}` + "\n" +
		reserveLine("d", "2026-10-31T23:00:00-02:00", "2") +
		reserveLine("e", "2026-10-31T23:00:00+00:00", "4")
	e := newEnv(t)
	e.write(log)
	if mtd, err := e.log(100, nov).MonthToDate(); err != nil || mtd != 3.3125 {
		t.Errorf("mtd %v %v", mtd, err)
	}
}

// TestPythonSum: CPython 3.12's sum() on Python-probed literals.
func TestPythonSum(t *testing.T) {
	cases := []struct {
		usd  []string
		want float64
	}{
		{[]string{"0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1"}, 1.0},
		{[]string{"0.3", "0.2", "0.1"}, 0.6},
		{[]string{"0.1", "0.2", "0.3"}, 0.6},
		{[]string{"0.1", "1", "0.2"}, 1.3},
		{[]string{"1", "0.1", "2", "0.2"}, 3.3000000000000003},
		{[]string{"0.3", "0.3", "0.3"}, 0.8999999999999999},
		{[]string{"9223372036854775807", "1", "0.5"}, 9.223372036854776e+18},
		{nil, 0.0},
	}
	for _, c := range cases {
		log := openLine
		for i, u := range c.usd {
			log += reserveLine(fmt.Sprint(i), "2026-11-02T00:00:00+00:00", u)
		}
		e := newEnv(t)
		e.write(log)
		if mtd, err := e.log(1e30, nov).MonthToDate(); err != nil || mtd != c.want {
			t.Errorf("%v: %v %v, want %v", c.usd, mtd, err, c.want)
		}
	}
}

func TestPathInsideRepoRefused(t *testing.T) {
	repo := parity.RepoRoot(t)
	inside := filepath.Join(repo, "spend_should_not_exist.jsonl")
	want := "spend log path " + inside + " is inside the repository " + repo + "; keep it outside"
	if _, err := OpenSpendLog(repo, inside, 1, SpendLogOptions{}); err == nil || err.Error() != want {
		t.Errorf("open: %v", err)
	}
	if err := CreateSpendLog(repo, inside, SpendLogOptions{}); err == nil || err.Error() != want {
		t.Errorf("create: %v", err)
	}
	if _, err := os.Stat(inside); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("file created: %v", err)
	}
	// A fake root reached through a symlink: <outside>/link/../x is <root>/a/x.
	root, outside := realTemp(t), realTemp(t)
	if err := os.MkdirAll(filepath.Join(root, "a", "b"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "a", "b"), filepath.Join(outside, "link")); err != nil {
		t.Fatal(err)
	}
	p := outside + "/link/../x"
	want = "spend log path " + outside + "/x is inside the repository " + root + "; keep it outside"
	if _, err := OpenSpendLog(root, p, 1, SpendLogOptions{}); err == nil || err.Error() != want {
		t.Errorf("open via link: %v", err)
	}
	if err := CreateSpendLog(root, p, SpendLogOptions{}); err == nil || err.Error() != want {
		t.Errorf("create via link: %v", err)
	}
	for _, f := range []string{filepath.Join(root, "a", "x"), filepath.Join(outside, "x")} {
		if _, err := os.Stat(f); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("%s created: %v", f, err)
		}
	}
}

func TestAppendFsyncs(t *testing.T) {
	orig := fsync
	t.Cleanup(func() { fsync = orig })
	count := 0
	fsync = func(f *os.File) error { count++; return orig(f) }
	e := newEnv(t)
	e.create(nov)
	count = 0
	l := e.log(1, nov)
	rid, err := l.Reserve("r", "m", "dev", 0.01)
	if err != nil || count != 1 {
		t.Fatalf("after reserve: %d %v", count, err)
	}
	if err := l.MarkUnknown(rid); err != nil || count != 2 {
		t.Errorf("after unknown: %d %v", count, err)
	}
}

func TestLoadMonthlyCap(t *testing.T) {
	repo := parity.RepoRoot(t)
	for _, p := range []string{"tests/fixtures/thresholds_spend.json", "evals/thresholds.json"} {
		if got, err := LoadMonthlyCap(filepath.Join(repo, p)); err != nil || got != 200.0 {
			t.Errorf("%s: %v %v", p, got, err)
		}
	}
	dir := t.TempDir()
	cap := func(v string) string { return `{"guardrails": {"api_spend_usd_per_month": {"max": ` + v + `}}}` }
	const bad = ": api_spend_usd_per_month.max must be a finite number > 0"
	cases := []struct{ name, content, suffix string }{
		{"missing_key", `{"guardrails": {}}`, " lacks guardrails.api_spend_usd_per_month.max"},
		{"not_object", `{"guardrails": []}`, " lacks guardrails.api_spend_usd_per_month.max"},
		{"zero", cap("0"), bad},
		{"negative", cap("-5"), bad},
		{"bool", cap("true"), bad},
		{"string", cap(`"200"`), bad},
		{"bad_json", "{", " is not valid JSON"},
		{"bad_utf8", "{\"x\": \"\xff\"}", " is not valid JSON"},
		{"nan", cap("NaN"), " is not valid JSON"},
		{"inf", cap("Infinity"), " is not valid JSON"},
		{"neginf", cap("-Infinity"), " is not valid JSON"},
		{"surrogate", `{"x": "\ud800"}`, " is not valid JSON"},
		{"int_1e30", cap("1000000000000000000000000000000"), bad},
		{"int_2_63", cap("9223372036854775808"), bad},
	}
	for _, c := range cases {
		path := filepath.Join(dir, c.name+".json")
		writeFile(t, path, c.content)
		_, err := LoadMonthlyCap(path)
		var se *SpendLogError
		if want := "thresholds file " + path + c.suffix; !errors.As(err, &se) || err.Error() != want {
			t.Errorf("%s: %v; want %s", c.name, err, want)
		}
	}
	path := filepath.Join(dir, "max.json")
	writeFile(t, path, cap("9223372036854775807"))
	if got, err := LoadMonthlyCap(path); err != nil || got != 9.223372036854776e+18 {
		t.Errorf("int64 max: %v %v", got, err)
	}
	missing := filepath.Join(dir, "missing.json")
	if _, err := LoadMonthlyCap(missing); err == nil || err.Error() != "thresholds file "+missing+" not found" {
		t.Errorf("missing: %v", err)
	}
}

// TestRefusesNonStandard: log parts of criterion 12 (Owner decisions Q1-Q3
// and #67 Q2). Python's results differ, so none of these is in the goldens.
func TestRefusesNonStandard(t *testing.T) {
	type tc struct{ name, content, problem string }
	var cases []tc
	for _, v := range []string{"NaN", "Infinity", "-Infinity"} {
		cases = append(cases, tc{"usd " + v, openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", v), "line 2: not valid JSON"})
	}
	cases = append(cases, tc{"surrogate", openLine + `{"at": "2026-11-02T00:00:00+00:00", "event": "reserve", "id": "\ud800", "model": "m", "run_id": "r", "split": "dev", "usd": 1}` + "\n", "line 2: not valid JSON"})
	for _, ts := range []string{"20261118T100000Z", "2026-W47-3T10:00:00+00:00", "2026-11-18T10+00:00", "2026-11-18T10:00:00,5+00:00",
		"2026-11-18T10:00:00.1234567+00:00", "2026-11-18T10:00:00+0000", "2026-11-18T10:00:00+00:00:30", "2026-11-18X10:00:00+00:00"} {
		cases = append(cases, tc{"ts " + ts, openLine + reserveLine("a", ts, "1"), "line 2: bad timestamp"})
	}
	for _, n := range []string{"1000000000000000000000000000000", "9223372036854775808"} {
		cases = append(cases,
			tc{"usd " + n, openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", n), "line 2: usd must be a finite number >= 0"},
			tc{"token " + n, openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", "1") +
				`{"at": "2026-11-02T00:00:00+00:00", "cache_read_tokens": 0, "cache_write_tokens": 0, "event": "settle", "id": "a", "input_tokens": 1, "output_tokens": ` + n + `, "usd": 0}` + "\n",
				"line 3: output_tokens must be a finite number >= 0"})
	}
	for _, ev := range []string{"[]", "{}", "1", "true", "null"} {
		cases = append(cases, tc{"event " + ev, openLine + `{"at": "2026-11-02T00:00:00+00:00", "event": ` + ev + `, "id": "a"}` + "\n", "line 2: unknown event"})
	}
	for _, c := range cases {
		e := newEnv(t)
		e.write(c.content)
		want := "spend log " + e.path + " " + c.problem
		l := e.log(1e30, nov)
		if _, err := l.MonthToDate(); err == nil || err.Error() != want {
			t.Errorf("%s: %v; want %s", c.name, err, want)
		}
		if _, err := l.Reserve("r", "m", "dev", 0.1); err == nil || err.Error() != want {
			t.Errorf("%s reserve: %v", c.name, err)
		}
		if e.read() != c.content {
			t.Errorf("%s: file changed", c.name)
		}
	}
	// int64 max is accepted where the field allows it.
	e := newEnv(t)
	e.write(openLine + reserveLine("a", "2026-11-02T00:00:00+00:00", "9223372036854775807"))
	if mtd, err := e.log(1e30, nov).MonthToDate(); err != nil || mtd != 9.223372036854776e+18 {
		t.Errorf("int64 max: %v %v", mtd, err)
	}
}

// lockFile takes flock(2) LOCK_EX on its own fd, the lock Python's fcntl.flock takes.
func lockFile(t *testing.T, path string) (unlock func()) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	done := false
	unlock = func() {
		if !done {
			done = true
			syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
			f.Close()
		}
	}
	t.Cleanup(unlock)
	return unlock
}

// startChild starts cmd with a stdout scanner; Cleanup kills and reaps it if
// the test ends while it is still running. exited is closed when it exits.
func startChild(t *testing.T, cmd *exec.Cmd) (lines *bufio.Scanner, exited chan error) {
	t.Helper()
	// An os.Pipe we own, not StdoutPipe: Wait would close StdoutPipe's read end
	// on exit, losing lines the child printed just before exiting.
	out, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { out.Close() })
	cmd.Stdout = w
	cmd.Stderr = os.Stderr
	err = cmd.Start()
	w.Close()
	if err != nil {
		t.Fatal(err)
	}
	exited = make(chan error, 1)
	lines = bufio.NewScanner(out)
	waited := make(chan struct{})
	t.Cleanup(func() {
		select {
		case <-waited:
		default:
			cmd.Process.Kill()
			<-waited
		}
	})
	go func() {
		exited <- cmd.Wait()
		close(waited)
	}()
	return lines, exited
}

func expectLine(t *testing.T, lines *bufio.Scanner, want string) {
	t.Helper()
	got := make(chan string, 1)
	go func() {
		if lines.Scan() {
			got <- lines.Text()
		} else {
			got <- "<EOF>"
		}
	}()
	select {
	case s := <-got:
		if s != want {
			t.Fatalf("child said %q, want %q", s, want)
		}
	case <-time.After(10 * time.Second):
		t.Fatalf("child did not say %q within 10s", want)
	}
}

func stillRunning(t *testing.T, exited chan error) {
	t.Helper()
	time.Sleep(500 * time.Millisecond)
	select {
	case err := <-exited:
		t.Fatalf("child exited while the lock was held: %v", err)
	default:
	}
}

const childEnv = "MODEL_SPENDLOG_LOCK_CHILD"

// TestReserveIsLocked: a re-exec'd child's Reserve waits on the parent's flock.
func TestReserveIsLocked(t *testing.T) {
	if path := os.Getenv(childEnv); path != "" {
		l, err := OpenSpendLog(parity.RepoRoot(t), path, 1.0, SpendLogOptions{Clock: func() time.Time { return nov }})
		if err != nil {
			fmt.Println("error:", err)
			os.Exit(3)
		}
		fmt.Println("ready")
		_, err = l.Reserve("child", "m", "dev", 0.6)
		var ce *MonthlyCapError
		switch {
		case err == nil:
			fmt.Println("ok")
		case errors.As(err, &ce):
			fmt.Println("cap")
		default:
			fmt.Println("error:", err)
		}
		os.Exit(0)
	}
	e := newEnv(t)
	e.create(nov)
	unlock := lockFile(t, e.path)
	cmd := exec.Command(os.Args[0], "-test.run=^TestReserveIsLocked$", "-test.count=1")
	cmd.Env = append(os.Environ(), childEnv+"="+e.path)
	lines, exited := startChild(t, cmd)
	expectLine(t, lines, "ready")
	stillRunning(t, exited)
	unlock()
	_, perr := e.log(1.0, nov).Reserve("parent", "m", "dev", 0.6)
	parent := "ok"
	var ce *MonthlyCapError
	if errors.As(perr, &ce) {
		parent = "cap"
	} else if perr != nil {
		t.Fatal(perr)
	}
	want := map[string]string{"ok": "cap", "cap": "ok"}[parent]
	expectLine(t, lines, want)
	select {
	case err := <-exited:
		if err != nil {
			t.Fatalf("child: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("child did not exit within 10s")
	}
	if mtd, err := e.log(1.0, nov).MonthToDate(); err != nil || mtd != 0.6 {
		t.Errorf("mtd %v %v", mtd, err)
	}
}

// TestConcurrentReserve: 8 goroutines, each with its own fd, against cap 1.0.
func TestConcurrentReserve(t *testing.T) {
	e := newEnv(t)
	e.create(nov)
	var wg sync.WaitGroup
	results := make(chan error, 8)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			l, err := OpenSpendLog(e.repo, e.path, 1.0, SpendLogOptions{Clock: func() time.Time { return nov }})
			if err != nil {
				results <- err
				return
			}
			_, err = l.Reserve("r", "m", "dev", 0.3)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	ok := 0
	for err := range results {
		var ce *MonthlyCapError
		switch {
		case err == nil:
			ok++
		case !errors.As(err, &ce):
			t.Errorf("unexpected error: %v", err)
		}
	}
	if ok != 3 {
		t.Errorf("%d reservations succeeded, want 3", ok)
	}
	if mtd, err := e.log(1.0, nov).MonthToDate(); err != nil || mtd != 0.8999999999999999 {
		t.Errorf("mtd %v %v", mtd, err)
	}
}
