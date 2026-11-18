"""Tests for triage.context_signals. Every thread, customer and brand here is made up by hand.

Expected counts are literal values from the spec's worked examples, never recomputed.
"""

import ast
import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from triage.context_signals import (
    ContactRecord,
    RepeatContactError,
    RepeatContactRule,
    brand_attribution,
    contact_counts,
    contact_record,
    load_repeat_contact_rule,
    repeat_contact_from_data,
)
from triage.threads import Thread, Turn

DAY0 = datetime(2021, 3, 1, 9, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
KNOWN = {"ShopCo", "OtherBrand"}


def day(n, hours=0):
    return DAY0 + timedelta(days=n, hours=hours)


def real_thread(root_id, customer, start, text, replies=()):
    """A real-shaped thread: one inbound first turn, then (brand, time) reply turns."""
    turns = [Turn(root_id, customer, True, start, text)]
    for i, (brand, at) in enumerate(replies, start=1):
        turns.append(Turn(root_id + i, brand, False, at, "made-up brand reply"))
    return Thread(root_tweet_id=root_id, brand_ids=tuple(b for b, _ in replies),
                  turns=tuple(turns), brand_replied=bool(replies))


def rec(root_id, customer, start, brands=(), replies=()):
    return ContactRecord(root_tweet_id=root_id, customer=customer, start=start,
                         brands_at_start=frozenset(brands), brand_replies=tuple(replies))


class TestContactRecord(unittest.TestCase):
    def test_mentions_and_outbound_first(self):
        r = contact_record(real_thread(10, "cust_zed", day(0), "@shopco where is my parcel"),
                           KNOWN)
        self.assertEqual(r, ContactRecord(10, "cust_zed", day(0), frozenset({"ShopCo"}), ()))

        two = contact_record(real_thread(11, "cust_zed", day(0), "@SHOPCO and @otherbrand help"),
                             KNOWN)
        self.assertEqual(two.brands_at_start, frozenset({"ShopCo", "OtherBrand"}))

        # a longer handle is a different brand, not a prefix match
        self.assertIsNone(contact_record(real_thread(12, "c", day(0), "@ShopCoUK hello"), KNOWN))
        # no known mention and no known reply
        self.assertIsNone(contact_record(real_thread(13, "c", day(0), "@Unknown hello"), KNOWN))
        self.assertIsNone(contact_record(
            real_thread(14, "c", day(0), "hello", replies=(("Unknown", day(0, 1)),)), KNOWN))
        # first turn outbound
        brand_first = Thread(root_tweet_id=15, brand_ids=("ShopCo",), brand_replied=True,
                             turns=(Turn(15, "ShopCo", False, day(0), "@ShopCo news"),))
        self.assertIsNone(contact_record(brand_first, KNOWN))
        # no turns
        self.assertIsNone(contact_record(Thread(16, (), (), False), KNOWN))

        reply_only = contact_record(
            real_thread(17, "c", day(0), "hello", replies=(("ShopCo", day(0, 2)),)), KNOWN)
        self.assertEqual(reply_only.brands_at_start, frozenset())
        self.assertEqual(reply_only.brand_replies, (("ShopCo", day(0, 2)),))

    def test_start_is_utc(self):
        plus2 = timezone(timedelta(hours=2))
        r = contact_record(real_thread(1, "c", datetime(2021, 3, 1, 11, 0, tzinfo=plus2),
                                       "@ShopCo hi"), KNOWN)
        self.assertEqual(r.start, day(0))
        self.assertEqual(r.start.utcoffset(), timedelta(0))


class TestContactCounts(unittest.TestCase):
    def three(self):
        return [contact_record(real_thread(100 + d, "cust_amber", day(d),
                                           "@ShopCo where is my parcel"), {"ShopCo"})
                for d in (0, 2, 5)]

    def test_no_look_ahead(self):
        records = self.three()
        expected = {100: 1, 102: 2, 105: 3}
        self.assertEqual(contact_counts(records, WEEK), expected)
        for seed in range(5):
            shuffled = list(records)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(contact_counts(shuffled, WEEK), expected)
        self.assertEqual(contact_counts(records[:1], WEEK), {100: 1})

    def test_same_instant_tie_break(self):
        records = [rec(9, "c", day(0), {"ShopCo"}), rec(7, "c", day(0), {"ShopCo"}),
                   rec(8, "c", day(0), {"ShopCo"})]
        self.assertEqual(contact_counts(records, WEEK), {7: 1, 8: 2, 9: 3})

    def test_reply_only_record_counts_zero(self):
        records = [rec(1, "c", day(0), {"ShopCo"}),
                   rec(2, "c", day(1), (), (("ShopCo", day(1, 1)),))]
        self.assertEqual(contact_counts(records, WEEK), {1: 1, 2: 0})

    def test_brand_reply_only_if_known_by_then(self):
        x = rec(3, "c", day(2), {"ShopCo"})
        late = rec(1, "c", day(0), (), (("ShopCo", day(3)),))
        self.assertEqual(contact_counts([late, x], WEEK)[3], 1)
        early = rec(1, "c", day(0), (), (("ShopCo", day(1)),))
        self.assertEqual(contact_counts([early, x], WEEK)[3], 2)
        at_start = rec(1, "c", day(0), (), (("ShopCo", day(2)),))
        self.assertEqual(contact_counts([at_start, x], WEEK)[3], 2)

    def test_own_reply_never_attributes(self):
        x = rec(5, "c", day(0), (), (("ShopCo", day(0)),))
        self.assertEqual(contact_counts([x], WEEK), {5: 0})
        y = rec(6, "c", day(0), {"OtherBrand"}, (("ShopCo", day(0)),))
        self.assertEqual(contact_counts([y], WEEK), {6: 1})

    def test_window_and_keys(self):
        records = self.three() + [
            rec(109, "cust_amber", day(9), {"ShopCo"}),
            rec(120, "cust_amber", day(20), {"ShopCo"}),
            rec(201, "cust_amber", day(5, 1), {"OtherBrand"}),
            rec(301, "cust_birch", day(5, 2), {"ShopCo"}),
        ]
        counts = contact_counts(records, WEEK)
        self.assertEqual(counts, {100: 1, 102: 2, 105: 3, 109: 3, 120: 1, 201: 1, 301: 1})
        # both window ends inclusive: exactly 7 days back counts
        edge = [rec(1, "c", day(0), {"ShopCo"}), rec(2, "c", day(7), {"ShopCo"})]
        self.assertEqual(contact_counts(edge, WEEK), {1: 1, 2: 2})

    def test_multi_brand_mention(self):
        records = [rec(1, "c", day(0), {"ShopCo"}), rec(2, "c", day(1), {"ShopCo"}),
                   rec(3, "c", day(2), {"OtherBrand"}),
                   rec(4, "c", day(3), {"ShopCo", "OtherBrand"})]
        self.assertEqual(contact_counts(records, WEEK), {1: 1, 2: 2, 3: 1, 4: 3})

    def test_inputs_not_mutated(self):
        records = self.three()
        before = list(records)
        contact_counts(records, WEEK)
        self.assertEqual(records, before)
        self.assertEqual(contact_counts([], WEEK), {})


RULE = {"window_days": 7, "min_conversations": 3, "effect": "escalate",
        "report_window_hours": 72}


class TestLoadRule(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, content):
        path = self.dir / name
        path.write_text(content if isinstance(content, str) else json.dumps(content),
                        encoding="utf-8")
        return path

    def test_errors(self):
        bad = {
            "missing": None,
            "not-json": "{not json",
            "no-block": {"public_reply_rules": []},
            "block-not-object": {"repeat_contact": [1]},
            "min-below-2": {"repeat_contact": dict(RULE, min_conversations=1)},
            "min-float": {"repeat_contact": dict(RULE, min_conversations=3.0)},
            "effect-other": {"repeat_contact": dict(RULE, effect="notify")},
            "bool-min": {"repeat_contact": dict(RULE, min_conversations=True)},
            "bool-window": {"repeat_contact": dict(RULE, window_days=True)},
            "bool-report": {"repeat_contact": dict(RULE, report_window_hours=True)},
            "zero-window": {"repeat_contact": dict(RULE, window_days=0)},
            "inf-report": {"repeat_contact": dict(RULE, report_window_hours=float("inf"))},
            "str-window": {"repeat_contact": dict(RULE, window_days="7")},
        }
        for key in RULE:
            bad["no-" + key] = {"repeat_contact": {k: v for k, v in RULE.items() if k != key}}
        for name, content in bad.items():
            with self.subTest(name):
                path = self.dir / (name + ".json")
                if content is not None:
                    if name == "inf-report":
                        path.write_text('{"repeat_contact": {"window_days": 7, '
                                        '"min_conversations": 3, "effect": "escalate", '
                                        '"report_window_hours": Infinity}}', encoding="utf-8")
                    else:
                        self.write(path.name, content)
                with self.assertRaises(RepeatContactError) as ctx:
                    load_repeat_contact_rule(path)
                self.assertIsInstance(ctx.exception, ValueError)
                self.assertIn(str(path), str(ctx.exception))

    def test_extra_keys_ignored(self):
        path = self.write("ok.json", {"repeat_contact": dict(RULE, note="made-up note"),
                                      "other": 1})
        self.assertEqual(load_repeat_contact_rule(path),
                         RepeatContactRule(window_days=7, min_conversations=3,
                                           effect="escalate", report_window_hours=72))
        frac = self.write("frac.json", {"repeat_contact": dict(RULE, window_days=1.5)})
        self.assertEqual(load_repeat_contact_rule(frac).window_days, 1.5)


class TestRepeatContactFromData(unittest.TestCase):
    def test_parity_with_path_loader(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "repeat_contact_rules.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(repeat_contact_from_data(data, fixture),
                         load_repeat_contact_rule(fixture))
        with self.assertRaises(RepeatContactError) as ctx:
            repeat_contact_from_data({"public_reply_rules": []}, "made-up-source")
        self.assertEqual(str(ctx.exception), "repeat_contact rule in made-up-source: "
                         "'repeat_contact' is missing or is not an object")


def turns_thread(root_id, turns):
    """A thread from (author, inbound, time, text) tuples, in order."""
    built = tuple(Turn(root_id + i, author, inbound, at, text)
                  for i, (author, inbound, at, text) in enumerate(turns))
    return Thread(root_tweet_id=root_id,
                  brand_ids=tuple(a for a, inbound, _, _ in turns if not inbound),
                  turns=built, brand_replied=any(not t[1] for t in turns))


class TestBrandAttribution(unittest.TestCase):
    KNOWN = {"AlphaCo", "BetaCo"}

    def attr(self, thread):
        return brand_attribution(thread, "alphaco", self.KNOWN)

    def test_first_tweet_mention_any_case(self):
        t = turns_thread(100, [("cust_a", True, day(0), "@ALPHACO my order is late")])
        self.assertEqual(self.attr(t), "brand")

    def test_brand_reply_before_last_inbound(self):
        t = turns_thread(110, [("cust_a", True, day(0), "my order is late"),
                               ("AlphaCo", False, day(0, 1), "made-up brand reply"),
                               ("cust_a", True, day(0, 2), "still late")])
        self.assertEqual(self.attr(t), "brand")

    def test_late_brand_reply_with_earlier_other_brand_reply(self):
        t = turns_thread(120, [("cust_a", True, day(0), "my order is late"),
                               ("BetaCo", False, day(0, 1), "made-up brand reply"),
                               ("cust_a", True, day(0, 2), "still late"),
                               ("AlphaCo", False, day(0, 3), "made-up brand reply")])
        self.assertEqual(self.attr(t), "other_brand")

    def test_other_brand_first_tweet_mention(self):
        t = turns_thread(130, [("cust_a", True, day(0), "@BetaCo my order is late")])
        self.assertEqual(self.attr(t), "other_brand")

    def test_only_brand_reply_after_last_inbound(self):
        t = turns_thread(140, [("cust_a", True, day(0), "my order is late"),
                               ("AlphaCo", False, day(0, 1), "made-up brand reply")])
        self.assertEqual(self.attr(t), "unattributed")

    def test_outbound_first_turn(self):
        t = turns_thread(150, [("AlphaCo", False, day(0), "made-up brand notice"),
                               ("cust_a", True, day(0, 1), "@alphaco thanks")])
        self.assertEqual(self.attr(t), "unattributed")

    def test_mention_outside_first_tweet(self):
        t = turns_thread(160, [("cust_a", True, day(0), "my order is late"),
                               ("cust_a", True, day(0, 1), "@alphaco hello?")])
        self.assertEqual(self.attr(t), "unattributed")


class TestModuleHygiene(unittest.TestCase):
    def test_stdlib_only(self):
        import sys
        source = (Path(__file__).resolve().parents[1] / "src" / "triage"
                  / "context_signals.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        stdlib = set(sys.stdlib_module_names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                self.assertTrue(top in stdlib or top == "triage", name)
                self.assertNotEqual(top, "os", name)
        self.assertNotIn("environ", source)
        self.assertNotIn("getenv", source)
        source.encode("ascii")


if __name__ == "__main__":
    unittest.main()
