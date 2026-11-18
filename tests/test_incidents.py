"""Tests for triage.incidents.

Every thread and tweet here is made up by hand. Nothing reads data/raw/, the real
data/split.json or the owner's evals/drift.json (except the opt-in conformance test).
"""

import ast
import contextlib
import copy
import io
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


BURST_AT = hours(200)
MIN = timedelta(minutes=1)


def owner_fixture(*parts):
    """Directory thread at hour 0 (BrandA and BrandB known, 170h of history covered
    before the burst at hour 200) plus the given thread lists."""
    threads = [directory(hours(0))]
    for p in parts:
        threads += p
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


    def test_60_burst_one_incident_all_members(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        r = find(owner_fixture(a), owner_cfg())
        self.assertEqual(len(r.incidents), 1)
        inc = r.incidents[0]
        self.assertEqual(inc.members, 60)
        self.assertEqual(list(inc.member_ids), [t.root_tweet_id for t in a])
        self.assertEqual(inc.opened_at, BURST_AT + 29 * MIN)  # the 30th thread's time
        self.assertEqual(inc.start, BURST_AT)
        self.assertEqual(inc.end, BURST_AT + 59 * MIN)
        self.assertEqual(inc.minutes_to_open, 29.0)
        self.assertEqual(r.outage_threads, 60)
        self.assertEqual(r.unattributed_outage_threads, 0)
        memberships_ok(self, r)

    def test_ordinary_traffic_no_incident(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        # one per hour for 48h before and after, all at least 3h clear of the burst
        before = burst(BASE + 2000, hours(149), 48, step=timedelta(hours=1))
        after = burst(BASE + 3000, hours(204), 48, step=timedelta(hours=1))
        r = find(owner_fixture(before, a, after), owner_cfg())
        self.assertEqual(len(r.incidents), 1)
        self.assertEqual(r.incidents[0].members, 60)
        ordinary = {t.root_tweet_id for t in before + after}
        self.assertFalse(ordinary & set(r.memberships))
        alone = find(owner_fixture(before, after), owner_cfg())
        self.assertEqual(alone.incidents, ())
        self.assertEqual(alone.outage_threads, 96)

    def test_two_brands_two_incidents(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        b = burst(BASE + 4000, BURST_AT + timedelta(seconds=30), 40, text=OUTAGE_B)
        r = find(owner_fixture(a, b), owner_cfg())
        self.assertEqual(len(r.incidents), 2)
        by_brand = {i.brand: set(i.member_ids) for i in r.incidents}
        self.assertEqual(by_brand, {"BrandA": {t.root_tweet_id for t in a},
                                    "BrandB": {t.root_tweet_id for t in b}})
        self.assertEqual([i.id for i in r.incidents], ["inc-0001", "inc-0002"])
        memberships_ok(self, r)

    def test_long_surge_merges(self):
        surge = burst(BASE + 1000, BURST_AT, 120, step=timedelta(seconds=150))  # ~5h
        r = find(owner_fixture(surge), owner_cfg())
        self.assertEqual(len(r.incidents), 1)
        self.assertEqual(r.incidents[0].members, 120)
        self.assertEqual(r.incidents[0].reopens, 0)

    def test_multi_brand_thread_one_incident(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        b = burst(BASE + 4000, BURST_AT + timedelta(seconds=30), 40, text=OUTAGE_B)
        both = make_thread(BASE + 5000, BURST_AT + timedelta(minutes=35, seconds=10),
                           "@BrandA @BrandB app is down again")
        r = find(owner_fixture(a, b, [both]), owner_cfg())
        self.assertEqual(len(r.incidents), 2)
        holders = [i.id for i in r.incidents if BASE + 5000 in i.member_ids]
        self.assertEqual(len(holders), 1)
        self.assertEqual(r.memberships[BASE + 5000], holders[0])
        memberships_ok(self, r)

    def test_reply_attribution_only_from_reply_time(self):
        mentioned = burst(BASE + 1000, BURST_AT, 29)
        start30 = BURST_AT + 30 * MIN

        def thirtieth(delay):
            return make_thread(BASE + 1100, start30, "app is down again",
                               replies=[("BrandA", start30 + delay)])

        late = find(owner_fixture(mentioned, [thirtieth(timedelta(hours=3))]), owner_cfg())
        self.assertEqual(late.incidents, ())
        self.assertEqual(late.unattributed_outage_threads, 1)
        soon = find(owner_fixture(mentioned, [thirtieth(timedelta(hours=1))]), owner_cfg())
        self.assertEqual(len(soon.incidents), 1)
        self.assertEqual(soon.incidents[0].opened_at, start30 + timedelta(hours=1))
        self.assertEqual(soon.incidents[0].members, 30)
        self.assertEqual(soon.unattributed_outage_threads, 0)

    def test_brand_unknown_until_first_reply(self):
        c = burst(BASE + 1000, BURST_AT, 30, text="@BrandC app is down again")

        def first_reply(when):
            return make_thread(BASE + 6000, hours(1), "hello there",
                               replies=[("BrandC", when)])

        soon = find(owner_fixture(c, [first_reply(BURST_AT + 40 * MIN)]), owner_cfg())
        self.assertEqual(len(soon.incidents), 1)
        self.assertEqual(soon.incidents[0].brand, "BrandC")
        self.assertEqual(soon.incidents[0].opened_at, BURST_AT + 40 * MIN)
        late = find(owner_fixture(c, [first_reply(hours(203))]), owner_cfg())
        self.assertEqual(late.incidents, ())
        self.assertEqual(late.unattributed_outage_threads, 30)
        # a lower-case mention enters the directory's spelling
        lower = burst(BASE + 1000, BURST_AT, 30, text="@branda app is down again")
        r = find(owner_fixture(lower), owner_cfg())
        self.assertEqual([i.brand for i in r.incidents], ["BrandA"])

    def test_label_first_turn_only(self):
        t = make_thread(BASE + 1000, BURST_AT, "hello")
        t = Thread(root_tweet_id=t.root_tweet_id, brand_ids=(), brand_replied=False,
                   turns=t.turns + (Turn(BASE + 1001, "cust_x", True, BURST_AT + MIN,
                                         "@BrandA app is down"),))
        self.assertEqual(CheapTier().label(t).intent, "outage_technical")  # uncut thread
        r = find(owner_fixture([t]), owner_cfg())
        self.assertEqual(r.outage_threads, 0)

    def test_warm_up_never_zero_history(self):
        a = burst(BASE + 1000, hours(10), 60)
        r = find(owner_fixture(a), owner_cfg())
        self.assertEqual(r.incidents, ())
        self.assertEqual(r.warm_up_outage_threads, 60)
        self.assertEqual(r.warm_up_events_at_min_burst, 31)

    def test_members_tagged_one_reply_slot(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        r = find(owner_fixture(a), owner_cfg())
        inc = r.incidents[0]
        self.assertEqual(inc.status_reply, STATUS)
        for t in a:
            self.assertEqual(r.memberships[t.root_tweet_id], inc.id)
        for name in ("reason_codes", "reason", "reasons", "topic_id"):
            self.assertFalse(hasattr(inc, name), name)

    def test_top_terms_strip_handles(self):
        r = find(owner_fixture(burst(BASE + 1000, BURST_AT, 60)), owner_cfg())
        terms = r.incidents[0].top_terms
        for term in ("again", "app", "down"):
            self.assertIn(term, terms)
        self.assertNotIn("branda", terms)
        self.assertNotIn("is", terms)
        self.assertLessEqual(len(terms), 5)

    def test_numbers_come_from_config(self):
        a = burst(BASE + 1000, BURST_AT, 60)
        self.assertEqual(find(owner_fixture(a), cfg(2, 61, 3.0, 168)).incidents, ())
        self.assertEqual(len(find(owner_fixture(a), cfg(2, 60, 3.0, 168)).incidents), 1)


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


TWCS_HEADER = ("tweet_id,author_id,inbound,created_at,text,response_tweet_id,"
               "in_response_to_tweet_id\n")
CLI_BASE = 9900000
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _twitter_date(dt):
    return "%s %s %02d %02d:%02d:%02d +0000 %d" % (
        _DAYS[dt.weekday()], _MONTHS[dt.month - 1], dt.day, dt.hour, dt.minute, dt.second,
        dt.year)


def _row(n, author, inbound, when, text, parent=""):
    return "%d,%s,%s,%s,\"%s\",,%s" % (
        CLI_BASE + n, author, "True" if inbound else "False", _twitter_date(when), text,
        "" if parent == "" else CLI_BASE + parent)


def _cli_rows():
    """Directory thread at hour 0 answered by both planted brands, then a 60-thread
    burst for one and a 40-thread burst for the other inside the same 2 hours."""
    rows = [_row(1, "custSENTINEL1", True, hours(0), "hello SENTINELTEXT1"),
            _row(2, "SENTINELBRANDA", False, hours(0) + MIN, "DM us SENTINELTEXT2", 1),
            _row(3, "SENTINELBRANDB", False, hours(0) + MIN, "DM us SENTINELTEXT3", 1)]
    for i in range(60):
        n = 100 + i
        rows.append(_row(n, "custSENTINEL%d" % n, True, BURST_AT + i * MIN,
                         "@SENTINELBRANDA app is down SENTINELTERM SENTINELTEXT%d" % n))
    for i in range(40):
        n = 300 + i
        rows.append(_row(n, "custSENTINEL%d" % n, True, BURST_AT + i * MIN + timedelta(seconds=30),
                         "@SENTINELBRANDB app is down SENTINELTERM SENTINELTEXT%d" % n))
    return rows


CLI_IDS = [CLI_BASE + n for n in [1, 2, 3] + list(range(100, 160)) + list(range(300, 340))]


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "twcs.csv"
        self.data.write_text(TWCS_HEADER + "\n".join(_cli_rows()) + "\n", encoding="utf-8")
        self.split = self.dir / "split.json"
        self.split.write_text(json.dumps({"seed": 0, "unit": "thread", "fractions": {
            "dev": 1.0, "eval": 0.0, "full": 0.0}}), encoding="utf-8")
        self.report = self.dir / "report.json"

    def main(self, *extra, config=FIXTURE_CONFIG, split="dev", report=None):
        argv = ["--split", split, "--report-out", str(report or self.report),
                "--config", str(config), "--data", str(self.data),
                "--split-config", str(self.split)] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = incidents.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_no_planted(self, text):
        self.assertNotIn("sentinel", text.lower())
        for i in CLI_IDS:
            self.assertNotIn(str(i), text)
        for term in ("inc-0", "sentinelterm"):
            self.assertNotIn(term, text.lower())

    def test_report_counts_and_no_ids(self):
        code, out, err = self.main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "incidents=2 members=100 drafts_avoided=98 unattributed=0 "
                              "split=dev\n")
        text = self.report.read_text(encoding="utf-8")
        report = json.loads(text)
        self.assertEqual(report["incidents"], 2)
        self.assertEqual(report["members_per_incident"], [40, 60])
        self.assertEqual(report["members_total"], 100)
        self.assertEqual(report["drafts_avoided"], 98)
        self.assertEqual(report["incidents_awaiting_approval"], 2)
        self.assertEqual(report["reopens"], 0)
        self.assertEqual(report["outage_threads"], 100)
        self.assertEqual(report["unattributed_outage_threads"], 0)
        self.assertEqual(report["minutes_first_message_to_incident"], [29.0, 29.0])
        self.assertEqual(report["look_ahead"], {
            "labels": "first_turn_only", "brands": "known_at_event_time",
            "baseline": "earlier_unassigned_only",
            "residual": ["thread_links_from_full_file", "dataset_snapshot"]})
        self.assertEqual(report["warm_up"], {"hours": 170, "outage_threads": 0,
                                             "events_at_min_burst": 0})
        self.assertEqual((report["config_version"], report["split"]), ("fixture-v1", "dev"))
        self.assertEqual((report["window_hours"], report["min_burst"],
                          report["baseline_factor"], report["baseline_lookback_hours"],
                          report["label"]), (2, 30, 3.0, 168, "outage_technical"))
        self.assert_no_planted(text)
        md = self.dir / "report.md"
        code, _, err = self.main("--format", "md", report=md)
        self.assertEqual((code, err), (0, ""))
        md_text = md.read_text(encoding="utf-8")
        self.assertIn("drafts_avoided", md_text)
        self.assertIn("98", md_text)
        self.assert_no_planted(md_text)

    def test_incidents_out_inside_repo_refused(self):
        inside = REPO_ROOT / ("incidents_test_%d.jsonl" % os.getpid())
        self.addCleanup(lambda: inside.unlink() if inside.exists() else None)
        code, out, err = self.main("--incidents-out", str(inside))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: "))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertFalse(inside.exists())
        self.assertFalse(self.report.exists())

    def test_eval_split_incidents_out_refused(self):
        path = self.dir / "incidents.jsonl"
        code, out, err = self.main("--incidents-out", str(path), split="eval")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))
        self.assertFalse(path.exists())
        self.assertFalse(self.report.exists())

    def test_missing_incidents_section_clear_error(self):
        config = self.dir / "no_incidents.json"
        data = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))
        del data["incidents"]
        config.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self.main(config=config)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("error: "))
        self.assertIn(str(config), err)
        self.assertFalse(self.report.exists())

    def test_incidents_out_written_outside_repo(self):
        path = self.dir / "incidents.jsonl"
        code, _, err = self.main("--incidents-out", str(path))
        self.assertEqual((code, err), (0, ""))
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([x["id"] for x in lines], ["inc-0001", "inc-0002"])
        self.assertEqual({x["brand"] for x in lines}, {"SENTINELBRANDA", "SENTINELBRANDB"})
        by_brand = {x["brand"]: x for x in lines}
        self.assertEqual(by_brand["SENTINELBRANDA"]["member_ids"],
                         [CLI_BASE + n for n in range(100, 160)])
        self.assertEqual(len(by_brand["SENTINELBRANDB"]["member_ids"]), 40)
        for x in lines:
            self.assertEqual(x["status_reply"], STATUS)
            self.assertIn("sentinelterm", x["top_terms"])
            datetime.fromisoformat(x["start"])
        self.assert_no_planted(self.report.read_text(encoding="utf-8"))

    def test_bad_data_exits_2(self):
        code, out, err = self.main()
        self.assertEqual(code, 0)
        self.data.unlink()
        code, out, err = self.main(report=self.dir / "r2.json")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: data file not found: "))
        self.assertFalse((self.dir / "r2.json").exists())


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
