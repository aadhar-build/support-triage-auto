"""Tests for triage.model_prices. Prices here are made up; no owner file is read."""

import json
import tempfile
import unittest
from pathlib import Path

from triage.model_prices import PricesError, UnknownBillError, load_prices
from triage.model_tier import TokenUsage

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "model_prices.json"


class TestLoadPrices(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return path

    def test_fixture_bands(self):
        prices = load_prices(FIXTURE)
        self.assertEqual(prices.version, "test-fixture")
        first = prices.band("fixture-model", 100000)
        second = prices.band("fixture-model", 100001)
        self.assertEqual((first.max_input_tokens, first.input, first.output),
                         (100000, 1.0, 5.0))
        self.assertIsNone(first.cache_write)
        self.assertEqual((second.max_input_tokens, second.input, second.output,
                          second.cache_write, second.cache_read), (None, 2.0, 10.0, 2.5, 0.2))

    def test_cost(self):
        prices = load_prices(FIXTURE)
        self.assertAlmostEqual(prices.cost("fixture-model", TokenUsage(1000, 50, 0, 0)), 0.00125)
        with self.assertRaises(UnknownBillError):
            prices.cost("other-model", TokenUsage(1, 1, 0, 0))
        with self.assertRaises(UnknownBillError):
            prices.cost("fixture-model", TokenUsage(10, 1, 5, 0))
        # cache tokens count toward the band; band 2 has cache prices
        self.assertAlmostEqual(
            prices.cost("fixture-model", TokenUsage(100000, 0, 0, 1)),
            (100000 * 2.0 + 1 * 0.2) / 1e6)

    def test_malformed(self):
        good = json.loads(FIXTURE.read_text(encoding="utf-8"))

        def variant(fn):
            d = json.loads(json.dumps(good))
            fn(d)
            return d

        bands = lambda d: d["models"]["fixture-model"]["bands"]  # noqa: E731
        cases = {
            "missing file": None,
            "bad json": "{not json",
            "empty models": variant(lambda d: d.update(models={})),
            "missing version": variant(lambda d: d.pop("version")),
            "empty bands": variant(lambda d: bands(d).clear()),
            "out of order": variant(lambda d: bands(d).reverse()),
            "non-final null": variant(lambda d: bands(d)[0].update(max_input_tokens=None)),
            "zero price": variant(lambda d: bands(d)[0].update(input_usd_per_mtok=0)),
            "nan price": '{"version": "v", "models": {"m": {"bands": [{"max_input_tokens": null, '
                         '"input_usd_per_mtok": NaN, "output_usd_per_mtok": 1}]}}}',
            "inf price": variant(lambda d: bands(d)[1].update(output_usd_per_mtok=float("inf"))),
        }
        for name, content in cases.items():
            with self.subTest(name):
                fname = name.replace(" ", "_") + ".json"
                path = self.dir / fname if content is None else self.write(fname, content)
                with self.assertRaises(PricesError) as cm:
                    load_prices(path)
                self.assertIn(fname, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
