"""Tests for triage.split. All ids and fixtures are made up by hand; no real dataset rows."""

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from triage.loader import Tweet
from triage.split import (
    DEFAULT_SPLIT_PATH,
    SPLIT_NAMES,
    SplitConfig,
    SplitConfigError,
    SplitReport,
    assign_split,
    config_from_dict,
    load_split_config,
    split_threads,
)
from triage.threads import Thread, threads_from_tweets

REPO_ROOT = Path(__file__).resolve().parents[1]
IDS = list(range(1, 10001))
N = len(IDS)

# Share tolerances: about 3 binomial standard deviations at n = 10,000,
# i.e. 3 * sqrt(p * (1 - p) / 10000).
#   dev  p=0.10: 3 * sqrt(0.10 * 0.90 / 10000) = 3 * 0.00300 = 0.0090
#   eval p=0.05: 3 * sqrt(0.05 * 0.95 / 10000) = 3 * 0.00218 = 0.0065
#   full p=0.85: 3 * sqrt(0.85 * 0.15 / 10000) = 3 * 0.00357 = 0.0107 -> 0.011
TOL_DEV = 0.009
TOL_EVAL = 0.0065
TOL_FULL = 0.011

BASE = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)


def tw(tweet_id, author_id="cust_a", inbound=True, minute=0, text=None, responses=(),
       in_response_to=None):
    return Tweet(
        tweet_id=tweet_id,
        author_id=author_id,
        inbound=inbound,
        created_at=BASE + timedelta(minutes=minute),
        text=text if text is not None else "SENTINEL_TEXT_%d" % tweet_id,
        response_tweet_ids=tuple(responses),
        in_response_to_tweet_id=in_response_to,
    )


def brand(tweet_id, author_id="BrandA", **kw):
    return tw(tweet_id, author_id=author_id, inbound=False, **kw)


def cfg_dict(seed=20261115, dev=0.1, eval_=0.05, full=0.85):
    return {"seed": seed, "unit": "thread",
            "fractions": {"dev": dev, "eval": eval_, "full": full}}


def cfg(**kw):
    return config_from_dict(cfg_dict(**kw))


def by_split(mapping):
    groups = {name: set() for name in SPLIT_NAMES}
    for key, name in mapping.items():
        groups[name].add(key)
    return groups


class TestLoadConfig(unittest.TestCase):
    def test_loads_repo_config_read_only(self):
        self.assertEqual(Path(DEFAULT_SPLIT_PATH), REPO_ROOT / "data" / "split.json")
        before = Path(DEFAULT_SPLIT_PATH).read_bytes()
        config = load_split_config()
        after = Path(DEFAULT_SPLIT_PATH).read_bytes()
        self.assertEqual(before, after)
        self.assertIsInstance(config, SplitConfig)
        self.assertEqual(config.seed, 20261115)
        self.assertEqual(dict(config.fractions), {"dev": 0.1, "eval": 0.05, "full": 0.85})
        self.assertEqual(tuple(n for n, _ in config.fractions), SPLIT_NAMES)

    def test_loads_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "split.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg_dict(seed=7), f)
            self.assertEqual(load_split_config(p).seed, 7)


