package policy

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// isoUTC is Python's dt.astimezone(utc).isoformat() with "+00:00" as "Z".
func isoUTC(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 != 0 {
		return t.Format("2006-01-02T15:04:05.000000") + "Z"
	}
	return t.Format("2006-01-02T15:04:05") + "Z"
}

// parityThreads builds the parity-harness threads and the known brands (every
// thread's BrandIDs, as run_eval.py does).
func parityThreads(t *testing.T) ([]ingest.Thread, []string) {
	t.Helper()
	twcs, _, _ := parity.Inputs(t)
	var threads []ingest.Thread
	if _, err := ingest.BuildThreads(twcs, ingest.Options{TempDir: t.TempDir()}, func(th ingest.Thread) error {
		threads = append(threads, th)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	var known []string
	for _, th := range threads {
		for _, b := range th.BrandIDs {
			if !slices.Contains(known, b) {
				known = append(known, b)
			}
		}
	}
	slices.Sort(known)
	return threads, known
}

// repeatSources are the loader sources, in the order scripts/make_golden.sh
// passes them to scripts/golden_context_signals.py.
func repeatSources(t *testing.T) []string {
	t.Helper()
	srcs := []string{"policies/rules.json", "tests/fixtures/repeat_contact_rules.json",
		"tests/fixtures/propose_change_proposed_repeat.json"}
	bad, err := filepath.Glob("testdata/context_signals/bad_repeat/*.json")
	if err != nil || len(bad) == 0 {
		t.Fatalf("bad_repeat fixtures: %v", err)
	}
	slices.Sort(bad)
	return append(append(srcs, bad...), "testdata/context_signals/missing_rule.json")
}

var readDetailRE = regexp.MustCompile(`(?s)(: cannot read file )\(.*\z`)

func TestContextSignalsGolden(t *testing.T) {
	atRepoRoot(t)

	var loaders strings.Builder
	for _, src := range repeatSources(t) {
		r, err := LoadRepeatContactRule(src)
		if err != nil {
			var rce *RepeatContactError
			if !errors.As(err, &rce) {
				t.Fatalf("%s: %T is not a *RepeatContactError", src, err)
			}
			loaders.WriteString(`{"error":` + pyDumps(readDetailRE.ReplaceAllString(err.Error(), "${1}(<detail>)")) +
				`,"source":` + pyDumps(src) + "}\n")
			continue
		}
		loaders.WriteString(`{"source":` + pyDumps(src) + `,"value":{"effect":` + pyDumps(r.Effect) +
			`,"min_conversations":` + pyDumps(r.MinConversations) + `,"report_window_hours":` +
			r.ReportWindowHours.String() + `,"window_days":` + r.WindowDays.String() + "}}\n")
	}
	parity.AssertGolden(t, "context_signals/loaders.jsonl", []byte(loaders.String()))

	threads, known := parityThreads(t)
	ordinals := map[string]string{}
	var records []ContactRecord
	var recLines, attrLines []map[string]any
	for _, th := range threads {
		root := int64(th.RootID)
		r, ok := ContactRecordOf(th, known)
		if !ok {
			recLines = append(recLines, map[string]any{"root": root, "record": nil})
		} else {
			records = append(records, r)
			name, seen := ordinals[r.Customer]
			if !seen {
				name = "c" + itoa(len(ordinals))
				ordinals[r.Customer] = name
			}
			replies := []any{}
			for _, rp := range r.BrandReplies {
				replies = append(replies, []any{rp.Brand, isoUTC(rp.At)})
			}
			recLines = append(recLines, map[string]any{"root": root, "record": map[string]any{
				"customer": name, "start": isoUTC(r.Start), "brands_at_start": r.BrandsAtStart,
				"brand_replies": replies}})
		}
		attr := map[string]any{}
		for _, b := range append(slices.Clone(known), "NoSuchBrand") {
			attr[b] = BrandAttribution(th, b, known)
		}
		attrLines = append(attrLines, map[string]any{"root": root, "attribution": attr})
	}
	parity.AssertGolden(t, "context_signals/records.jsonl", jsonLines(recLines))
	parity.AssertGolden(t, "context_signals/attribution.jsonl", jsonLines(attrLines))

	var countLines []map[string]any
	for _, w := range []time.Duration{0, 72 * time.Hour, 7 * 24 * time.Hour} {
		counts := ContactCounts(records, w)
		roots := make([]uint64, 0, len(counts))
		for root := range counts {
			roots = append(roots, root)
		}
		slices.Sort(roots)
		for _, root := range roots {
			countLines = append(countLines, map[string]any{"window_seconds": int(w / time.Second),
				"root": int64(root), "count": counts[root]})
		}
	}
	parity.AssertGolden(t, "context_signals/counts.jsonl", jsonLines(countLines))
}

func itoa(n int) string { return pyDumps(n) }

// TestContextGoldensHoldNoCustomerIDs: no golden line holds the author id of
// any inbound turn.
func TestContextGoldensHoldNoCustomerIDs(t *testing.T) {
	threads, _ := parityThreads(t)
	customers := map[string]bool{}
	for _, th := range threads {
		for _, tn := range th.Turns {
			if tn.Inbound {
				customers[tn.AuthorID] = true
			}
		}
	}
	if len(customers) == 0 {
		t.Fatal("no customers in the parity threads")
	}
	dir := parity.GoldenPath(t, "context_signals")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		b, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		for i, line := range strings.Split(string(b), "\n") {
			for c := range customers {
				if strings.Contains(line, c) {
					t.Fatalf("%s line %d holds a customer id", e.Name(), i+1)
				}
			}
		}
	}
}

func TestContextGoldenRefusesRawData(t *testing.T) {
	root := atRepoRoot(t)
	if _, err := exec.LookPath("python3"); err != nil {
		t.Fatalf("python3 is required: %v", err)
	}
	out := t.TempDir()
	cmd := exec.Command("python3", filepath.Join(root, "scripts", "golden_context_signals.py"),
		"--out", out, "--twcs", "data/raw/x.csv", "--rules", "policies/rules.json")
	var stderr strings.Builder
	cmd.Stderr = &stderr
	err := cmd.Run()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 2 {
		t.Fatalf("exit %v, want 2", err)
	}
	if lines := strings.Split(strings.TrimSuffix(stderr.String(), "\n"), "\n"); len(lines) != 1 ||
		!strings.Contains(lines[0], "resolves under data/raw; refused") {
		t.Errorf("stderr %q, want one refusal line", stderr.String())
	}
	if entries, _ := os.ReadDir(out); len(entries) != 0 {
		t.Errorf("wrote %d entries to --out", len(entries))
	}
}
