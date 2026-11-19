package policy

import (
	"errors"
	"os"
	"strings"
	"testing"
)

// Expected messages are literals from the Python oracle (triage.policy_check).
func TestLoadersFailClosed(t *testing.T) {
	t.Chdir(t.TempDir())
	files := map[string]string{
		"bad_utf8.json":    "\xff{}",
		"bad.json":         "{not json",
		"list.json":        "[1]",
		"norules.json":     `{"version":"x"}`,
		"empty.json":       `{"public_reply_rules":[]}`,
		"notopics.json":    `{"public_reply_rules":[{"id":"p","type":"banned_phrase","phrases":["x"]}]}`,
		"emptytopics.json": `{"public_reply_rules":[{"id":"p","type":"banned_phrase","phrases":["x"]}],"escalate_always":[]}`,
		"blank.json":       "  \n",
	}
	for name, body := range files {
		if err := os.WriteFile(name, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	cases := []struct {
		path, rules, topics string // "" = loads
	}{
		{"missing.json", "missing.json: cannot read rules file (", "missing.json: cannot read rules file ("},
		{"bad_utf8.json", "bad_utf8.json: cannot read rules file (", "bad_utf8.json: cannot read rules file ("},
		{"bad.json", "bad.json: not valid JSON (", "bad.json: not valid JSON ("},
		{"blank.json", "blank.json: not valid JSON (", "blank.json: not valid JSON ("},
		{"list.json", "list.json: top level must be a JSON object", "list.json: top level must be a JSON object"},
		{"norules.json", "norules.json: 'public_reply_rules' is missing or is not a list", "norules.json: 'escalate_always' is missing or is not a list"},
		{"empty.json", "empty.json: 'public_reply_rules' is empty; at least one rule is required", "empty.json: 'escalate_always' is missing or is not a list"},
		{"notopics.json", "", "notopics.json: 'escalate_always' is missing or is not a list"},
		{"emptytopics.json", "", "emptytopics.json: 'escalate_always' is empty; at least one topic is required"},
	}
	check := func(t *testing.T, what string, n int, err error, want string) {
		t.Helper()
		if want == "" {
			if err != nil || n == 0 {
				t.Errorf("%s: got %d items, err %v; want a non-empty load", what, n, err)
			}
			return
		}
		var re *RulesError
		if !errors.As(err, &re) {
			t.Fatalf("%s: err %v (%T), want *RulesError", what, err, err)
		}
		if n != 0 {
			t.Errorf("%s: %d items returned with an error", what, n)
		}
		msg := err.Error()
		if strings.HasSuffix(want, "(") {
			if !strings.HasPrefix(msg, want) || !strings.HasSuffix(msg, ")") {
				t.Errorf("%s: %q, want prefix %q", what, msg, want)
			}
		} else if msg != want {
			t.Errorf("%s: %q, want %q", what, msg, want)
		}
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			rules, err := LoadRules(c.path)
			check(t, "LoadRules", len(rules), err, c.rules)
			topics, err := LoadEscalateTopics(c.path)
			check(t, "LoadEscalateTopics", len(topics), err, c.topics)
		})
	}
}

func TestRulesVersion(t *testing.T) {
	t.Chdir(t.TempDir())
	os.WriteFile("v.json", []byte(`{"version":"2024-01","escalate_always":[]}`), 0o644)
	os.WriteFile("n.json", []byte(`{"version":3}`), 0o644)
	if v, ok, err := RulesVersion("v.json"); v != "2024-01" || !ok || err != nil {
		t.Errorf("v.json: %q %v %v", v, ok, err)
	}
	if v, ok, err := RulesVersion("n.json"); v != "" || ok || err != nil {
		t.Errorf("n.json: %q %v %v", v, ok, err)
	}
	if _, _, err := RulesVersion("missing.json"); err == nil || !strings.HasPrefix(err.Error(), "missing.json: cannot read rules file (") {
		t.Errorf("missing.json: %v", err)
	}
}

func TestPythonRepr(t *testing.T) {
	data, err := ParseJSON([]byte(`{"public_reply_rules":[{"id":"o","type":["a",1.0,null,true,1e16,{"k":"it's"}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	_, err = RulesFromData(data, "mem")
	want := `mem: rule 'o' has type ['a', 1.0, None, True, 1e+16, {'k': "it's"}]; expected one of banned_phrase, pii_echo`
	if err == nil || err.Error() != want {
		t.Errorf("got %v\nwant %s", err, want)
	}
}
