"""Tests for triage.model_tier. No test starts a `claude` process or opens a connection.

Threads are hand-written; prices are tests/fixtures/model_prices.json (made up); every
spend log lives in a temp dir.
"""

import json
import math
import os
import socket
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from triage import model_tier, quoting
from triage.cheap_tier import CheapTier
from triage.model_prices import load_prices
from triage.model_tier import (MAX_RUN_SPEND_USD, PROMPT_PATH, PROMPT_VERSION, STRIPPED_ENV,
                               ClaudeCodeClient, FakeModelClient, ModelCallError,
                               ModelLargerTier, ModelReply, TokenUsage, load_prompt,
                               parse_prompt_version)
from triage.spend_log import SpendLog
from triage.threads import Thread, Turn
from triage.tiers import ACTIONS, INTENTS, URGENCIES, Labels

REPO_ROOT = Path(__file__).resolve().parents[1]
PRICES = load_prices(REPO_ROOT / "tests" / "fixtures" / "model_prices.json")
MODEL = "fixture-model"
NOV = datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)
T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)
GOOD = '{"intent": "billing", "urgency": "high", "action": "route", "confidence": 0.8}'


def thread(root=1, text="my package never arrived SENTINEL_TURN"):
    return Thread(root_tweet_id=root, brand_ids=("brandx",), brand_replied=False,
                  turns=(Turn(root, "cust_x", True, T0, text),))


def reply(text=GOOD, inp=1000, out=50, model=MODEL, **cache):
    return ModelReply(text=text, usage={model: TokenUsage(inp, out, **cache)})


class TierBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.log_path = self.dir / "spend.jsonl"
        SpendLog.create(self.log_path)
        self.log = SpendLog(self.log_path, 1.0, clock=lambda: NOV)
        self.cheap = CheapTier()

    def tier(self, replies, **kw):
        client = kw.pop("client", None) or FakeModelClient(replies)
        args = dict(client=client, fallback=self.cheap, prices=PRICES, spend_log=self.log,
                    model=MODEL, run_id="run-1", split="dev", max_spend_usd=2.5)
        args.update(kw)
        return ModelLargerTier(**args), client

    def events(self):
        return [json.loads(x) for x in self.log_path.read_text(encoding="utf-8").splitlines()]


