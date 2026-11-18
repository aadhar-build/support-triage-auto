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


CARD_ONLY = (Rule("t-card", "pii_echo", (), ("card_number",)),)


class TestCardNumber(unittest.TestCase):
    def assertFlaggedWhole(self, number):
        draft = "Card " + number + " on file"
        self.assertEqual(check(draft, [], CARD_ONLY),
                         [Violation("t-card", 5, 5 + len(number), number)], number)

    def test_luhn_valid_flagged_plain_spaced_dashed(self):
        for number in ("4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
                       "378282246310005", "4000 0000 0000 0000 006"):
            with self.subTest(number):
                self.assertFlaggedWhole(number)
        self.assertEqual(check("Card 4111 1111 1111 1111 on file", [], CARD_ONLY),
                         [Violation("t-card", 5, 24, "4111 1111 1111 1111")])

    def test_flagged_whether_or_not_customer_posted_it(self):
        draft = "Is 4111111111111111 yours?"
        expected = [Violation("t-card", 3, 19, "4111111111111111")]
        self.assertEqual(check(draft, [], CARD_ONLY), expected)
        self.assertEqual(check(draft, ["my card is 4111111111111111"], CARD_ONLY), expected)

    def test_length_bounds(self):
        self.assertFlaggedWhole("4222222222222")              # 13 digits
        self.assertFlaggedWhole("4000 0000 0000 0000 006")    # 19 digits
        for number in ("400000000002", "40000000000000000002"):  # 12 and 20, Luhn-valid
            with self.subTest(number):
                self.assertEqual(check("Card " + number + " on file", [], CARD_ONLY), [])

    def test_luhn_invalid_not_flagged(self):
        for number in ("4111 1111 1111 1112", "4000 0000 0000 0000 007", "4111111111111112"):
            with self.subTest(number):
                self.assertEqual(check("Card " + number + " on file", [], CARD_ONLY), [])

    def test_run_ends_at_double_space_or_other_separator(self):
        self.assertEqual(check("Ref 4111  1111 1111 1111", [], CARD_ONLY), [])
        self.assertEqual(check("Ref 4111/1111/1111/1111", [], CARD_ONLY), [])

    def test_letter_touching_run_not_flagged(self):
        self.assertEqual(check("Order A4111111111111111", [], CARD_ONLY), [])


class TestEmailEcho(unittest.TestCase):
    CUSTOMER = ["reach me at jo.bloggs@example.com"]

    def test_customer_email_echo_flagged_case_insensitive(self):
        draft = "Thanks JO.Bloggs@Example.com, or write to help@brand.example."
        self.assertEqual(check(draft, self.CUSTOMER, RULES),
                         [Violation("t-pii", 7, 28, "JO.Bloggs@Example.com")])

    def test_brand_email_not_flagged(self):
        draft = "Please write to help@brand.example."
        self.assertEqual(check(draft, self.CUSTOMER, RULES), [])
        # The address stops before the trailing dot, so it still matches when the customer posts it.
        self.assertEqual(check(draft, ["I wrote to help@brand.example already"], RULES),
                         [Violation("t-pii", 16, 34, "help@brand.example")])

    def test_digits_inside_email_are_not_card_or_phone(self):
        customer = ["call 4111111111111111 or mail 4111111111111111@example.com"]
        draft = "We saw 4111111111111111@example.org"
        self.assertEqual(check(draft, customer, RULES), [])


if __name__ == "__main__":
    unittest.main()