class TestConfigValidation(unittest.TestCase):
    def test_file_failures(self):
        with tempfile.TemporaryDirectory() as d:
            with self.subTest("missing file"):
                with self.assertRaises(SplitConfigError) as cm:
                    load_split_config(os.path.join(d, "nope.json"))
                self.assertIsNotNone(cm.exception.__cause__)
            with self.subTest("invalid JSON"):
                p = os.path.join(d, "bad.json")
                with open(p, "w", encoding="utf-8") as f:
                    f.write("{not json")
                with self.assertRaises(SplitConfigError) as cm:
                    load_split_config(p)
                self.assertIsNotNone(cm.exception.__cause__)

    def test_bad_dicts(self):
        def without(key):
            d = cfg_dict()
            del d[key]
            return d

        def with_(key, value):
            d = cfg_dict()
            d[key] = value
            return d

        def fracs(**f):
            d = cfg_dict()
            d["fractions"] = f
            return d

        cases = {
            "not a dict": [("seed", 1)],
            "missing seed": without("seed"),
            "string seed": with_("seed", "20261115"),
            "float seed": with_("seed", 1.0),
            "bool seed": with_("seed", True),
            "missing unit": without("unit"),
            "wrong unit": with_("unit", "tweet"),
            "missing fractions": without("fractions"),
            "list fractions": with_("fractions", [0.1, 0.05, 0.85]),
            "missing split name": fracs(dev=0.15, full=0.85),
            "extra split name": fracs(dev=0.1, eval=0.05, full=0.8, test=0.05),
            "string fraction": fracs(dev="0.1", eval=0.05, full=0.85),
            "bool fraction": fracs(dev=False, eval=True, full=0.0),
            "none fraction": fracs(dev=None, eval=0.05, full=0.95),
            "negative fraction": fracs(dev=-0.1, eval=0.25, full=0.85),
            "NaN fraction": fracs(dev=float("nan"), eval=0.05, full=0.85),
            "infinite fraction": fracs(dev=float("inf"), eval=0.05, full=0.85),
            "sum below 1": fracs(dev=0.1, eval=0.05, full=0.8),
            "sum above 1": fracs(dev=0.1, eval=0.05, full=0.9),
            "sum just outside tolerance": fracs(dev=0.1, eval=0.05, full=0.85 + 1e-6),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(SplitConfigError):
                    config_from_dict(data)

    def test_float_rounding_accepted(self):
        # 0.1 + 0.05 + 0.85 may differ from 1 by float rounding only; it must pass.
        config = cfg()
        cfg(dev=0.7, eval_=0.2, full=0.1)  # 0.7 + 0.2 + 0.1 = 0.9999999999999999
        self.assertEqual(dict(config.fractions)["full"], 0.85)
        # Ints are fine, extra top-level keys are ignored.
        d = cfg_dict(dev=0, eval_=0, full=1)
        d["decided"] = "someone, some day"
        self.assertEqual(dict(config_from_dict(d).fractions)["full"], 1)

    def test_error_is_value_error(self):
        self.assertTrue(issubclass(SplitConfigError, ValueError))


class TestNoLeakage(unittest.TestCase):
    def test_ids_in_exactly_one_split(self):
        mapping, _ = split_threads(IDS, load_split_config())
        self.assertEqual(set(mapping), set(IDS))
        self.assertEqual(len(mapping), N)
        for v in mapping.values():
            self.assertIn(v, SPLIT_NAMES)
        groups = by_split(mapping)
        names = list(SPLIT_NAMES)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertFalse(groups[a] & groups[b], (a, b))
        self.assertEqual(set().union(*groups.values()), set(IDS))

    def test_tweets_follow_their_thread(self):
        tweets = []
        # 30 small threads: customer root, brand reply, customer follow-up.
        for k in range(30):
            root = 1000 + 10 * k
            tweets.append(tw(root, author_id="cust_%d" % k, minute=k, responses=(root + 1,)))
            tweets.append(brand(root + 1, minute=k + 1, in_response_to=root,
                                responses=(root + 2,)))
            tweets.append(tw(root + 2, author_id="cust_%d" % k, minute=k + 2,
                             in_response_to=root + 1))
        threads, _ = threads_from_tweets(tweets)
        self.assertEqual(len(threads), 30)
        mapping, _ = split_threads(threads, load_split_config())

        tweet_split = {}
        for th in threads:
            s = mapping[th.root_tweet_id]
            for turn in th.turns:
                self.assertNotIn(turn.tweet_id, tweet_split)
                tweet_split[turn.tweet_id] = s
                self.assertEqual(tweet_split[turn.tweet_id], s)
        self.assertEqual(set(tweet_split), {t.tweet_id for t in tweets})
        for th in threads:
            self.assertEqual({tweet_split[t.tweet_id] for t in th.turns},
                             {mapping[th.root_tweet_id]})


class TestDeterminism(unittest.TestCase):
    def test_order_independent(self):
        config = load_split_config()
        shuffled = list(IDS)
        random.Random(12345).shuffle(shuffled)
        self.assertNotEqual(shuffled, IDS)
        a, ra = split_threads(IDS, config)
        b, rb = split_threads(shuffled, config)
        self.assertEqual(a, b)
        self.assertEqual(ra, rb)

    def test_repeat_calls_equal(self):
        config = load_split_config()
        a, ra = split_threads(IDS, config)
        b, rb = split_threads(iter(IDS), config)
        self.assertEqual(a, b)
        self.assertEqual(ra, rb)

    def test_stable_across_hash_seeds(self):
        ids = list(range(1, 201)) + [10 ** 18 + 7, 1059823406227132416]
        config = cfg(seed=99)
        expected = json.dumps([assign_split(i, config) for i in ids])
        code = (
            "import json, sys\n"
            "from triage.split import assign_split, config_from_dict\n"
            "c = config_from_dict({'seed': 99, 'unit': 'thread',"
            " 'fractions': {'dev': 0.1, 'eval': 0.05, 'full': 0.85}})\n"
            "ids = json.loads(sys.argv[1])\n"
            "print(json.dumps([assign_split(i, c) for i in ids]))\n"
        )
        for hs in ("1", "2"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = hs
            env["PYTHONPATH"] = str(REPO_ROOT / "src")
            out = subprocess.run(
                [sys.executable, "-c", code, json.dumps(ids)],
                env=env, capture_output=True, text=True, check=True, timeout=60,
            )
            self.assertEqual(out.stdout.strip(), expected, "PYTHONHASHSEED=" + hs)

    def test_different_seed_changes_assignment(self):
        a, _ = split_threads(IDS, cfg(seed=20261115))
        b, _ = split_threads(IDS, cfg(seed=1))
        self.assertTrue(any(a[i] != b[i] for i in IDS))


class TestShares(unittest.TestCase):
    def test_shares_near_target(self):
        _, report = split_threads(IDS, load_split_config())
        self.assertEqual(report.total, N)
        self.assertLessEqual(abs(report.shares["dev"] - 0.10), TOL_DEV)
        self.assertLessEqual(abs(report.shares["eval"] - 0.05), TOL_EVAL)
        self.assertLessEqual(abs(report.shares["full"] - 0.85), TOL_FULL)


class TestReport(unittest.TestCase):
    def test_report_fields(self):
        config = load_split_config()
        ids = IDS[:777]
        mapping, report = split_threads(ids, config)
        self.assertIsInstance(report, SplitReport)
        self.assertEqual(report.seed, config.seed)
        self.assertEqual(report.total, len(set(ids)))
        self.assertEqual(set(report.counts), set(SPLIT_NAMES))
        self.assertEqual(sum(report.counts.values()), report.total)
        for name in SPLIT_NAMES:
            self.assertEqual(report.counts[name],
                             sum(1 for v in mapping.values() if v == name))
            self.assertEqual(report.shares[name], report.counts[name] / report.total)
        self.assertEqual(report.targets, dict(config.fractions))

    def test_duplicates_counted_once(self):
        config = load_split_config()
        th42 = Thread(root_tweet_id=42, brand_ids=(), turns=(), brand_replied=False)
        items = [42, 7, th42, 8, 42]
        mapping, report = split_threads(items, config)
        self.assertEqual(set(mapping), {7, 8, 42})
        self.assertEqual(mapping[42], assign_split(42, config))
        self.assertEqual(report.total, 3)
        self.assertEqual(sum(report.counts.values()), report.total)

    def test_empty_input(self):
        mapping, report = split_threads([], load_split_config())
        self.assertEqual(mapping, {})
        self.assertEqual(report.total, 0)
        self.assertEqual(report.counts, {n: 0 for n in SPLIT_NAMES})
        self.assertEqual(report.shares, {n: 0.0 for n in SPLIT_NAMES})

    def test_iterates_once(self):
        consumed = []

        def gen():
            for i in IDS[:50]:
                consumed.append(i)
                yield i

        mapping, _ = split_threads(gen(), load_split_config())
        self.assertEqual(len(mapping), 50)
        self.assertEqual(consumed, IDS[:50])


class TestAssign(unittest.TestCase):
    def test_degenerate_fractions(self):
        all_full = cfg(dev=0, eval_=0, full=1)
        all_dev = cfg(dev=1, eval_=0, full=0)
        for i in IDS[:2000]:
            self.assertEqual(assign_split(i, all_full), "full")
            self.assertEqual(assign_split(i, all_dev), "dev")

    def test_bad_key_types(self):
        config = load_split_config()
        for bad in (True, False, "42", 4.2, None, (42,)):
            with self.subTest(key=repr(bad)):
                with self.assertRaises(TypeError):
                    assign_split(bad, config)
        for bad in ("42", 4.2, None, True, object()):
            with self.subTest(item=repr(bad)):
                with self.assertRaises(TypeError):
                    split_threads([1, bad], config)

    def test_known_recipe(self):
        import hashlib
        config = load_split_config()
        for key in (1, 2, 3, 42, 10 ** 15):
            d = hashlib.sha256(("%d:%d" % (config.seed, key)).encode("ascii")).digest()
            u = int.from_bytes(d[:8], "big") / 2 ** 64
            want = "dev" if u < 0.1 else ("eval" if u < 0.1 + 0.05 else "full")
            self.assertEqual(assign_split(key, config), want)


if __name__ == "__main__":
    unittest.main()
