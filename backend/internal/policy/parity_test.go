package policy

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// The rules sources, in the order scripts/make_golden.sh passes them to
// scripts/golden_policy.py.
var fixtureRules = []string{
	"policy_rules.json", "policy_rules_alt_topics.json", "brand_rules/rules.json",
	"brand_rules/rules_other_version.json", "admin/rules.json", "propose_change_rules.json",
	"propose_change_proposed_repeat.json", "propose_change_proposed_reply_rules.json",
	"propose_change_proposed_topic.json", "repeat_contact_rules.json",
}

// brand guidelines file -> its rules block, as Python extracted it.
var brandSources = []struct{ md, data string }{
	{"tests/fixtures/brands/alpha/guidelines.md", "alpha"},
	{"tests/fixtures/brands/alphaco/guidelines.md", "alphaco"},
	{"tests/fixtures/brands/beta/guidelines.md", "beta"},
	{"tests/fixtures/brand_rules/brands/same/guidelines.md", "same"},
	{"tests/fixtures/propose_change_brand_guidelines.md", "propose_change_brand_guidelines"},
	{"brands/amazonhelp/guidelines.md", "amazonhelp"},
}

// atRepoRoot moves the test to the repo root, so loader messages carry the
// same repo-relative paths as Python's.
func atRepoRoot(t *testing.T) string {
	t.Helper()
	root := parity.RepoRoot(t)
	t.Chdir(root)
	return root
}

func ruleFiles(t *testing.T) []string {
	t.Helper()
	srcs := []string{"policies/rules.json"}
	for _, f := range fixtureRules {
		srcs = append(srcs, "tests/fixtures/"+f)
	}
	bad, err := os.ReadDir("testdata/policy/bad_rules")
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range bad {
		srcs = append(srcs, "testdata/policy/bad_rules/"+e.Name())
	}
	return append(srcs, "testdata/policy/missing_rules.json",
		"./tests/fixtures/policy_rules.json", "tests//fixtures/policy_rules.json")
}

var detailRE = regexp.MustCompile(`(?s)(: (?:cannot read rules file|not valid JSON) )\(.*\z`)

func errObj(err error) map[string]any {
	return map[string]any{"error": detailRE.ReplaceAllString(err.Error(), "${1}(<detail>)")}
}

func rulesObj(rules []Rule) []any {
	out := []any{}
	for _, r := range rules {
		out = append(out, map[string]any{"id": r.ID, "type": r.Type, "phrases": r.Phrases, "detect": r.Detect})
	}
	return out
}

func topicsObj(topics []Topic) []any {
	out := []any{}
	for _, tp := range topics {
		out = append(out, map[string]any{"id": tp.ID, "phrases": tp.Phrases})
	}
	return out
}

type ruleSet struct {
	source string
	rules  []Rule
}

type topicSet struct {
	source string
	topics []Topic
}

// loadAll loads every source as golden_policy.py does and returns the
// loaders.jsonl lines plus the rule and topic sets that loaded.
func loadAll(t *testing.T) ([]map[string]any, []ruleSet, []topicSet) {
	t.Helper()
	var lines []map[string]any
	var rs []ruleSet
	var ts []topicSet
	add := func(line map[string]any, src string, rules []Rule, rerr error, topics []Topic, terr error) {
		if rerr != nil {
			var re *RulesError
			if !errors.As(rerr, &re) {
				t.Fatalf("%s: %T is not a *RulesError", src, rerr)
			}
			line["rules"] = errObj(rerr)
		} else {
			line["rules"] = map[string]any{"value": rulesObj(rules)}
			rs = append(rs, ruleSet{src, rules})
		}
		if terr != nil {
			line["topics"] = errObj(terr)
		} else {
			line["topics"] = map[string]any{"value": topicsObj(topics)}
			ts = append(ts, topicSet{src, topics})
		}
		lines = append(lines, line)
	}
	for _, src := range ruleFiles(t) {
		rules, rerr := LoadRules(src)
		topics, terr := LoadEscalateTopics(src)
		line := map[string]any{"kind": "file", "source": src}
		switch v, ok, err := RulesVersion(src); {
		case err != nil:
			line["rules_version"] = errObj(err)
		case ok:
			line["rules_version"] = map[string]any{"value": v}
		default:
			line["rules_version"] = map[string]any{"value": nil}
		}
		add(line, src, rules, rerr, topics, terr)
	}
	for _, b := range brandSources {
		raw, err := os.ReadFile(filepath.Join("testdata", "golden", "policy", "rules_data", b.data+".json"))
		if err != nil {
			t.Fatal(err)
		}
		data, err := ParseJSON(raw)
		if err != nil {
			t.Fatalf("%s: %v", b.data, err)
		}
		rules, rerr := RulesFromData(data, b.md)
		topics, terr := TopicsFromData(data, b.md)
		add(map[string]any{"kind": "data", "source": b.md}, b.md, rules, rerr, topics, terr)
	}
	return lines, rs, ts
}