class TestPrompt(unittest.TestCase):
    def test_prompt_file(self):
        raw = PROMPT_PATH.read_text(encoding="utf-8")
        lines = raw.splitlines()
        self.assertEqual(lines[0], "<!-- prompt-version: larger-tier-v1 -->")
        self.assertEqual(PROMPT_VERSION, "larger-tier-v1")
        self.assertTrue(lines[1].startswith("<!--") and lines[1].endswith("-->"))
        for phrase in ("dev or synthetic", "never on eval", "versioned PR", "Aadhar merges",
                       "once, after the merge"):
            self.assertIn(phrase, lines[1])
        self.assertNotIn("{{THREAD}}", raw)
        rendered = load_prompt()
        for value in INTENTS + URGENCIES + ACTIONS:
            self.assertIn(value, rendered)
        self.assertNotIn("{{", rendered)
        self.assertTrue(rendered.startswith(lines[0] + "\n" + lines[1] + "\n"))
        raw.encode("ascii")

    def test_bad_version_line(self):
        for bad in ("", "no header\n", "<!-- prompt-version: -->\n", "<!-- version: v1 -->\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_prompt_version(bad)
        path = Path(tempfile.mkdtemp()) / "p.md"
        self.addCleanup(lambda: (path.unlink(), path.parent.rmdir()))
        path.write_text("hello\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_prompt(path)


class TestLabel(TierBase):
    def test_valid_reply(self):
        tier, fake = self.tier([reply()])
        t = thread()
        self.assertEqual(tier.label(t), Labels("billing", "high", "route", 0.8))
        self.assertEqual(fake.calls, [(load_prompt(), quoting.quote_conversation(t))])
        self.assertEqual(tier.answered, 1)
        self.assertEqual(tier.model_id, MODEL)
        self.assertEqual(tier.name, "larger")

    def test_uses_quote_conversation(self):
        tier, fake = self.tier([reply()])
        with mock.patch.object(quoting, "quote_conversation", return_value="SENTINEL_QUOTED"):
            tier.label(thread())
        system, user = fake.calls[0]
        self.assertEqual(user, "SENTINEL_QUOTED")
        self.assertNotIn("SENTINEL_TURN", system)
        self.assertNotIn("SENTINEL_TURN", user)


class TestFallback(TierBase):
    def test_bad_replies_fall_back(self):
        t = thread()
        cheap = self.cheap.label(t)

        def obj(**over):
            d = {"intent": "billing", "urgency": "high", "action": "route", "confidence": 0.8}
            d.update(over)
            return json.dumps({k: v for k, v in d.items() if v is not ...})

        cases = {
            "unparseable": ("not json at all", "parse_error"),
            "broken braces": ("{intent: billing}", "parse_error"),
            "non-object": ("[1, 2]", "parse_error"),
            "unknown intent": (obj(intent="weather"), "invalid_labels"),
            "unknown urgency": (obj(urgency="urgent"), "invalid_labels"),
            "unknown action": (obj(action="refund"), "invalid_labels"),
            "missing key": (obj(action=...), "invalid_labels"),
            "confidence 1.5": (obj(confidence=1.5), "invalid_labels"),
            "confidence -0.1": (obj(confidence=-0.1), "invalid_labels"),
            "confidence string": (obj(confidence="0.9"), "invalid_labels"),
            "confidence bool": (obj(confidence=True), "invalid_labels"),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(name):
                tier, _ = self.tier([reply(text=text)])
                self.assertEqual(tier.label(t), cheap)
                self.assertEqual(tier.fallbacks[reason], 1)
                self.assertEqual(sum(tier.fallbacks.values()), 1)
                self.assertEqual(tier.answered, 0)
                self.assertIsNone(tier.stopped_by)

    def test_extra_keys_and_surrounding_text_ok(self):
        tier, _ = self.tier([reply(text='Sure: {"intent": "praise", "urgency": "low", '
                                        '"action": "answer", "confidence": 1, "x": 2} done')])
        self.assertEqual(tier.label(thread()), Labels("praise", "low", "answer", 1))

    def test_call_error_fails_closed(self):
        tier, fake = self.tier([ModelCallError("model call failed: exit 1"), reply()])
        t = thread()
        self.assertEqual(tier.label(t), self.cheap.label(t))
        self.assertEqual(tier.fallbacks["api_error"], 1)
        self.assertEqual(tier.stopped_by, "unknown_bill")
        self.assertEqual(tier.confirmed_usd, 0.0)
        reserved = [e for e in self.events() if e["event"] == "reserve"]
        self.assertEqual(len(reserved), 1)
        self.assertAlmostEqual(tier.unconfirmed_usd, reserved[0]["usd"])
        self.assertGreater(tier.unconfirmed_usd, 0)
        self.assertEqual([e["event"] for e in self.events()], ["open", "reserve", "unknown"])
        self.assertEqual(self.events()[2]["id"], reserved[0]["id"])
        # the next thread falls back as unknown_bill without a call
        self.assertEqual(tier.label(thread(2)), self.cheap.label(thread(2)))
        self.assertEqual(tier.fallbacks["unknown_bill"], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(self.events()), 3)


class TestSpend(TierBase):
    def test_worked_example(self):
        tier, _ = self.tier([reply(inp=1000, out=50), reply(inp=2000, out=100)])
        tier.label(thread(1))
        tier.label(thread(2))
        self.assertEqual((tier.input_tokens, tier.output_tokens), (3000, 150))
        self.assertEqual((tier.cache_write_tokens, tier.cache_read_tokens), (0, 0))
        self.assertAlmostEqual(tier.confirmed_usd, 0.00375)
        self.assertEqual(tier.unconfirmed_usd, 0)
        self.assertAlmostEqual(tier.spent_usd, 0.00375)
        self.assertIsNone(tier.stopped_by)
        ev = self.events()
        self.assertEqual([e["event"] for e in ev],
                         ["open", "reserve", "settle", "reserve", "settle"])
        self.assertEqual(ev[2]["id"], ev[1]["id"])
        self.assertEqual(ev[4]["id"], ev[3]["id"])
        self.assertAlmostEqual(ev[2]["usd"], 0.00125)
        self.assertAlmostEqual(ev[4]["usd"], 0.0025)
        self.assertEqual((ev[2]["input_tokens"], ev[2]["output_tokens"]), (1000, 50))
        self.assertEqual((ev[1]["run_id"], ev[1]["model"], ev[1]["split"]),
                         ("run-1", MODEL, "dev"))

    def _overhead_for(self, total, t):
        # make system + user + overhead == total input-token estimate
        size = len(load_prompt().encode("utf-8")) + \
            len(quoting.quote_conversation(t).encode("utf-8"))
        return total - size

    def test_run_cap_stops_before_crossing(self):
        # system+user is fixed at 150 bytes by patching the quoting seam; overhead 850
        t = thread()
        with mock.patch.object(quoting, "quote_conversation", return_value="x" * 50):
            sys_len = len(load_prompt().encode("utf-8"))
            overhead = 850 + 100 - sys_len  # 150 bytes of "system + user", then 850 overhead
            ok, _ = self.tier([reply()], overhead_tokens=overhead, reserve_output_tokens=100,
                              max_spend_usd=0.0015)
            ok.label(t)
            self.assertAlmostEqual(self.events()[1]["usd"], 0.0015)

            tier, fake = self.tier([reply()], overhead_tokens=overhead,
                                   reserve_output_tokens=100, max_spend_usd=0.0014)
            before = self.log_path.read_bytes()
            self.assertEqual(tier.label(t), self.cheap.label(t))
            self.assertEqual(tier.stopped_by, "spend_cap")
            self.assertEqual(fake.calls, [])
            self.assertEqual(self.log_path.read_bytes(), before)
        with mock.patch.object(quoting, "quote_conversation",
                               side_effect=AssertionError("rendered after stop")):
            tier.label(thread(2))
        self.assertEqual(tier.fallbacks["spend_cap"], 2)

    def test_reservation_exceeded_stops(self):
        tier, fake = self.tier([reply(inp=90000, out=1000), reply()])
        tier.label(thread(1))
        self.assertEqual(tier.reservation_exceeded, 1)
        self.assertEqual(tier.stopped_by, "reservation_exceeded")
        ev = self.events()
        self.assertEqual([e["event"] for e in ev], ["open", "reserve", "settle"])
        self.assertAlmostEqual(ev[2]["usd"], 0.095)
        self.assertGreater(ev[2]["usd"], ev[1]["usd"])
        self.assertAlmostEqual(tier.confirmed_usd, 0.095)
        tier.label(thread(2))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(tier.fallbacks["reservation_exceeded"], 1)

    def test_too_long(self):
        tier, fake = self.tier([reply()], max_input_tokens=100)
        tier.label(thread())
        self.assertEqual(tier.fallbacks["too_long"], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.events()), 1)
        self.assertIsNone(tier.stopped_by)

    def test_zero_cap_makes_no_calls(self):
        tier, fake = self.tier([reply()] * 3, max_spend_usd=0)
        for i in range(3):
            tier.label(thread(i + 1))
        self.assertEqual(fake.calls, [])
        self.assertEqual(tier.stopped_by, "spend_cap")
        self.assertEqual(len(self.events()), 1)

    def test_cap_bounds(self):
        self.assertEqual(MAX_RUN_SPEND_USD, 2.50)
        for bad in (2.51, -0.01, float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.tier([], max_spend_usd=bad)
        self.tier([], max_spend_usd=2.5)

    def test_monthly_cap(self):
        self.log_path.write_text(
            self.log_path.read_text(encoding="utf-8")
            + json.dumps({"event": "reserve", "id": "a" * 32, "at": NOV.isoformat(),
                          "run_id": "old", "model": MODEL, "split": "dev", "usd": 0.0095}) + "\n"
            + json.dumps({"event": "settle", "id": "a" * 32, "at": NOV.isoformat(),
                          "usd": 0.0095, "input_tokens": 1, "output_tokens": 1,
                          "cache_write_tokens": 0, "cache_read_tokens": 0}) + "\n",
            encoding="utf-8")
        self.log = SpendLog(self.log_path, 0.01, clock=lambda: NOV)
        t = thread()
        tier, fake = self.tier([reply()], overhead_tokens=self._overhead_for(1000, t),
                               reserve_output_tokens=0)
        before = self.log_path.read_bytes()
        tier.label(t)
        self.assertEqual(tier.stopped_by, "monthly_cap")
        self.assertEqual(tier.fallbacks["monthly_cap"], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_spend_log_error_stops(self):
        self.log_path.unlink()
        tier, fake = self.tier([reply()])
        tier.label(thread())
        self.assertEqual(tier.stopped_by, "spend_log")
        self.assertEqual(fake.calls, [])

    def test_unknown_bill(self):
        cases = {
            "model not in price file": reply(model="other-model"),
            "cache tokens without cache price": reply(cache_read_tokens=5),
            "no usage": ModelReply(text=GOOD, usage={}),
        }
        for name, r in cases.items():
            with self.subTest(name):
                tier, fake = self.tier([r, reply()])
                labels = tier.label(thread())
                self.assertEqual(labels, Labels("billing", "high", "route", 0.8))
                self.assertEqual(tier.stopped_by, "unknown_bill")
                self.assertEqual(tier.confirmed_usd, 0)
                last_two = self.events()[-2:]
                self.assertEqual([e["event"] for e in last_two], ["reserve", "unknown"])
                self.assertAlmostEqual(tier.unconfirmed_usd, last_two[0]["usd"])
                tier.label(thread(2))
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(tier.fallbacks["unknown_bill"], 1)

    def test_unknown_model_at_reservation(self):
        tier, fake = self.tier([reply()], model="not-priced")
        tier.label(thread())
        self.assertEqual(tier.stopped_by, "unknown_bill")
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.events()), 1)

    def test_fallback_keys(self):
        tier, _ = self.tier([])
        self.assertEqual(set(tier.fallbacks), {
            "spend_cap", "monthly_cap", "spend_log", "unknown_bill", "reservation_exceeded",
            "api_error", "too_long", "parse_error", "invalid_labels"})


SUCCESS = json.dumps({"type": "result", "is_error": False, "result": GOOD,
                      "modelUsage": {MODEL: {"inputTokens": 120, "outputTokens": 30,
                                             "cacheCreationInputTokens": 4,
                                             "cacheReadInputTokens": 5},
                                     "other": {"inputTokens": 1, "outputTokens": 2}}})


class FakeRunner:
    def __init__(self, stdout=SUCCESS, returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class TestClaudeCodeClient(unittest.TestCase):
    def test_invocation(self):
        runner = FakeRunner()
        client = ClaudeCodeClient("fixture-model", claude_bin="/opt/x/claude", runner=runner,
                                  timeout_s=33)
        env = {name: "SENTINEL_ENV_%d" % i for i, name in enumerate(STRIPPED_ENV)}
        with mock.patch.dict(os.environ, env):
            client.complete("SYSTEM_TEXT", "SENTINEL_USER")
        argv, kw = runner.calls[0]
        self.assertEqual(argv, ["/opt/x/claude", "-p", "--output-format", "json", "--model",
                                "fixture-model", "--system-prompt", "SYSTEM_TEXT", "--tools",
                                "", "--safe-mode", "--strict-mcp-config",
                                "--no-session-persistence"])
        self.assertNotIn("--bare", argv)
        for arg in argv:
            self.assertNotIn("SENTINEL_USER", arg)
        self.assertEqual(kw["input"], "SENTINEL_USER")
        self.assertTrue(kw["capture_output"])
        self.assertTrue(kw["text"])
        self.assertEqual(kw["timeout"], 33)
        self.assertNotIn("shell", kw)
        cwd = Path(kw["cwd"]).resolve()
        self.assertNotEqual(cwd, REPO_ROOT)
        self.assertNotIn(REPO_ROOT, cwd.parents)
        for name in STRIPPED_ENV:
            self.assertNotIn(name, kw["env"])
        self.assertEqual(set(STRIPPED_ENV), {
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"})

    def test_parse(self):
        r = ClaudeCodeClient("fixture-model", runner=FakeRunner()).complete("s", "u")
        self.assertEqual(r.text, GOOD)
        self.assertEqual(r.usage, {MODEL: TokenUsage(120, 30, 4, 5),
                                   "other": TokenUsage(1, 2, 0, 0)})

    def _errors(self):
        def out(**over):
            d = {"is_error": False, "result": "SENTINEL_STDOUT",
                 "modelUsage": {MODEL: {"inputTokens": 1, "outputTokens": 1}}}
            d.update(over)
            return json.dumps(d)
        return {
            "non-zero exit": FakeRunner(stdout="SENTINEL_STDOUT", returncode=1,
                                        stderr="SENTINEL_STDERR"),
            "timeout": FakeRunner(raises=subprocess.TimeoutExpired(["x"], 1,
                                                                   output="SENTINEL_STDOUT")),
            "oserror": FakeRunner(raises=FileNotFoundError("SENTINEL_STDERR")),
            "non-json": FakeRunner(stdout="SENTINEL_STDOUT not json", stderr="SENTINEL_STDERR"),
            "is_error true": FakeRunner(stdout=out(is_error=True)),
            "missing modelUsage": FakeRunner(stdout=json.dumps(
                {"is_error": False, "result": "SENTINEL_STDOUT"})),
            "empty modelUsage": FakeRunner(stdout=out(modelUsage={})),
            "negative count": FakeRunner(stdout=out(
                modelUsage={MODEL: {"inputTokens": -1, "outputTokens": 1}})),
            "non-int count": FakeRunner(stdout=out(
                modelUsage={MODEL: {"inputTokens": 1.5, "outputTokens": 1}})),
            "undecodable output": FakeRunner(raises=UnicodeDecodeError(
                "utf-8", b"SENTINEL_STDOUT", 0, 1, "SENTINEL_STDOUT")),
            "bool count": FakeRunner(stdout=out(
                modelUsage={MODEL: {"inputTokens": True, "outputTokens": 1}})),
        }

    def test_errors_are_opaque(self):
        env = {name: "SENTINEL_ENV" for name in STRIPPED_ENV}
        for name, runner in self._errors().items():
            with self.subTest(name):
                client = ClaudeCodeClient("fixture-model", runner=runner)
                with mock.patch.dict(os.environ, env):
                    with self.assertRaises(ModelCallError) as cm:
                        client.complete("SENTINEL_SYSTEM", "SENTINEL_USER")
                for text in (str(cm.exception), repr(cm.exception), repr(client)):
                    self.assertNotIn("SENTINEL", text)
                self.assertTrue(str(cm.exception).startswith("model call failed: "))
                self.assertIsNone(cm.exception.__cause__)
        self.assertEqual(repr(ClaudeCodeClient("fixture-model")),
                         "ClaudeCodeClient(model='fixture-model')")


def _forbidden(*a, **kw):
    raise AssertionError("a process or network call was attempted")


class TestHygiene(TierBase):
    def test_no_process_or_network(self):
        runner = FakeRunner()
        fake_subprocess = types.SimpleNamespace(run=runner)
        with mock.patch.object(subprocess, "run", _forbidden), \
                mock.patch.object(subprocess, "Popen", _forbidden), \
                mock.patch.object(socket.socket, "connect", _forbidden), \
                mock.patch.object(model_tier, "subprocess", fake_subprocess):
            client = ClaudeCodeClient(MODEL)  # no explicit runner: looks up subprocess.run
            settled = json.dumps({"is_error": False, "result": GOOD, "modelUsage": {
                MODEL: {"inputTokens": 100, "outputTokens": 10}}})
            outputs = [settled, SUCCESS, "not json", json.dumps({"is_error": True})]
            for i, stdout in enumerate(outputs):
                runner.stdout = stdout
                tier, _ = self.tier(None, client=client)
                tier.label(thread(i + 1))
                tier.label(thread(i + 10))
            runner.raises = subprocess.TimeoutExpired(["x"], 1)
            tier, _ = self.tier(None, client=client)
            tier.label(thread(20))
            for kw in (dict(max_spend_usd=0), dict(max_input_tokens=10),
                       dict(model="not-priced")):
                tier, _ = self.tier(None, client=client, **kw)
                tier.label(thread(30))
        self.assertGreaterEqual(len(runner.calls), 5)
        self.assertIn("settle", [e["event"] for e in self.events()])


if __name__ == "__main__":
    unittest.main()
