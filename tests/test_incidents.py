"""Tests for triage.incidents.

Every thread and tweet here is made up by hand. Nothing reads data/raw/, the real
data/split.json or the owner's evals/drift.json (except the opt-in conformance test).
"""

import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from datetime import datetime, timedelta, timezone

import triage.incidents as incidents
from triage.cheap_tier import CheapTier
from triage.threads import Thread, Turn

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "drift_config.json"

UTC = timezone.utc
BASE = 6100000  # made-up ids are BASE + small number, so they never collide with a count
H0 = datetime(2016, 6, 1, tzinfo=UTC)  # made-up "hour 0"
OUTAGE_A = "@BrandA app is down again"  # cheap tier: outage_technical
OUTAGE_B = "@BrandB app is down again"
STATUS = {"state": "awaiting_reply", "needs_human_approval": True, "text": None}


def at(seconds):
    """H0 plus a whole number of seconds, so window edges compare exactly."""
    return H0 + timedelta(seconds=seconds)


def hours(h):
    return at(round(h * 3600))


def make_thread(root, when, text, replies=()):
    """A thread whose first turn is an inbound tweet at `when`. `replies` holds
    (brand handle, time) for later outbound turns."""
    turns = [Turn(root, "cust_%d" % root, True, when, text)]
    for i, (brand, r_at) in enumerate(replies):
        turns.append(Turn(root + 500000 + i, brand, False, r_at, "sorry, DM us"))
    brand_ids = tuple(sorted({t.author_id for t in turns if not t.inbound}))
    return Thread(root_tweet_id=root, brand_ids=brand_ids, turns=tuple(turns),
                  brand_replied=bool(brand_ids))


def directory(when, root=BASE):
    """A non-outage thread answered by BrandA and BrandB a minute after `when`."""
    return make_thread(root, when, "hello there",
                       replies=[("BrandA", when + timedelta(minutes=1)),
                                ("BrandB", when + timedelta(minutes=1))])


def cfg(window=2, min_burst=3, factor=3.0, lookback=2):
    return incidents.IncidentConfig(path="fixture", version="test", window_hours=window,
                                    min_burst=min_burst, baseline_factor=factor,
                                    baseline_lookback_hours=lookback,
                                    label="outage_technical")


def owner_cfg():
    return incidents.load_incident_config(FIXTURE_CONFIG)


def find(threads, config):
    return incidents.find_incidents(threads, CheapTier(), config)


def memberships_ok(test, result):
    """Every member maps to exactly its own incident, and no id is in two incidents."""
    seen = [m for inc in result.incidents for m in inc.member_ids]
    test.assertEqual(len(seen), len(set(seen)))
    test.assertEqual(result.memberships,
                     {m: inc.id for inc in result.incidents for m in inc.member_ids})


def burst(start_root, start, n, text=OUTAGE_A, step=timedelta(minutes=1)):
    return [make_thread(start_root + i, start + i * step, text) for i in range(n)]


# Worked examples: window 2h, min_burst 3, factor 3, lookback 2h, history from hour -10.
EXAMPLE_A = (0, 0.1, 0.2, 2.15, 2.16)


def example(hours_list):
    threads = [directory(hours(-10))]
    threads += [make_thread(BASE + 1 + i, hours(h), OUTAGE_A) for i, h in enumerate(hours_list)]
    return threads


class TestFindIncidents(unittest.TestCase):
    def test_worked_example_no_double_membership(self):
        r = find(example(EXAMPLE_A), cfg())
        self.assertEqual(len(r.incidents), 1)
        inc = r.incidents[0]
        self.assertEqual(inc.id, "inc-0001")
        self.assertEqual(inc.brand, "BrandA")
        self.assertEqual(list(inc.member_ids), [BASE + 1, BASE + 2, BASE + 3])
        self.assertEqual(inc.members, 3)
        self.assertEqual((inc.start, inc.opened_at, inc.end), (hours(0), hours(0.2), hours(0.2)))
        self.assertEqual(inc.minutes_to_open, 12.0)
        self.assertEqual(inc.reopens, 0)
        memberships_ok(self, r)

    def test_reopen_within_window_merges(self):
        r = find(example(EXAMPLE_A + (2.17,)), cfg())
        self.assertEqual(len(r.incidents), 1)
        inc = r.incidents[0]
        self.assertEqual(inc.members, 6)
        self.assertEqual(inc.reopens, 1)
        self.assertEqual(inc.end, hours(2.17))
        self.assertEqual(inc.opened_at, hours(0.2))
        memberships_ok(self, r)

    def test_burst_after_window_new_incident(self):
        # The spec's literal example C (A plus 5.0, 5.1, 5.2) cannot trigger under its own
        # baseline rule: at 5.2 the baseline window (1.2, 3.2] holds the unassigned 2.15
        # and 2.16, so r = 2 * (2/2) = 2 and f = 3 is not > 3 * 2 = 6. This fixture keeps
        # what C proves: A's incident (0, 0.1, 0.2) and a later burst whose window
        # (3.2, 5.2] does not contain that incident's end, so a new incident opens.
        r = find(example((0, 0.1, 0.2, 5.0, 5.1, 5.2)), cfg())
        self.assertEqual([i.id for i in r.incidents], ["inc-0001", "inc-0002"])
        self.assertEqual(r.incidents[0].reopens, 0)
        second = r.incidents[1]
        self.assertEqual(second.members, 3)
        self.assertEqual(list(second.member_ids), [BASE + 4, BASE + 5, BASE + 6])
        self.assertEqual(second.opened_at, hours(5.2))
        memberships_ok(self, r)
        # the literal example C, as the rules are written: still only inc-0001
        literal = find(example(EXAMPLE_A + (5.0, 5.1, 5.2)), cfg())
        self.assertEqual(len(literal.incidents), 1)
        memberships_ok(self, literal)


class TestLoadIncidentConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.owner = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))

    def variant(self, edit):
        data = copy.deepcopy(self.owner)
        edit(data)
        return json.dumps(data)

    def test_accepts_owner_shape(self):
        c = incidents.load_incident_config(FIXTURE_CONFIG)
        self.assertEqual(c.version, "fixture-v1")
        self.assertEqual(c.path, str(FIXTURE_CONFIG))
        self.assertEqual((c.window_hours, c.min_burst, c.baseline_factor,
                          c.baseline_lookback_hours, c.label),
                         (2, 30, 3.0, 168, "outage_technical"))
        # extra keys are ignored; a non-string version becomes None
        path = self.dir / "extra.json"

        def edit(d):
            d["version"] = 7
            d["incidents"]["something_else"] = [1]
        path.write_text(self.variant(edit), encoding="utf-8")
        self.assertIsNone(incidents.load_incident_config(path).version)

    def test_errors(self):
        def set_(key, value):
            def edit(d):
                d["incidents"][key] = value
            return edit

        def drop(key):
            def edit(d):
                del d["incidents"][key]
            return edit

        def no_section(d):
            del d["incidents"]

        def section_list(d):
            d["incidents"] = [1]

        inf = float("inf")
        cases = [
            ("missing file", None),
            ("invalid json", "{not json"),
            ("top level not object", "[1, 2]"),
            ("no incidents section", self.variant(no_section)),
            ("incidents not object", self.variant(section_list)),
        ]
        for key in ("window_hours", "min_burst", "baseline_factor",
                    "baseline_lookback_hours", "label"):
            cases.append(("missing " + key, self.variant(drop(key))))
        for key in ("window_hours", "baseline_factor", "baseline_lookback_hours"):
            cases += [
                ("%s zero" % key, self.variant(set_(key, 0))),
                ("%s negative" % key, self.variant(set_(key, -1.5))),
                ("%s infinite" % key, self.variant(set_(key, inf))),
                ("%s nan" % key, self.variant(set_(key, float("nan")))),
                ("%s bool" % key, self.variant(set_(key, True))),
                ("%s string" % key, self.variant(set_(key, "2"))),
            ]
        cases += [
            ("min_burst 1", self.variant(set_("min_burst", 1))),
            ("min_burst float", self.variant(set_("min_burst", 30.0))),
            ("min_burst bool", self.variant(set_("min_burst", True))),
            ("min_burst string", self.variant(set_("min_burst", "30"))),
            ("lookback shorter than window",
             self.variant(set_("baseline_lookback_hours", 1))),
            ("other label", self.variant(set_("label", "complaint"))),
            ("label not string", self.variant(set_("label", None))),
        ]
        for name, text in cases:
            with self.subTest(name):
                path = self.dir / (name.replace(" ", "_") + ".json")
                if text is not None:
                    path.write_text(text, encoding="utf-8")
                with self.assertRaises(incidents.IncidentConfigError) as ctx:
                    incidents.load_incident_config(path)
                self.assertIsInstance(ctx.exception, ValueError)
                self.assertTrue(str(ctx.exception).startswith(
                    "incident config %s: " % path), str(ctx.exception))


class TestLiveConformance(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TRIAGE_LIVE_CONFORMANCE") == "1",
                         "set TRIAGE_LIVE_CONFORMANCE=1 to load the owner's evals/drift.json")
    def test_live_incident_config_loads(self):
        config = incidents.load_incident_config(REPO_ROOT / "evals" / "drift.json")
        self.assertEqual(config.label, "outage_technical")


class TestModuleHygiene(unittest.TestCase):
    def test_stdlib_only(self):
        banned = {"urllib", "http", "socket", "requests", "ssl", "ftplib", "smtplib"}
        stdlib = set(sys.stdlib_module_names)
        src = (REPO_ROOT / "src" / "triage" / "incidents.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                self.assertNotIn(top, banned, mod)
                self.assertTrue(top == "triage" or top in stdlib, "non-stdlib %s" % mod)


if __name__ == "__main__":
    unittest.main()
