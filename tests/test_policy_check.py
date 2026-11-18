"""Tests for triage.policy_check. All drafts and customer turns are made up by hand; no real tweets.

Card numbers are well-known test numbers or numbers built from zeros, not real cards.
The live policies/rules.json is loaded only as a schema smoke test, never as an oracle.
"""

import json
import tempfile
import unittest
from pathlib import Path

from triage.policy_check import Rule, RulesError, Violation, check, is_safe_to_send, load_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "policy_rules.json"


class TestLoadRules(unittest.TestCase):
    def test_loads_fixture(self):
        rules = load_rules(FIXTURE)
        self.assertEqual(rules, (
            Rule("t-promise", "banned_phrase", ("we'll refund", "full refund", "compensation"), ()),
            Rule("t-secret", "banned_phrase", ("cvv", "your password"), ()),
            Rule("t-pii", "pii_echo", (), ("card_number", "email_from_customer", "phone_from_customer")),
        ))
        self.assertEqual(load_rules(str(FIXTURE)), rules)

    def test_errors(self):
        phrase_rule = {"id": "p1", "type": "banned_phrase", "phrases": ["x"]}
        cases = [
            # (name, file text or None for a missing file, text the message must also name)
            ("missing file", None, None),
            ("invalid json", "{not json", None),
            ("no public_reply_rules", json.dumps({"version": "x"}), None),
            ("rule with no id", json.dumps({"public_reply_rules": [
                phrase_rule, {"type": "banned_phrase", "phrases": ["x"]}]}), "index 1"),
            ("duplicate id", json.dumps({"public_reply_rules": [phrase_rule, phrase_rule]}), "p1"),
            ("unknown type", json.dumps({"public_reply_rules": [
                {"id": "odd", "type": "regex", "phrases": ["x"]}]}), "odd"),
            ("missing phrases", json.dumps({"public_reply_rules": [
                {"id": "bp", "type": "banned_phrase"}]}), "bp"),
            ("empty phrases", json.dumps({"public_reply_rules": [
                {"id": "bp", "type": "banned_phrase", "phrases": []}]}), "bp"),
            ("missing detect", json.dumps({"public_reply_rules": [
                {"id": "pe", "type": "pii_echo"}]}), "pe"),
            ("unknown detect", json.dumps({"public_reply_rules": [
                {"id": "pe", "type": "pii_echo", "detect": ["card_number", "postcode"]}]}), "pe"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for i, (name, text, named) in enumerate(cases):
                with self.subTest(name):
                    path = Path(tmp) / ("rules_%d.json" % i)
                    if text is not None:
                        path.write_text(text, encoding="utf-8")
                    with self.assertRaises(RulesError) as ctx:
                        load_rules(path)
                    self.assertIsInstance(ctx.exception, ValueError)
                    message = str(ctx.exception)
                    self.assertIn(str(path), message)
                    if named is not None:
                        self.assertIn(named, message)

    def test_empty_rule_list_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"public_reply_rules": []}), encoding="utf-8")
            self.assertEqual(load_rules(path), ())

    def test_live_rules_file_loads(self):
        # Schema-compatibility smoke test only: no assertions about the owner's content.
        load_rules(REPO_ROOT / "policies" / "rules.json")


RULES = load_rules(FIXTURE)


class TestBannedPhrase(unittest.TestCase):
    def test_phrase_hit_has_id_and_span(self):
        self.assertEqual(check("Sorry! We'll refund you today.", [], RULES),
                         [Violation("t-promise", 7, 19, "We'll refund")])

    def test_each_occurrence_reported(self):
        draft = "No full refund. Really, no full refund."
        self.assertEqual(check(draft, [], RULES), [
            Violation("t-promise", 3, 14, "full refund"),
            Violation("t-promise", 27, 38, "full refund"),
        ])

    def test_case_insensitive(self):
        self.assertEqual(check("What is YOUR PASSWORD?", [], RULES),
                         [Violation("t-secret", 8, 21, "YOUR PASSWORD")])

    def test_word_boundaries(self):
        self.assertEqual(check("Send the cvvx code", [], RULES), [])
        self.assertEqual(check("Send the xcvv code", [], RULES), [])
        self.assertEqual(check("overcompensation", [], RULES), [])
        self.assertEqual(check("cvv please", [], RULES), [Violation("t-secret", 0, 3, "cvv")])
        self.assertEqual(check("tell us the cvv", [], RULES),
                         [Violation("t-secret", 12, 15, "cvv")])

    def test_curly_and_straight_apostrophes(self):
        self.assertEqual(check("we\u2019ll refund it", [], RULES),
                         [Violation("t-promise", 0, 12, "we\u2019ll refund")])
        curly_rule = (Rule("t-curly", "banned_phrase", ("we\u2019ll refund",), ()),)
        self.assertEqual(check("ok we'll refund", [], curly_rule),
                         [Violation("t-curly", 3, 15, "we'll refund")])

    def test_space_matches_any_whitespace_run(self):
        self.assertEqual(check("a full\n  refund", [], RULES),
                         [Violation("t-promise", 2, 15, "full\n  refund")])


if __name__ == "__main__":
    unittest.main()
