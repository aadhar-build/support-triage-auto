package policy

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func runBrandRules(t *testing.T, args ...string) (string, string, int) {
	t.Helper()
	var out, errb bytes.Buffer
	code := BrandRulesMain(args, &out, &errb)
	return out.String(), errb.String(), code
}

func TestPythonEquality(t *testing.T) {
	parse := func(s string) Value {
		v, err := ParseJSON([]byte(s))
		if err != nil {
			t.Fatalf("%s: %v", s, err)
		}
		return v
	}
	// Python 3.12: json.loads(a) == json.loads(b).
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"100000000000000000000", "100000000000000000000", true},
		{"100000000000000000001", "100000000000000000000", false},
		{"100000000000000000000", "1e20", true},
		{"9007199254740993", "9007199254740992.0", false},
		{"9007199254740992", "9007199254740992.0", true},
		{"true", "1", true},
		{"true", "1.0", true},
		{"false", "0", true},
		{"true", "2", false},
		{"-0.0", "0", true},
		{"1e400", "2e400", true},
		{"1e400", "-1e400", false},
		{"1e400", "1", false},
		{"1", "1.0", true},
		{`{"a": 1, "b": {"c": [1, 2]}}`, `{"b": {"c": [1.0, 2]}, "a": true}`, true},
		{`{"a": 1}`, `{"a": 1, "b": null}`, false},
		{`null`, `null`, true},
		{`null`, `0`, false},
		{`"1"`, `1`, false},
		{`[1, 2]`, `[2, 1]`, false},
		{`"aé"`, `"aé"`, false},
	} {
		if got := pyEqual(parse(c.a), parse(c.b)); got != c.want {
			t.Errorf("%s == %s: got %v, want %v", c.a, c.b, got, c.want)
		}
	}
}

func TestConformanceCLI(t *testing.T) {
	atRepoRoot(t)
	const pair = "tests/fixtures/brand_rules/"
	const mine = "testdata/brand_rules/"
	cases := []struct {
		name           string
		args           []string
		stdout, stderr string
		code           int
	}{
		{"ok", []string{"--conformance", "--brand", "same", "--rules", pair + "rules.json", "--brands-dir", pair + "brands"},
			"conformance: ok brand=same\n", "", 0},
		{"differ", []string{"--conformance", "--brand=same", "--rules=" + pair + "rules_other_version.json", "--brands-dir", pair + "brands"},
			"conformance: differ brand=same keys=version\n", "", 1},
		{"badrule", []string{"--conformance", "--brand", "badrule", "--rules", mine + "numeq_rules.json", "--brands-dir", mine + "brands"},
			"", "error: badrule: 'public_reply_rules' is empty; at least one rule is required\n", 2},
		{"badrepeat", []string{"--conformance", "--brand", "badrepeat", "--rules", mine + "numeq_rules.json", "--brands-dir", mine + "brands"},
			"", "error: repeat_contact rule in badrepeat: 'min_conversations' must be an int of at least 2\n", 2},
		{"numeq", []string{"--conformance", "--brand", "numeq", "--rules", mine + "numeq_rules.json", "--brands-dir", mine + "brands"},
			"conformance: ok brand=numeq\n", "", 0},
		{"numdiff", []string{"--conformance", "--brand", "numdiff", "--rules", mine + "numdiff_rules.json", "--brands-dir", mine + "brands"},
			"conformance: differ brand=numdiff keys=meta,x\n", "", 1},
		{"brand error wins", []string{"--conformance", "--brand", "gamma", "--rules", mine + "missing.json", "--brands-dir", "testdata/brand_rules/nope"},
			"", "error: brand 'gamma' not found under " + mustReal(t, ".") + "/testdata/brand_rules/nope\n", 2},
		{"missing conformance", []string{"--brand", "same"},
			"", "error: the following arguments are required: --conformance\n", 2},
		{"unknown args", []string{"--conformance", "--bogus", "x"},
			"", "error: unrecognized arguments: --bogus x\n", 2},
		{"no abbreviation", []string{"--conf"},
			"", "error: the following arguments are required: --conformance\n", 2},
		{"missing value", []string{"--conformance", "--brand"},
			"", "error: argument --brand: expected one argument\n", 2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			out, errOut, code := runBrandRules(t, c.args...)
			if out != c.stdout || errOut != c.stderr || code != c.code {
				t.Errorf("got (%q, %q, %d), want (%q, %q, %d)", out, errOut, code, c.stdout, c.stderr, c.code)
			}
		})
	}
	t.Run("differ prints no values", func(t *testing.T) {
		out, errOut, _ := runBrandRules(t, "--conformance", "--brand", "same", "--rules", pair+"rules_other_version.json", "--brands-dir", pair+"brands")
		if all := out + errOut; strings.Contains(all, "fixture-s") || strings.Contains(all, "snorkelbar") {
			t.Errorf("output leaks values: %q", all)
		}
	})
	t.Run("broken", func(t *testing.T) {
		out, errOut, code := runBrandRules(t, "--conformance", "--brand", "broken", "--rules", pair+"rules.json", "--brands-dir", pair+"brands")
		if out != "" || code != 2 || !strings.HasPrefix(errOut, "error: ") || strings.Count(errOut, "\n") != 1 {
			t.Errorf("got (%q, %q, %d)", out, errOut, code)
		}
	})
	for _, h := range []string{"-h", "--help"} {
		out, errOut, code := runBrandRules(t, h)
		if !strings.HasPrefix(out, "usage: triage brand-rules") || strings.Count(out, "\n") != 1 || errOut != "" || code != 0 {
			t.Errorf("%s: got (%q, %q, %d)", h, out, errOut, code)
		}
	}
}

