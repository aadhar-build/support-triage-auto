package policy

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func ruleIDs(rs []Rule) []string {
	var ids []string
	for _, r := range rs {
		ids = append(ids, r.ID)
	}
	return ids
}

func topicIDs(ts []Topic) []string {
	var ids []string
	for _, x := range ts {
		ids = append(ids, x.ID)
	}
	return ids
}

func mustLoadBrand(t *testing.T, path string) EffectiveRules {
	t.Helper()
	eff, err := LoadBrandFile(path)
	if err != nil {
		t.Fatalf("LoadBrandFile(%s): %v", path, err)
	}
	return eff
}

func TestLoadBrandFileFixtures(t *testing.T) {
	atRepoRoot(t)
	fromJSON, err := LoadEffective("tests/fixtures/brand_rules/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	same := mustLoadBrand(t, "tests/fixtures/brand_rules/brands/same/guidelines.md")
	if !fromJSON.Equal(same) || !same.Equal(fromJSON) {
		t.Errorf("rules.json and same/guidelines.md are not Equal")
	}
	if same.RepeatContact == nil {
		t.Errorf("same: repeat contact absent")
	}
	if same.Version == nil || *same.Version != "fixture-s" {
		t.Errorf("same: version = %v, want fixture-s", same.Version)
	}

	alpha := mustLoadBrand(t, "tests/fixtures/brands/alpha/guidelines.md")
	if alpha.RepeatContact != nil {
		t.Errorf("alpha: repeat contact = %+v, want absent", *alpha.RepeatContact)
	}
	if alpha.Equal(same) {
		t.Errorf("alpha Equal same")
	}

	beta := mustLoadBrand(t, "tests/fixtures/brands/beta/guidelines.md")
	if got := ruleIDs(beta.Rules); !slices.Equal(got, []string{"b-banned", "b-pii"}) {
		t.Errorf("beta rules = %v", got)
	}
	if got := topicIDs(beta.Topics); !slices.Equal(got, []string{"b-topic"}) {
		t.Errorf("beta topics = %v", got)
	}
	rc := beta.RepeatContact
	if rc == nil || rc.WindowDays.Lit != "7" || rc.MinConversations != 3 || rc.Effect != "escalate" || rc.ReportWindowHours.Lit != "72" {
		t.Errorf("beta repeat contact = %+v", rc)
	}
	if beta.Version == nil || *beta.Version != "fixture-b" || beta.Status == nil || *beta.Status != "made-up" {
		t.Errorf("beta version/status = %v/%v", beta.Version, beta.Status)
	}
}

const validBlock = `{"public_reply_rules": [{"id": "m-banned", "type": "banned_phrase", "phrases": ["flimflam"]}], "escalate_always": [{"id": "m-topic", "phrases": ["made-up topic"]}]}`

func brandText(block, prose string) string {
	return "# Made-up guidelines\n\n" + prose + "\n```rules\n" + block + "\n```\n"
}

func writeBrand(t *testing.T, dir, name, text string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func loadErr(t *testing.T, path string) string {
	t.Helper()
	_, err := LoadBrandFile(path)
	var re *RulesError
	if !errors.As(err, &re) {
		t.Fatalf("LoadBrandFile(%s) error = %v (%T), want *RulesError", path, err, err)
	}
	return err.Error()
}

func TestLoadBrandFileErrors(t *testing.T) {
	dir := t.TempDir()
	const prose = "Made-up prose.\n"
	cases := []struct{ name, text, want string }{
		{"no block", "# Made-up\n\nno rules here\n", "no ```rules block found"},
		{"two blocks", brandText(validBlock, prose) + "\n```rules\n" + validBlock + "\n```\n",
			"more than one ```rules block found (2); exactly one is allowed"},
		{"rules json fence", "# Made-up\n\n```rules json\n" + validBlock + "\n```\n",
			"malformed rules fence on line 3; it must be exactly ```rules"},
		{"unclosed", "# Made-up\n\n```rules\n" + validBlock + "\n", "unclosed ```rules block starting on line 3"},
		{"empty block", brandText("   \n", prose), "the ```rules block is empty"},
		{"invalid json", brandText("{\n\"public_reply_rules\": [\n", prose), "rules block is not valid JSON (line "},
		{"not an object", brandText("[1, 2]", prose), "rules block top level must be a JSON object"},
		{"empty rules", brandText(`{"public_reply_rules": [], "escalate_always": [{"id": "m-topic", "phrases": ["made-up topic"]}]}`, prose),
			"'public_reply_rules' is empty"},
		{"empty topics", brandText(`{"public_reply_rules": [{"id": "m-banned", "type": "banned_phrase", "phrases": ["flimflam"]}], "escalate_always": []}`, prose),
			"'escalate_always' is empty"},
		{"topic without phrases", brandText(`{"public_reply_rules": [{"id": "m-banned", "type": "banned_phrase", "phrases": ["flimflam"]}], "escalate_always": [{"id": "m-topic"}]}`, prose),
			"needs a non-empty 'phrases' list"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := writeBrand(t, dir, "guidelines.md", c.text)
			got := loadErr(t, p)
			if !strings.HasPrefix(got, p+": ") || !strings.Contains(got, c.want) {
				t.Errorf("got %q, want %q prefixed by the path", got, c.want)
			}
		})
	}
	t.Run("malformed repeat_contact", func(t *testing.T) {
		block := strings.TrimSuffix(validBlock, "}") + `, "repeat_contact": {"window_days": 7}}`
		p := writeBrand(t, dir, "guidelines.md", brandText(block, prose))
		want := "repeat_contact rule in " + p + ": 'report_window_hours' is missing"
		if got := loadErr(t, p); got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("unreadable", func(t *testing.T) {
		p := filepath.Join(dir, "nope.md")
		got := loadErr(t, p)
		if !strings.HasPrefix(got, p+": cannot read brand file (") {
			t.Errorf("got %q", got)
		}
	})
	t.Run("invalid UTF-8", func(t *testing.T) {
		p := writeBrand(t, dir, "bad.md", "```rules\n\xff\n```\n")
		if got, want := loadErr(t, p), p+": cannot read brand file (invalid UTF-8)"; got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	})
}

func TestInvalidJSONLineIsWithinBlock(t *testing.T) {
	p := writeBrand(t, t.TempDir(), "guidelines.md", "# t\n\nprose\n\nmore prose\n\n```rules\n{\n  \"a\": 1,\n  oops\n}\n```\n")
	got := loadErr(t, p)
	if !strings.HasPrefix(got, p+": rules block is not valid JSON (line 3 of the block: ") {
		t.Errorf("got %q", got)
	}
}

// Expected values are Python 3.12 results of load_brand_file on the same strings.
func TestBrandFileLineSplitting(t *testing.T) {
	dir := t.TempDir()
	ok := func(name, text string) {
		t.Run(name, func(t *testing.T) {
			p := writeBrand(t, dir, "g.md", text)
			eff, err := LoadBrandFile(p)
			if err != nil {
				t.Fatalf("got error %v, want ok", err)
			}
			if got := ruleIDs(eff.Rules); !slices.Equal(got, []string{"m-banned"}) {
				t.Errorf("rules = %v", got)
			}
		})
	}
	fails := func(name, text, want string) {
		t.Run(name, func(t *testing.T) {
			p := writeBrand(t, dir, "g.md", text)
			if got := loadErr(t, p); !strings.HasPrefix(got, p+": "+want) {
				t.Errorf("got %q, want prefix %q", got, want)
			}
		})
	}
	for _, ws := range []string{"\x1c", "\x1f", "\u0085", " ", "　"} {
		ok(fmt.Sprintf("open %q", ws), "```rules"+ws+"\n"+validBlock+"\n```\n")
		ok(fmt.Sprintf("close %q", ws), "```rules\n"+validBlock+"\n```"+ws+"\n")
	}
	fails("space before fence", " ```rules\n"+validBlock+"\n```\n", "no ```rules block found")
	for _, sep := range []string{"\r\n", "\r", "\v", "\f", "\x1c", "\u0085", " ", " "} {
		ok(fmt.Sprintf("separator %q", sep), "x"+sep+"```rules"+sep+validBlock+sep+"```"+sep)
	}
	fails("U+2028 inside a JSON string", "```rules\n"+strings.Replace(validBlock, "flimflam", "flim flam", 1)+"\n```\n",
		"rules block is not valid JSON (line 1 of the block: ")
	fails("leading BOM", "\ufeff```rules\n"+validBlock+"\n```\n", "no ```rules block found")
	fails("malformed line number", "a\r\nb\rc\vd\n```rules x\n", "malformed rules fence on line 5; it must be exactly ```rules")
	fails("unclosed line number", "a\r\nb\rc\vd\n```rules\n"+validBlock+"\n", "unclosed ```rules block starting on line 5")
}
