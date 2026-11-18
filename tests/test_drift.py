"""Tests for triage.drift.

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

import triage.drift as drift
from triage.cheap_tier import CheapTier
from triage.evaluate import wilson_interval
from triage.threads import Thread, Turn

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "drift_config.json"

INTENTS7 = ("delivery", "billing", "account_access", "outage_technical", "complaint", "praise",
            "other")


UTC = timezone.utc
BASE = 7300000  # made-up ids are BASE + small number, so they never collide with a count
# 2016-03-07 is the Monday of ISO week 2016-W10. All fixture weeks are made-up test weeks.
MON = datetime(2016, 3, 7, tzinfo=UTC)
DELIVERY_TEXT = "where is my parcel"  # cheap tier: delivery, action ask, confidence 0.6
PRAISE_TEXT = "thanks love it"  # cheap tier: praise, action answer, confidence 0.75


def week_label(i):
    return "2016-W%02d" % (10 + i)


def make_thread(root, at, text, more=()):
    """A thread whose first turn is an inbound tweet at `at`. `more` holds
    (id, inbound, at, text) for later turns."""
    turns = [Turn(root, "cust_%d" % root, True, at, text)]
    for tid, inbound, t_at, t_text in more:
        turns.append(Turn(tid, "cust_%d" % root if inbound else "brandx", inbound, t_at, t_text))
    brand_ids = tuple(sorted({t.author_id for t in turns if not t.inbound}))
    return Thread(root_tweet_id=root, brand_ids=brand_ids, turns=tuple(turns),
                  brand_replied=bool(brand_ids))


def shares(**kw):
    return {i: kw.get(i, 0.0) for i in INTENTS7}


class TestPsi(unittest.TestCase):
    def test_worked_example_a(self):
        ref = {i: 0.15 for i in INTENTS7}
        ref["delivery"] = 0.10
        win = {i: 0.10 for i in INTENTS7}
        win["delivery"] = 0.40
        self.assertAlmostEqual(drift.psi(ref, win), 0.537528, delta=0.001)

    def test_worked_example_b_zero_bins(self):
        ref = shares(delivery=0.5, billing=0.25, praise=0.25)
        win = shares(delivery=0.5, billing=0.5)
        self.assertAlmostEqual(drift.psi(ref, win), 2.128516, delta=0.001)

    def test_identical_is_zero(self):
        s = shares(delivery=0.3, praise=0.7)
        self.assertEqual(drift.psi(s, dict(s)), 0.0)


class TestCut(unittest.TestCase):
    def test_later_turns_and_replies_excluded(self):
        cutoff = MON + timedelta(days=7)  # Monday 00:00 UTC after week 2016-W10
        t = make_thread(BASE + 1, MON + timedelta(days=6, hours=23), "hello",
                        more=[(BASE + 2, True, cutoff - timedelta(seconds=1), "still there"),
                              (BASE + 3, False, cutoff, "sorry, DM us"),
                              (BASE + 4, True, cutoff + timedelta(hours=1), "my parcel")])
        before = (t.turns, t.brand_ids, t.brand_replied)
        cut = drift.cut_thread(t, cutoff)
        self.assertEqual([x.tweet_id for x in cut.turns], [BASE + 1, BASE + 2])
        self.assertFalse(cut.brand_replied)
        self.assertEqual(cut.brand_ids, ())
        self.assertEqual(cut.root_tweet_id, BASE + 1)
        self.assertEqual((t.turns, t.brand_ids, t.brand_replied), before)
        self.assertTrue(t.brand_replied)
        self.assertEqual(len(t.turns), 4)
        # cutoff before the first turn keeps nothing
        self.assertIsNone(drift.cut_thread(t, MON))

    def test_signals_use_cut_copy(self):
        # first turn in week W (no keyword); a later inbound turn in W+1 names a parcel
        w0 = make_thread(BASE + 10, MON + timedelta(days=2), "hello there",
                         more=[(BASE + 11, True, MON + timedelta(days=8), DELIVERY_TEXT)])
        w1 = make_thread(BASE + 20, MON + timedelta(days=9), PRAISE_TEXT)
        buckets = drift.replay_weeks([w0, w1])
        self.assertEqual([len(b.threads[0].turns) for b in buckets], [1, 1])
        signals = drift.weekly_signals(buckets, CheapTier())
        self.assertEqual(signals[0].intent_counts["delivery"], 0)
        self.assertEqual(signals[0].intent_counts["other"], 1)
        # the uncut thread would have been labelled delivery
        self.assertEqual(CheapTier().label(w0).intent, "delivery")


HIGH = 100.0  # a threshold no fixture signal can reach


def cfg(start, end, *, intent_psi=HIGH, volume=HIGH, mean_confidence=HIGH,
        escalated_share=HIGH, other_share=HIGH, min_volume=1, k=0):
    return drift.DriftConfig(
        path="fixture", version="test", start_week=start, end_week=end,
        min_volume=min_volume, samples_per_callout=k,
        thresholds={"intent_psi": intent_psi, "volume": volume,
                    "mean_confidence": mean_confidence, "escalated_share": escalated_share,
                    "other_share": other_share})


def week_threads(i, n_delivery, n_praise):
    """n_delivery + n_praise one-turn threads in fixture week i (2016-W10 + i)."""
    start = MON + timedelta(weeks=i, hours=1)
    texts = [DELIVERY_TEXT] * n_delivery + [PRAISE_TEXT] * n_praise
    return [make_thread(BASE + 10000 * (i + 1) + j, start + timedelta(minutes=j), text)
            for j, text in enumerate(texts)]


def run(weeks, config):
    """weeks: list of (n_delivery, n_praise) per fixture week, from week 0."""
    threads = [t for i, (d, p) in enumerate(weeks) for t in week_threads(i, d, p)]
    return drift.check_drift(drift.weekly_signals(drift.replay_weeks(threads), CheapTier()),
                             config)


def callouts(result):
    return [(c.week, c.signal) for c in result.callouts]


def by_week(result):
    return {w.week: w for w in result.weeks}


class TestCheckDrift(unittest.TestCase):
    def planted(self, week5):
        # lead-in (partial), weeks 1-4 reference, week 5 monitored, trail-out (partial)
        return [(0, 3), (10, 90), (10, 90), (10, 90), (10, 90), week5, (0, 3)]

    def test_planted_shift_week5_only(self):
        # Only intent_psi can fire: it is 0.1 and every other threshold is set too high
        # to be reached (HIGH), with min_volume 50.
        config = cfg(week_label(1), week_label(4), intent_psi=0.1, min_volume=50)
        result = run(self.planted((40, 60)), config)
        self.assertEqual(callouts(result), [(week_label(5), "intent_psi")])
        c = result.callouts[0]
        self.assertAlmostEqual(c.value, 0.537528, delta=0.001)
        self.assertEqual(c.reference_value, 0.0)
        self.assertEqual(c.threshold, 0.1)
        self.assertFalse(hasattr(c, "reason") or hasattr(c, "reasons"))
        self.assertEqual(result.reference.volume, 100)
        self.assertAlmostEqual(result.reference.intent_shares["delivery"], 0.10)

    def test_no_shift_no_callouts(self):
        config = cfg(week_label(1), week_label(4), intent_psi=0.1, mean_confidence=0.01,
                     escalated_share=0.01, other_share=0.01, volume=2.0, min_volume=50)
        result = run(self.planted((10, 90)), config)
        self.assertEqual(callouts(result), [])
        self.assertEqual(by_week(result)[week_label(5)].intent_psi, 0.0)

    def test_thresholds_come_from_config(self):
        weeks = self.planted((40, 60))
        low = run(weeks, cfg(week_label(1), week_label(4), intent_psi=0.1, min_volume=50))
        high = run(weeks, cfg(week_label(1), week_label(4), intent_psi=0.6, min_volume=50))
        self.assertEqual(callouts(low), [(week_label(5), "intent_psi")])
        self.assertEqual(callouts(high), [])

    def test_reference_frozen_earlier_weeks_only(self):
        # partial lead-in, a non-partial pre-reference week whose mix would breach,
        # reference weeks 2-3, monitored week 4, partial trail-out
        config = cfg(week_label(2), week_label(3), intent_psi=0.1, mean_confidence=0.01,
                     min_volume=50)
        calm = run([(0, 3), (50, 50), (10, 90), (10, 90), (10, 90), (0, 3)], config)
        self.assertEqual(callouts(calm), [])
        weeks = by_week(calm)
        self.assertEqual(weeks[week_label(1)].phase, "before_reference")
        self.assertEqual(weeks[week_label(2)].phase, "reference")
        self.assertEqual(weeks[week_label(4)].phase, "monitored")
        for i in (0, 1, 2, 3, 5):
            w = weeks[week_label(i)]
            self.assertEqual((w.volume_ratio, w.intent_psi, w.mean_confidence_delta,
                              w.escalated_share_delta, w.other_share_delta),
                             (None,) * 5, week_label(i))
        shifted = run([(0, 3), (50, 50), (10, 90), (10, 90), (70, 30), (0, 3)], config)
        self.assertEqual(shifted.reference, calm.reference)
        self.assertEqual(callouts(shifted), [(week_label(4), "intent_psi"),
                                             (week_label(4), "mean_confidence")])

    def test_low_volume_reported_not_flagged(self):
        config = cfg(week_label(1), week_label(2), intent_psi=0.1, mean_confidence=0.01,
                     volume=2.0, min_volume=80)
        result = run([(0, 3), (10, 90), (10, 90), (30, 30), (0, 3)], config)
        self.assertEqual(callouts(result), [])
        w = by_week(result)[week_label(3)]
        self.assertTrue(w.low_volume)
        self.assertGreater(w.intent_psi, 0.1)
        self.assertGreater(w.mean_confidence_delta, 0.01)
        self.assertAlmostEqual(w.volume_ratio, 0.6)

    def test_volume_ratio_inclusive_and_empty_week(self):
        # reference mean 100; monitored weeks 250, 200, 51, 50, 40, 0, 199.
        # min_volume 300 so every monitored week is low volume: volume still fires.
        sizes = [250, 200, 51, 50, 40, 0, 199]
        weeks = [(0, 3), (0, 100)] + [(0, n) for n in sizes] + [(0, 3)]
        result = run(weeks, cfg(week_label(1), week_label(1), volume=2.0, min_volume=300))
        got = [(c.week, c.signal, c.value, c.reference_value, c.threshold)
               for c in result.callouts]
        self.assertEqual(got, [(week_label(2), "volume", 2.5, 1.0, 2.0),
                               (week_label(3), "volume", 2.0, 1.0, 2.0),
                               (week_label(5), "volume", 0.5, 1.0, 2.0),
                               (week_label(6), "volume", 0.4, 1.0, 2.0),
                               (week_label(7), "volume", 0.0, 1.0, 2.0)])
        empty = by_week(result)[week_label(7)]
        self.assertEqual(empty.signals.volume, 0)
        self.assertTrue(empty.low_volume)
        self.assertIsNone(empty.intent_psi)

    def test_partial_weeks_ignored(self):
        # the partial lead-in sits inside the reference range; both partial weeks carry
        # a mix and volume that would breach every signal
        config = cfg(week_label(0), week_label(2), intent_psi=0.1, volume=2.0,
                     mean_confidence=0.01, other_share=0.01, min_volume=50)
        result = run([(500, 0), (0, 100), (0, 100), (0, 100), (500, 0)], config)
        self.assertEqual(callouts(result), [])
        self.assertEqual(result.reference.weeks_used, (week_label(1), week_label(2)))
        self.assertEqual(result.reference.weeks_missing, (week_label(0),))
        self.assertEqual(result.reference.volume, 100)
        self.assertEqual(result.reference.intent_shares["praise"], 1.0)
        self.assertIsNone(by_week(result)[week_label(4)].volume_ratio)

    def test_empty_reference_errors(self):
        # reference weeks inside the span but empty
        config = cfg(week_label(1), week_label(2))
        with self.assertRaises(drift.DriftDataError) as ctx:
            run([(0, 3), (0, 0), (0, 0), (0, 5), (0, 3)], config)
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertEqual(str(ctx.exception),
                         "reference range 2016-W11..2016-W12 holds no usable threads")
        # reference weeks outside the span
        with self.assertRaises(drift.DriftDataError):
            run([(0, 3), (0, 5), (0, 3)], cfg("2016-W30", "2016-W31"))


class TestLoadDriftConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.owner = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def variant(self, edit):
        data = copy.deepcopy(self.owner)
        edit(data)
        return json.dumps(data)

    def test_errors(self):
        def set_(keys, value):
            def edit(d):
                for k in keys[:-1]:
                    d = d[k]
                d[keys[-1]] = value
            return edit

        def drop(keys):
            def edit(d):
                for k in keys[:-1]:
                    d = d[k]
                del d[keys[-1]]
            return edit

        cases = [
            ("missing file", None),
            ("invalid json", "{not json"),
            ("top level not object", "[1, 2]"),
            ("no drift section", self.variant(drop(["drift"]))),
            ("malformed week", self.variant(set_(["drift", "reference", "start_week"],
                                                 "2016-11"))),
            ("week 53 in a 52-week year", self.variant(
                set_(["drift", "reference", "end_week"], "2017-W53"))),
            ("missing week", self.variant(drop(["drift", "reference", "end_week"]))),
            ("start after end", self.variant(set_(["drift", "reference", "start_week"],
                                                  "2016-W13"))),
            ("missing threshold key", self.variant(drop(["drift", "thresholds",
                                                         "other_share"]))),
            ("non-positive threshold", self.variant(
                set_(["drift", "thresholds", "mean_confidence"], 0))),
            ("bool threshold", self.variant(set_(["drift", "thresholds", "intent_psi"], True))),
            ("volume of 1.0", self.variant(set_(["drift", "thresholds", "volume"], 1.0))),
            ("min_volume of 0", self.variant(set_(["drift", "min_volume"], 0))),
            ("bool min_volume", self.variant(set_(["drift", "min_volume"], True))),
            ("negative samples", self.variant(set_(["drift", "samples_per_callout"], -1))),
            ("missing samples", self.variant(drop(["drift", "samples_per_callout"]))),
            ("NaN threshold", self.variant(set_(["drift", "thresholds", "intent_psi"],
                                                float("nan")))),
        ]
        for i, (name, text) in enumerate(cases):
            with self.subTest(name):
                path = self.dir / ("missing_%d.json" % i) if text is None \
                    else self.write("case_%d.json" % i, text)
                with self.assertRaises(drift.DriftConfigError) as ctx:
                    drift.load_drift_config(path)
                self.assertIsInstance(ctx.exception, ValueError)
                self.assertIn(str(path), str(ctx.exception))

    def test_accepts_owner_shape_with_incidents(self):
        self.assertIn("incidents", self.owner)
        self.assertIn("threshold_notes", self.owner["drift"])
        config = drift.load_drift_config(FIXTURE_CONFIG)
        self.assertEqual(config.version, "fixture-v1")
        self.assertEqual((config.start_week, config.end_week), ("2016-W11", "2016-W12"))
        self.assertEqual(config.min_volume, 3)
        self.assertEqual(config.samples_per_callout, 2)
        self.assertEqual(config.thresholds, {"intent_psi": 0.2, "volume": 2.0,
                                             "mean_confidence": 0.05,
                                             "escalated_share": 0.03, "other_share": 0.05})


# ---- CLI fixture: a made-up twcs.csv, never real rows ----
TWCS_HEADER = ("tweet_id,author_id,inbound,created_at,text,response_tweet_id,"
               "in_response_to_tweet_id\n")
CLI_BASE = 8800000
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _twitter_date(dt):
    return "%s %s %02d %02d:%02d:%02d +0000 %d" % (
        _DAYS[dt.weekday()], _MONTHS[dt.month - 1], dt.day, dt.hour, dt.minute, dt.second,
        dt.year)


def _row(n, inbound, at, response="", parent=""):
    author = "custSENTINEL%d" % n if inbound else "SENTINELBRAND"
    text = ("thanks love it SENTINELTEXT%d" % n) if inbound else "DM us SENTINELTEXT%d" % n
    return "%d,%s,%s,%s,\"%s\",%s,%s" % (
        CLI_BASE + n, author, "True" if inbound else "False", _twitter_date(at), text,
        "" if response == "" else CLI_BASE + response, "" if parent == "" else CLI_BASE + parent)


def _cli_rows():
    w = lambda i, day, hour=10: MON + timedelta(weeks=i, days=day, hours=hour)  # noqa: E731
    rows = [_row(1, True, w(0, 2))]                                   # 2016-W10, partial
    rows += [_row(11, True, w(1, 1)), _row(12, True, w(1, 3))]        # 2016-W11, reference
    rows += [_row(21, True, w(2, 1)), _row(22, True, w(2, 3))]        # 2016-W12, reference
    # 2016-W13, monitored, 5 threads: volume ratio 2.5 against the reference mean of 2.
    # 31 has a brand reply inside the week; 34 gets a brand reply and an inbound turn
    # only after the week's end (2016-W14).
    rows += [_row(31, True, w(3, 1), response=51), _row(51, False, w(3, 5), parent=31)]
    rows += [_row(32, True, w(3, 2)), _row(33, True, w(3, 3))]
    rows += [_row(34, True, w(3, 4), response=52), _row(52, False, w(4, 0, 1), 53, 34),
             _row(53, True, w(4, 0, 2), parent=52)]
    rows += [_row(35, True, w(3, 6))]
    rows += [_row(41, True, w(4, 2))]                                 # 2016-W14, partial
    return rows


CLI_IDS = [CLI_BASE + n for n in (1, 11, 12, 21, 22, 31, 32, 33, 34, 35, 41, 51, 52, 53)]
# The two smallest sha256("0:2016-W13:<id>") digests among 8800031..8800035, worked out
# by hand once: 8800031 (526d3101...) then 8800034 (9be80a9f...).
EXPECTED_SAMPLE_IDS = [8800031, 8800034]


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
            code = drift.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_config_is_clear_error(self):
        missing = self.dir / "no_such_drift.json"
        code, out, err = self.main(config=missing)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("error: "))
        self.assertIn(str(missing), err)
        self.assertFalse(self.report.exists())

    def test_empty_reference_exits_2(self):
        config = self.dir / "cfg.json"
        data = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))
        data["drift"]["reference"].update(start_week="2016-W30", end_week="2016-W31")
        config.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = self.main(config=config)
        self.assertEqual(code, 2)
        self.assertIn("error: reference range 2016-W30..2016-W31 holds no usable threads", err)
        self.assertFalse(self.report.exists())

    def test_samples_hash_selected_bounded_and_cut(self):
        samples = self.dir / "samples.jsonl"
        code, out, err = self.main("--samples-out", str(samples))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "weeks=5 callouts=1 samples_written=2 split=dev\n")
        first = samples.read_bytes()
        lines = [json.loads(x) for x in first.decode("utf-8").splitlines()]
        self.assertEqual([x["thread_id"] for x in lines], EXPECTED_SAMPLE_IDS)
        for x in lines:
            self.assertEqual(x["split"], "dev")
            self.assertEqual(x["week"], "2016-W13")  # only the flagged week is sampled
            self.assertEqual(x["signals"], ["volume"])
            for turn in x["turns"]:
                self.assertEqual(set(turn), {"tweet_id", "inbound", "created_at", "text"})
        # cut at the week's end: 31 keeps its in-week reply; 34 loses the later turns
        self.assertEqual([t["tweet_id"] for t in lines[0]["turns"]],
                         [CLI_BASE + 31, CLI_BASE + 51])
        self.assertEqual([t["tweet_id"] for t in lines[1]["turns"]], [CLI_BASE + 34])
        # same input, same file
        samples.unlink()
        self.assertEqual(self.main("--samples-out", str(samples))[0], 0)
        self.assertEqual(samples.read_bytes(), first)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["samples_written"], 2)

    def test_samples_out_inside_repo_refused(self):
        inside = REPO_ROOT / ("drift_samples_test_%d.jsonl" % os.getpid())
        self.addCleanup(lambda: inside.unlink() if inside.exists() else None)
        code, out, err = self.main("--samples-out", str(inside))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))
        self.assertFalse(inside.exists())
        self.assertFalse(self.report.exists())

    def test_eval_split_samples_refused(self):
        samples = self.dir / "samples.jsonl"
        code, out, err = self.main("--samples-out", str(samples), split="eval")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))
        self.assertFalse(samples.exists())
        self.assertFalse(self.report.exists())

    def test_report_aggregate_only_look_ahead_and_ci(self):
        md = self.dir / "report.md"
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.main("--format", "md", report=md)[0], 0)
        for path in (self.report, md):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SENTINEL", text, path.name)
            self.assertNotIn("thanks love it", text, path.name)
            for tid in CLI_IDS:
                self.assertNotIn(str(tid), text, path.name)
        md_text = md.read_text(encoding="utf-8")
        self.assertIn("cut_at_week_end", md_text)
        self.assertIn("thread_links_from_full_file", md_text)
        self.assertIn("| 2016-W13 |", md_text)

        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["look_ahead"]["turns"], "cut_at_week_end")
        self.assertEqual(report["look_ahead"]["reference"], "earlier_weeks_only")
        self.assertEqual(set(report["look_ahead"]["residual"]),
                         {"thread_links_from_full_file", "dataset_snapshot"})
        self.assertEqual(report["config_version"], "fixture-v1")
        self.assertEqual(report["split"], "dev")
        self.assertEqual(report["psi_epsilon"], 1e-4)
        self.assertEqual(report["samples_written"], 0)
        self.assertEqual(report["weeks"], 5)
        self.assertEqual(report["callouts"], 1)
        self.assertEqual(report["reference"]["weeks_used"], ["2016-W11", "2016-W12"])
        rows = {r["week"]: r for r in report["week_rows"]}
        self.assertEqual(sorted(rows), ["2016-W10", "2016-W11", "2016-W12", "2016-W13",
                                        "2016-W14"])
        w13 = rows["2016-W13"]
        self.assertEqual((w13["phase"], w13["volume"], w13["volume_ratio"]),
                         ("monitored", 5, 2.5))
        self.assertTrue(w13["directional"])  # volume under 30
        self.assertEqual(w13["intents"]["praise"]["count"], 5)
        for row in report["week_rows"]:
            n = row["volume"]
            for intent, cell in row["intents"].items():
                self.assertEqual(cell["ci"], wilson_interval(cell["count"], n), intent)
            for key in ("escalated", "other"):
                self.assertEqual(row[key]["ci"], wilson_interval(row[key]["count"], n))
        self.assertIsNone(rows["2016-W11"]["intent_psi"])
        self.assertEqual(report["call_outs"], [{"week": "2016-W13", "signal": "volume",
                                                "value": 2.5, "reference_value": 1.0,
                                                "threshold": 2.0}])


class TestLiveConformance(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TRIAGE_LIVE_CONFORMANCE") == "1",
                         "set TRIAGE_LIVE_CONFORMANCE=1 to load the owner's evals/drift.json")
    def test_live_drift_config_loads(self):
        config = drift.load_drift_config(REPO_ROOT / "evals" / "drift.json")
        self.assertEqual(set(config.thresholds), set(drift.SIGNAL_ORDER))


class TestModuleHygiene(unittest.TestCase):
    def test_stdlib_only(self):
        banned = {"urllib", "http", "socket", "requests", "ssl", "ftplib", "smtplib"}
        stdlib = set(sys.stdlib_module_names)
        src = (REPO_ROOT / "src" / "triage" / "drift.py").read_text(encoding="utf-8")
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


class TestReplay(unittest.TestCase):
    def test_order_iso_week_utc_gaps_partial(self):
        a = make_thread(BASE + 31, MON + timedelta(days=1), "a")             # W10
        b = make_thread(BASE + 32, MON + timedelta(days=0, hours=5), "b")    # W10, earlier
        # Sunday 2016-03-20 23:30 at UTC-05:00 is Monday 04:30 UTC: ISO week W12, not W11
        tz5 = timezone(timedelta(hours=-5))
        c = make_thread(BASE + 33, datetime(2016, 3, 20, 23, 30, tzinfo=tz5), "c")
        d = make_thread(BASE + 34, MON + timedelta(days=28), "d")            # W14
        empty = Thread(root_tweet_id=BASE + 35, brand_ids=(), turns=(), brand_replied=False)
        buckets = drift.replay_weeks([d, c, empty, a, b])
        self.assertEqual([x.week for x in buckets],
                         ["2016-W10", "2016-W11", "2016-W12", "2016-W13", "2016-W14"])
        self.assertEqual([[t.root_tweet_id for t in x.threads] for x in buckets],
                         [[BASE + 32, BASE + 31], [], [BASE + 33], [], [BASE + 34]])
        self.assertEqual([x.partial for x in buckets], [True, False, False, False, True])
        self.assertEqual(buckets[0].week_end, datetime(2016, 3, 14, tzinfo=UTC))
        self.assertEqual(drift.replay_weeks([]), [])
        only = drift.replay_weeks([a])
        self.assertEqual(len(only), 1)
        self.assertTrue(only[0].partial)


if __name__ == "__main__":
    unittest.main()