func violationsObj(vs []Violation) []any {
	out := []any{}
	for _, v := range vs {
		out = append(out, []any{v.RuleID, v.Start, v.End, v.Text})
	}
	return out
}

type synCase struct {
	ID    string `json:"id"`
	Turns []struct {
		Inbound bool   `json:"inbound"`
		Text    string `json:"text"`
	} `json:"turns"`
	Draft *string `json:"draft_reply"`
}

func synCases(t *testing.T) []synCase {
	t.Helper()
	_, _, casesJSON := parity.Inputs(t)
	raw, err := os.ReadFile(casesJSON)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []synCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	return doc.Cases
}

func (c synCase) inbound() []string {
	out := []string{}
	for _, t := range c.Turns {
		if t.Inbound {
			out = append(out, t.Text)
		}
	}
	return out
}

// goldenLines decodes each line of a golden JSONL file (only to read the
// inputs it records).
func goldenLines[T any](t *testing.T, name string) []T {
	t.Helper()
	raw, err := os.ReadFile(parity.GoldenPath(t, name))
	if err != nil {
		t.Fatal(err)
	}
	var out []T
	sc := bufio.NewScanner(bytes.NewReader(raw))
	sc.Buffer(nil, 1<<24)
	for sc.Scan() {
		var v T
		if err := json.Unmarshal(sc.Bytes(), &v); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		out = append(out, v)
	}
	return out
}

type edgeCases struct {
	Check []struct {
		ID            string   `json:"id"`
		Draft         string   `json:"draft"`
		CustomerTurns []string `json:"customer_turns"`
	} `json:"check"`
	Topics []struct {
		ID            string   `json:"id"`
		CustomerTurns []string `json:"customer_turns"`
	} `json:"topics"`
	Quote []struct {
		ID    string `json:"id"`
		Turns []struct {
			Inbound bool   `json:"inbound"`
			Text    string `json:"text"`
		} `json:"turns"`
	} `json:"quote"`
}

func loadEdge(t *testing.T) (edgeCases, []Rule, []Topic) {
	t.Helper()
	var e edgeCases
	raw, err := os.ReadFile("testdata/policy/edge_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &e); err != nil {
		t.Fatal(err)
	}
	rules, err := LoadRules("testdata/policy/edge_rules.json")
	if err != nil {
		t.Fatal(err)
	}
	topics, err := LoadEscalateTopics("testdata/policy/edge_rules.json")
	if err != nil {
		t.Fatal(err)
	}
	return e, rules, topics
}

type threadLine struct {
	Root          int64    `json:"root"`
	CustomerTurns []string `json:"customer_turns"`
	Drafts        []struct {
		Draft string `json:"draft"`
	} `json:"drafts"`
	Turns [][2]any `json:"turns"`
}

func TestLoadersGolden(t *testing.T) {
	atRepoRoot(t)
	lines, _, _ := loadAll(t)
	parity.AssertGolden(t, "policy/loaders.jsonl", jsonLines(lines))
}

