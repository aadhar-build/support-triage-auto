package policy

import (
	"reflect"
	"testing"
)

func mustData(t *testing.T, s string) Value {
	t.Helper()
	v, err := ParseJSON([]byte(s))
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func mustRules(t *testing.T, s string) []Rule {
	t.Helper()
	rules, err := RulesFromData(mustData(t, s), "mem")
	if err != nil {
		t.Fatal(err)
	}
	return rules
}

// Expected values are literals from the Python oracle (triage.policy_check.check).
func TestCheckExamples(t *testing.T) {
	rules := mustRules(t, `{"public_reply_rules":[
		{"id":"p","type":"banned_phrase","phrases":["full refund","x ","IZMIR"]},
		{"id":"c","type":"pii_echo","detect":["card_number","email_from_customer","phone_from_customer"]}]}`)
	cust := []string{"call 555 123 4567, bo@ex.com"}
	cases := []struct {
		draft string
		want  []Violation
	}{
		{"A FULL refund now", []Violation{{"p", 2, 13, "FULL refund"}}},
		{"é full refund", []Violation{{"p", 2, 13, "full refund"}}},
		{"full refunds", []Violation{}},
		{"x  a", []Violation{{"p", 0, 2, "x "}}},
		{"izmir İzmir", []Violation{{"p", 0, 5, "izmir"}, {"p", 6, 11, "İzmir"}}},
		{"é 4111 1111 1111 1111", []Violation{{"c", 2, 21, "4111 1111 1111 1111"}}},
		{"a4111111111111111", []Violation{}},
		{"mail Bo@Ex.com or 555-123-4567", []Violation{{"c", 5, 14, "Bo@Ex.com"}, {"c", 18, 30, "555-123-4567"}}},
	}
	for _, c := range cases {
		got := Check(c.draft, cust, rules)
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("Check(%q) = %#v, want %#v", c.draft, got, c.want)
		}
		if safe := IsSafeToSend(c.draft, cust, rules); safe != (len(c.want) == 0) {
			t.Errorf("IsSafeToSend(%q) = %v", c.draft, safe)
		}
	}
}

func TestCurlyApostropheKeptInText(t *testing.T) {
	rules := mustRules(t, `{"public_reply_rules":[{"id":"w","type":"banned_phrase","phrases":["we'll refund"]}]}`)
	got := Check("Yes, We’ll  refund it", nil, rules)
	want := []Violation{{"w", 5, 18, "We’ll  refund"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %#v, want %#v", got, want)
	}
}

func TestNoRulesNoViolations(t *testing.T) {
	if got := Check("full refund 4111 1111 1111 1111", nil, nil); got == nil || len(got) != 0 {
		t.Errorf("got %#v, want an empty non-nil slice", got)
	}
}

func TestMatchedTopicsExample(t *testing.T) {
	topics, err := TopicsFromData(mustData(t, `{"escalate_always":[
		{"id":"t-a","phrases":["legal action"]},{"id":"t-b","phrases":["ıd"]}]}`), "mem")
	if err != nil {
		t.Fatal(err)
	}
	got := MatchedTopics([]string{"I will take LEGAL action", "ID"}, topics)
	if want := []string{"t-a", "t-b"}; !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestCompileOncePerRuleset(t *testing.T) {
	data := mustData(t, `{"public_reply_rules":[{"id":"p","type":"banned_phrase","phrases":["full refund","x "]},
		{"id":"c","type":"pii_echo","detect":["card_number"]}],
		"escalate_always":[{"id":"t-a","phrases":["legal action","lawyer"]}]}`)
	atLoad := compiled.Load()
	rules, err := RulesFromData(data, "mem")
	if err != nil {
		t.Fatal(err)
	}
	topics, err := TopicsFromData(data, "mem")
	if err != nil {
		t.Fatal(err)
	}
	before := compiled.Load()
	if before-atLoad != 4 {
		t.Errorf("loading compiled %d patterns, want 4 (one per phrase)", before-atLoad)
	}
	for i := 0; i < 100; i++ {
		Check("a full refund for x  y, 4111 1111 1111 1111", []string{"my lawyer"}, rules)
		MatchedTopics([]string{"legal action now"}, topics)
	}
	if after := compiled.Load(); after != before {
		t.Errorf("100 calls compiled %d patterns, want 0", after-before)
	}
}

// A Rule built by hand (not by RulesFromData) is enforced from its Phrases,
// as Python's check compiles rule.phrases on every call. It must never fail
// open. Expected values are literals from the Python oracle.
func TestHandBuiltRuleIsEnforced(t *testing.T) {
	rules := []Rule{{ID: "p", Type: "banned_phrase", Phrases: []string{"full refund"}}}
	want := []Violation{{"p", 2, 13, "FULL refund"}}
	if got := Check("A FULL refund now", nil, rules); !reflect.DeepEqual(got, want) {
		t.Errorf("Check = %#v, want %#v", got, want)
	}
	if IsSafeToSend("A FULL refund now", nil, rules) {
		t.Error("IsSafeToSend = true for a hand-built banned phrase; want false")
	}
}

// Changing a loaded rule's Phrases changes what Check enforces.
func TestEditedPhrasesAreEnforced(t *testing.T) {
	rules := mustRules(t, `{"public_reply_rules":[{"id":"p","type":"banned_phrase","phrases":["full refund"]}]}`)
	rules[0].Phrases[0] = "lawyer"
	if got := Check("A FULL refund now", nil, rules); len(got) != 0 {
		t.Errorf("old phrase still enforced: %#v", got)
	}
	want := []Violation{{"p", 3, 9, "lawyer"}}
	if got := Check("my lawyer", nil, rules); !reflect.DeepEqual(got, want) {
		t.Errorf("Check = %#v, want %#v", got, want)
	}
}