func mustReal(t *testing.T, p string) string {
	t.Helper()
	r, err := filepath.EvalSymlinks(p)
	if err != nil {
		t.Fatal(err)
	}
	r, err = filepath.Abs(r)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestBrandBlocksMatchGolden(t *testing.T) {
	atRepoRoot(t)
	for _, b := range brandSources {
		got, _, err := brandObject(b.md)
		if err != nil {
			t.Fatalf("%s: %v", b.md, err)
		}
		raw, err := os.ReadFile(filepath.Join("testdata", "golden", "policy", "rules_data", b.data+".json"))
		if err != nil {
			t.Fatal(err)
		}
		want, err := ParseJSON(raw)
		if err != nil {
			t.Fatal(err)
		}
		if !pyEqual(got, want) {
			t.Errorf("%s: rules block differs from Python's extraction", b.md)
		}
	}
}

// normalizeConformance applies the golden's rewrites: the repo realpath becomes
// <repo>, and the OS or parser detail of the four bracketed phrases becomes
// (<detail>) (#67 Q1 and the owner's Q1 decision). Nothing else is rewritten.
func normalizeConformance(s, repo string) string {
	s = strings.ReplaceAll(s, repo, "<repo>")
	lines := strings.Split(s, "\n")
	for i, l := range lines {
		for _, p := range []string{"cannot read rules file (", "not valid JSON (", "cannot read brand file (", "rules block is not valid JSON ("} {
			at := strings.Index(l, p)
			end := strings.LastIndex(l, ")")
			if at >= 0 && end > at+len(p)-1 {
				lines[i] = l[:at+len(p)-1] + "(<detail>)" + l[end+1:]
				break
			}
		}
	}
	return strings.Join(lines, "\n")
}

func TestConformanceGolden(t *testing.T) {
	root := atRepoRoot(t)
	repo := mustReal(t, root)
	raw, err := os.ReadFile("testdata/golden/brand_rules/conformance.txt")
	if err != nil {
		t.Fatal(err)
	}
	type goldenCase struct {
		name           string
		args           []string
		exit           string
		stdout, stderr string
	}
	var cases []goldenCase
	var c *goldenCase
	for _, l := range strings.Split(strings.TrimSuffix(string(raw), "\n"), "\n") {
		key, val, _ := strings.Cut(l, " ")
		switch {
		case key == "case":
			cases = append(cases, goldenCase{name: val})
			c = &cases[len(cases)-1]
		case c == nil:
			t.Fatalf("line before first case: %q", l)
		case key == "args":
			c.args = strings.Split(val, " ")
		case key == "exit":
			c.exit = val
		case key == "stdout":
			c.stdout += val + "\n"
		case key == "stderr":
			c.stderr += val + "\n"
		case key == "end":
			c = nil
		default:
			t.Fatalf("bad golden line %q", l)
		}
	}
	if len(cases) != 12 {
		t.Fatalf("golden has %d cases, want 12", len(cases))
	}
	for _, gc := range cases {
		t.Run(gc.name, func(t *testing.T) {
			out, errOut, code := runBrandRules(t, gc.args...)
			if got := fmt.Sprint(code); got != gc.exit {
				t.Errorf("exit %s, want %s", got, gc.exit)
			}
			if got := normalizeConformance(out, repo); got != gc.stdout {
				t.Errorf("stdout %q, want %q", got, gc.stdout)
			}
			if got, want := normalizeConformance(errOut, repo), normalizeConformance(gc.stderr, repo); got != want {
				t.Errorf("stderr %q, want %q", got, want)
			}
		})
	}
}