func TestCheckGolden(t *testing.T) {
	atRepoRoot(t)
	_, rs, _ := loadAll(t)
	var syn []map[string]any
	for _, c := range synCases(t) {
		if c.Draft == nil {
			continue
		}
		for _, r := range rs {
			syn = append(syn, map[string]any{"case": c.ID, "source": r.source,
				"violations": violationsObj(Check(*c.Draft, c.inbound(), r.rules)),
				"safe":       IsSafeToSend(*c.Draft, c.inbound(), r.rules)})
		}
	}
	parity.AssertGolden(t, "policy/check_synthetic.jsonl", jsonLines(syn))

	live, err := LoadRules("policies/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	var threads []map[string]any
	for _, th := range goldenLines[threadLine](t, "policy/check_threads.jsonl") {
		drafts := []any{}
		for _, d := range th.Drafts {
			drafts = append(drafts, map[string]any{"draft": d.Draft,
				"violations": violationsObj(Check(d.Draft, th.CustomerTurns, live)),
				"safe":       IsSafeToSend(d.Draft, th.CustomerTurns, live)})
		}
		threads = append(threads, map[string]any{"root": th.Root, "customer_turns": th.CustomerTurns, "drafts": drafts})
	}
	parity.AssertGolden(t, "policy/check_threads.jsonl", jsonLines(threads))

	edge, rules, _ := loadEdge(t)
	var lines []map[string]any
	for _, c := range edge.Check {
		lines = append(lines, map[string]any{"case": c.ID,
			"violations": violationsObj(Check(c.Draft, c.CustomerTurns, rules)),
			"safe":       IsSafeToSend(c.Draft, c.CustomerTurns, rules)})
	}
	parity.AssertGolden(t, "policy/check_edge.jsonl", jsonLines(lines))
}

func TestTopicsGolden(t *testing.T) {
	atRepoRoot(t)
	_, _, ts := loadAll(t)
	var syn []map[string]any
	for _, c := range synCases(t) {
		for _, s := range ts {
			syn = append(syn, map[string]any{"case": c.ID, "source": s.source,
				"topics": MatchedTopics(c.inbound(), s.topics)})
		}
	}
	parity.AssertGolden(t, "policy/topics_synthetic.jsonl", jsonLines(syn))

	live, err := LoadEscalateTopics("policies/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	var threads []map[string]any
	for _, th := range goldenLines[threadLine](t, "policy/topics_threads.jsonl") {
		threads = append(threads, map[string]any{"root": th.Root, "customer_turns": th.CustomerTurns,
			"topics": MatchedTopics(th.CustomerTurns, live)})
	}
	parity.AssertGolden(t, "policy/topics_threads.jsonl", jsonLines(threads))

	edge, _, topics := loadEdge(t)
	var lines []map[string]any
	for _, c := range edge.Topics {
		lines = append(lines, map[string]any{"case": c.ID, "topics": MatchedTopics(c.CustomerTurns, topics)})
	}
	parity.AssertGolden(t, "policy/topics_edge.jsonl", jsonLines(lines))
}

func quoteObj(key string, id any, turns []Turn) map[string]any {
	pairs := []any{}
	for _, tn := range turns {
		pairs = append(pairs, []any{tn.Inbound, tn.Text})
	}
	line := map[string]any{key: id, "turns": pairs}
	if q, err := QuoteConversation(turns); err != nil {
		line["error"] = err.Error()
	} else {
		line["quoted"] = q
	}
	return line
}

func TestQuoteGolden(t *testing.T) {
	atRepoRoot(t)
	var syn []map[string]any
	for _, c := range synCases(t) {
		var turns []Turn
		for _, tn := range c.Turns {
			turns = append(turns, Turn{tn.Inbound, tn.Text})
		}
		syn = append(syn, quoteObj("case", c.ID, turns))
	}
	parity.AssertGolden(t, "policy/quote_synthetic.jsonl", jsonLines(syn))

	var threads []map[string]any
	for _, th := range goldenLines[threadLine](t, "policy/quote_threads.jsonl") {
		var turns []Turn
		for _, p := range th.Turns {
			turns = append(turns, Turn{p[0].(bool), p[1].(string)})
		}
		threads = append(threads, quoteObj("root", th.Root, turns))
	}
	parity.AssertGolden(t, "policy/quote_threads.jsonl", jsonLines(threads))

	edge, _, _ := loadEdge(t)
	var lines []map[string]any
	for _, c := range edge.Quote {
		var turns []Turn
		for _, tn := range c.Turns {
			turns = append(turns, Turn{tn.Inbound, tn.Text})
		}
		lines = append(lines, quoteObj("case", c.ID, turns))
	}
	parity.AssertGolden(t, "policy/quote_edge.jsonl", jsonLines(lines))
}

func TestPolicyGoldenRefusesRawData(t *testing.T) {
	root := atRepoRoot(t)
	if _, err := exec.LookPath("python3"); err != nil {
		t.Fatalf("python3 is required: %v", err)
	}
	out := t.TempDir()
	cmd := exec.Command("python3", filepath.Join(root, "scripts", "golden_policy.py"),
		"--out", out, "--cases", "evals/gold/synthetic.json", "--twcs", "data/raw/x.csv",
		"--edge-rules", "testdata/policy/edge_rules.json", "--edge-cases", "testdata/policy/edge_cases.json",
		"--rules", "policies/rules.json")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	err := cmd.Run()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 2 {
		t.Fatalf("exit %v, want 2", err)
	}
	if s := stderr.String(); strings.Count(s, "\n") != 1 || !strings.HasSuffix(s, "\n") || !strings.Contains(s, "--twcs") {
		t.Errorf("stderr %q, want one line naming --twcs", s)
	}
	if entries, _ := os.ReadDir(out); len(entries) != 0 {
		t.Errorf("--out holds %d entries after a refusal", len(entries))
	}
}

// Owner decision Q2: Go refuses what Python's json accepts beyond RFC 8259.
func TestRefusesNonStandardJSON(t *testing.T) {
	atRepoRoot(t)
	for _, name := range []string{"nan.json", "infinity.json", "neg_infinity.json", "lone_surrogate.json"} {
		p := "testdata/policy/nonstandard_json/" + name
		for what, err := range map[string]error{
			"LoadRules":          func() error { _, err := LoadRules(p); return err }(),
			"LoadEscalateTopics": func() error { _, err := LoadEscalateTopics(p); return err }(),
			"RulesVersion":       func() error { _, _, err := RulesVersion(p); return err }(),
		} {
			var re *RulesError
			if !errors.As(err, &re) || !strings.HasPrefix(err.Error(), p+": not valid JSON (") {
				t.Errorf("%s(%s) = %v, want %s: not valid JSON (...)", what, name, err, p)
			}
		}
	}
}
