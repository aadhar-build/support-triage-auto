"""Tests for python3 -m triage.propose_change.

Every test builds a temp repo layout from the flat, made-up fixtures under tests/fixtures/
and passes --repo pointing at it; the owner's own files are never read (the opt-in
TestLiveConformance aside). The change path runs only through FakeChangeClient: no test
runs a real git push, git fetch or gh, and no test configures a git remote. Expected numbers
are worked out by hand from the fixture cases (see the comments), never with the code's own
metric helpers.

Fixture cases (n=6), as the keyword cheap tier labels them:
  1 pc-near      "where is my parcel": delivery, conf 0.6, ask. Gold ask, no escalation.
  2 pc-draft     praise, conf 0.9, answer; its draft holds the proposed "glimmerflux" phrase.
  3 pc-topic     no keywords: other, conf 0.2, ask (to larger at any threshold above 0.2).
                 Gold escalate. Only the proposed topic pc-zindle matches it.
  4-6 repeat trio, billing conf 0.75, ask; same made-up customer and brand one day apart,
                 contact counts 1, 2, 3. Gold: 4 no escalation, 5 and 6 escalate.
"""

import contextlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triage import propose_change as pc
from triage.brand_rules import load_effective
from triage.router import is_known_reason_code
from triage.run_synthetic import load_cases
from triage.tiers import FakeAgentTier, FakeLargerTier, Labels

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures"
BASE_RULES = FIX / "propose_change_rules.json"
THRESHOLDS = FIX / "propose_change_thresholds.json"
BRAND = FIX / "propose_change_brand_guidelines.md"
CASES = FIX / "propose_change_cases.json"
REPLY = FIX / "propose_change_proposed_reply_rules.json"
TOPIC = FIX / "propose_change_proposed_topic.json"
REPEAT = FIX / "propose_change_proposed_repeat.json"

BRANCH_RE = re.compile(r"^factory/policy-change-\d{8}T\d{6}Z$")
SECRET_TEXT = ("glimmerflux", "zindlewump", "quuxfrob", "zorptastic", "parcel",
               "Happy to help", "invoice", "made_up_customer_a", "made_up_brand")


class FakeChangeClient(pc.ChangeClient):
    """Records every call; never touches git. add_worktree returns a fresh temp dir (not the
    owner's repo) holding a copy of the base files, as a new worktree at the base would."""

    def __init__(self, repo, fail_on=()):
        self.repo = Path(repo)
        self.fail_on = set(fail_on)
        self.calls = []
        self.worktree = None
        self.pr_body = None

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail_on:
            raise pc.ChangeError("%s failed (fake)" % name)

    def capture_base(self):
        self._record("capture_base")
        return "a" * 40

    def add_worktree(self, branch, base_sha):
        self._record("add_worktree", branch, base_sha)
        self.worktree = Path(tempfile.mkdtemp(prefix="fake-wt-"))
        for rel in pc.ALLOWED_PATHS:
            src = self.repo / rel
            if src.exists():
                (self.worktree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, self.worktree / rel)
        return self.worktree

    def commit(self, paths, message):
        self._record("commit", tuple(paths))

    def verify(self, branch, base_sha, paths):
        self._record("verify", branch, base_sha, tuple(paths))

    def push(self, branch):
        self._record("push", branch, "refs/heads/%s:refs/heads/%s" % (branch, branch))

    def create_draft_pr(self, branch, base, title, body):
        self._record("create_draft_pr", branch, base)
        self.pr_body = body
        return "https://example.invalid/pr/1"

    def remove_worktree(self):
        self._record("remove_worktree")
        if self.worktree is not None:
            shutil.rmtree(self.worktree, ignore_errors=True)

    def names(self):
        return [c[0] for c in self.calls]


class StubLarger(FakeLargerTier):
    """A larger tier whose labels differ from the cheap tier's: it always says escalate."""

    def label(self, thread):
        return Labels("other", "normal", "escalate", 1.0)


def stub_tiers():
    from triage.cheap_tier import CheapTier
    return (CheapTier(), StubLarger(), FakeAgentTier())


class Base(unittest.TestCase):
    brand = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        for src, rel in ((BASE_RULES, "policies/rules.json"),
                         (THRESHOLDS, "evals/thresholds.json"),
                         (BRAND, "brands/amazonhelp/guidelines.md")):
            if rel.startswith("brands") and not self.brand:
                continue
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, self.repo / rel)
        self.cases = self.tmp / "cases.json"
        shutil.copyfile(CASES, self.cases)
        self.client = FakeChangeClient(self.repo)

    def snapshot(self):
        return {str(p.relative_to(self.repo)): p.read_bytes()
                for p in sorted(self.repo.rglob("*")) if p.is_file()}

    def run_main(self, *extra, confirm=lambda prompt: True, tiers=None, client=True):
        argv = ["--repo", str(self.repo), "--cases", str(self.cases)] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pc.main(argv, client=self.client if client else None, confirm=confirm,
                           tiers=tiers)
        return code, out.getvalue(), err.getvalue()

    def write_rules(self, data, name="proposed.json"):
        path = self.tmp / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def base_data(self):
        return json.loads(BASE_RULES.read_text(encoding="utf-8"))

    def assertRow(self, out, name, before, after):
        self.assertIn("| %s | %s | %s |" % (name, before, after), out)


class TestValidation(Base):
    def assertRefused(self, *argv, says):
        before = self.snapshot()
        code, out, err = self.run_main(*argv)
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn(says, err)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_rules_the_loader_refuses(self):
        def empty_reply(d):
            d["public_reply_rules"] = []

        def empty_topics(d):
            d["escalate_always"] = []

        def topic_without_phrases(d):
            del d["escalate_always"][0]["phrases"]

        def repeat_without_window(d):
            del d["repeat_contact"]["window_days"]

        for edit, says in ((empty_reply, "'public_reply_rules' is empty"),
                           (empty_topics, "'escalate_always' is empty"),
                           (topic_without_phrases, "needs a non-empty 'phrases' list"),
                           (repeat_without_window, "'window_days' is missing")):
            with self.subTest(edit.__name__):
                self.client.calls.clear()
                data = self.base_data()
                edit(data)
                self.assertRefused("--rules", str(self.write_rules(data)), says=says)

    def test_removed_repeat_contact_refused_when_cases_have_customers(self):
        data = self.base_data()
        del data["repeat_contact"]
        self.assertRefused("--rules", str(self.write_rules(data)), says="repeat_contact")

    def test_bad_thresholds(self):
        for value in ("-0.1", "1.5", "NaN", "true", "abc"):
            with self.subTest(value):
                self.client.calls.clear()
                self.assertRefused("--threshold", value,
                                   says="threshold must be a number between 0 and 1, got")

    def test_no_change(self):
        self.assertRefused("--rules", str(BASE_RULES), says="no change")

    def test_needs_rules_or_threshold(self):
        self.assertRefused(says="--rules or --threshold")


class TestPreviewArithmetic(Base):
    # Stub larger tier always says escalate. At 0.5 only case 3 (conf 0.2) reaches it, so
    # 3 becomes a true escalation; 6 escalates by repeat contact; 5 is missed.
    #   before: TP 3,6  FN 5  TN 1,2,4 -> TPR 2/3 TNR 3/3 missed 1; north star 1,2,4 = 3/6
    # At 0.7 case 1 (conf 0.6) reaches it too and becomes a false escalation:
    #   after:  TP 3,6  FN 5  TN 2,4 FP 1 -> TPR 2/3 TNR 2/3 missed 1; north star 2,4 = 2/6
    def test_threshold_moves_one_case_with_stub_tiers(self):
        code, out, err = self.run_main("--threshold", "0.7", "--dry-run", tiers=stub_tiers())
        self.assertEqual(code, 0, err)
        self.assertRow(out, "north_star", "3/6", "2/6")
        self.assertRow(out, "missed_escalations", "1", "1")
        self.assertRow(out, "escalation_tpr", "2/3", "2/3")
        self.assertRow(out, "escalation_tnr", "3/3", "2/3")

    def test_preview_seam(self):
        cases = load_cases(CASES)
        eff = load_effective(BASE_RULES)
        before = pc.preview(cases, eff, 0.5, stub_tiers())
        after = pc.preview(cases, eff, 0.7, stub_tiers())
        self.assertEqual((before["north_star"]["num"], before["north_star"]["n"]), (3, 6))
        self.assertEqual((after["north_star"]["num"], after["north_star"]["n"]), (2, 6))
        self.assertEqual((after["escalation_tnr"]["num"], after["escalation_tnr"]["n"]), (2, 3))


class TestPreviewFakeTiers(Base):
    # Default fakes: the fake larger tier copies the cheap labels, so labels never move.
    # Visits at 0.5: cheap 6/6; larger only case 3 = 1/6; agent cases 1-5 = 5/6
    #   (6 is forced by repeat contact, which skips the agent).
    #   cost = 1000 * (0.002 * 1 + 0.01 * 5) / 6 = 8.6667
    # At 0.7 case 1 also visits larger: larger 2/6; cost = 1000 * (0.004 + 0.05) / 6 = 9.0000
    def test_threshold_changes_only_path_and_cost(self):
        code, out, err = self.run_main("--threshold", "0.7", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("path and cost only", out)
        self.assertRow(out, "north_star", "3/6", "3/6")
        self.assertRow(out, "missed_escalations", "2", "2")
        self.assertRow(out, "escalation_tpr", "1/3", "1/3")
        self.assertRow(out, "escalation_tnr", "3/3", "3/3")
        self.assertRow(out, "tier_share.cheap", "6/6", "6/6")
        self.assertRow(out, "tier_share.larger", "1/6", "2/6")
        self.assertRow(out, "tier_share.agent", "5/6", "5/6")
        self.assertRow(out, "cost_per_1000_usd (placeholder prices)", "8.6667", "9.0000")
        self.assertIn("current threshold: code default 0.5 (thresholds file value is null)", out)
        self.assertIn("recorded experiment, not operational", out)


class TestPreviewRules(Base):
    def test_reply_rule_change(self):
        # Case 2's draft holds "glimmerflux": 0 -> 1 violation, and a mismatch with its
        # expected [] list. Escalation untouched: TPR 1/3 (only 6), TNR 3/3.
        code, out, err = self.run_main("--rules", str(REPLY), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertRow(out, "policy_violations", "0", "1")
        self.assertRow(out, "policy_mismatches", "0", "1")
        self.assertRow(out, "escalation_tpr", "1/3", "1/3")
        self.assertRow(out, "escalation_tnr", "3/3", "3/3")

    def test_topic_change(self):
        # pc-zindle matches case 3: forced escalate, a true escalation. TPR 1/3 -> 2/3.
        code, out, err = self.run_main("--rules", str(TOPIC), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertRow(out, "escalation_tpr", "1/3", "2/3")
        self.assertRow(out, "escalation_tnr", "3/3", "3/3")
        self.assertRow(out, "missed_escalations", "2", "1")
        self.assertRow(out, "forced_escalations.escalate_always", "0", "1")

    def test_repeat_contact_change(self):
        # min_conversations 3 -> 2: case 5 (count 2) now escalates too. TPR 1/3 -> 2/3.
        code, out, err = self.run_main("--rules", str(REPEAT), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertRow(out, "escalation_tpr", "1/3", "2/3")
        self.assertRow(out, "escalation_tnr", "3/3", "3/3")
        self.assertRow(out, "forced_escalations.repeat_contact", "1", "2")

    def test_counted_reason_codes_are_registered(self):
        cases = load_cases(CASES)
        for path in (TOPIC, REPEAT):
            result = pc.preview(cases, load_effective(path), 0.5)
            self.assertTrue(result["forced_escalations"])
            for code in result["forced_escalations"]:
                self.assertTrue(is_known_reason_code(code), code)


class TestThresholdFile(Base):
    def test_current_threshold(self):
        self.assertEqual(pc.current_threshold(self.repo), (0.5, None))
        path = self.repo / "evals" / "thresholds.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["confidence_threshold"]["value"] = 0.65
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(pc.current_threshold(self.repo), (0.65, 0.65))

    def test_write_changes_only_the_value(self):
        path = self.tmp / "t.json"
        shutil.copyfile(THRESHOLDS, path)
        pc.write_threshold(path, 0.7)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {
            "version": "pc-fixture",
            "confidence_threshold": {"value": 0.7,
                                     "note": "Made-up note: placeholder in code."},
            "reporting": {"ci_level": 0.95, "ci_method": "wilson"}})
        self.assertEqual(list(data), ["version", "confidence_threshold", "reporting"])


class TestCallOrder(Base):
    def test_calls_in_order(self):
        code, out, err = self.run_main("--rules", str(REPEAT), "--threshold", "0.7")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.client.names(), ["capture_base", "add_worktree", "commit",
                                               "verify", "push", "create_draft_pr",
                                               "remove_worktree"])
        _, branch, base = self.client.calls[1]
        self.assertRegex(branch, BRANCH_RE)
        self.assertEqual(base, "a" * 40)
        self.assertEqual(self.client.calls[2][1],
                         ("policies/rules.json", "evals/thresholds.json"))
        self.assertEqual(self.client.calls[4][1], branch)
        self.assertEqual(self.client.calls[5][1:], (branch, "main"))
        for call in self.client.calls:
            if call[0] == "push":
                self.assertNotIn("main", call[1:])

    def test_threshold_only_commits_only_thresholds(self):
        code, _, err = self.run_main("--threshold", "0.7")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.client.calls[2][1], ("evals/thresholds.json",))


class TestOwnerConfirmation(Base):
    def test_refusing_confirm(self):
        before = self.snapshot()
        code, out, err = self.run_main("--threshold", "0.7", confirm=lambda prompt: False)
        self.assertEqual(code, 2)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_default_confirm_needs_a_terminal(self):
        before = self.snapshot()
        with mock.patch("sys.stdin", io.StringIO("yes\n")):
            code, out, err = self.run_main("--threshold", "0.7", confirm=None)
        self.assertEqual(code, 2)
        self.assertIn("proposals need the owner at a terminal; use --dry-run", err)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_default_confirm_without_client_makes_no_git_call(self):
        with mock.patch("sys.stdin", io.StringIO("yes\n")), \
                mock.patch("triage.propose_change.subprocess.run") as run:
            code, _, _ = self.run_main("--threshold", "0.7", confirm=None, client=False)
        self.assertEqual(code, 2)
        run.assert_not_called()


class TestPushGuard(unittest.TestCase):
    BRANCH = "factory/policy-change-20261119T101500Z"
    BASE = "b" * 40

    def ok(self, **over):
        values = dict(head_ref="refs/heads/" + self.BRANCH, parents=[self.BASE],
                      changed_paths=["policies/rules.json"], base_sha=self.BASE,
                      branch=self.BRANCH, written_paths=["policies/rules.json"])
        values.update(over)
        return pc.check_push_ready(**values)

    def test_accepts_matching(self):
        self.assertIsNone(self.ok())

    def test_refusals(self):
        for name, over in (
                ("other ref", {"head_ref": "refs/heads/main"}),
                ("origin advanced", {"parents": ["c" * 40]}),
                ("two parents", {"parents": [self.BASE, "c" * 40]}),
                ("extra changed path", {"changed_paths": ["policies/rules.json", "README.md"]}),
                ("written path outside allowlist", {
                    "changed_paths": ["brands/amazonhelp/guidelines.md"],
                    "written_paths": ["brands/amazonhelp/guidelines.md"]}),
                ("branch not a policy-change branch", {"branch": "main",
                                                       "head_ref": "refs/heads/main"})):
            with self.subTest(name):
                with self.assertRaises(pc.ChangeError):
                    self.ok(**over)


class TestVerifyFailure(Base):
    def test_verify_raising_stops_before_push(self):
        self.client.fail_on = {"verify"}
        code, _, err = self.run_main("--threshold", "0.7")
        self.assertEqual(code, 2)
        self.assertNotIn("push", self.client.names())
        self.assertNotIn("create_draft_pr", self.client.names())
        self.assertEqual(self.client.names()[-1], "remove_worktree")


def _git_env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                        "GIT_COMMON_DIR")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(cwd, *args):
    """A local git command in a throwaway repo with no remote."""
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, env=_git_env()).stdout.strip()


def _init_repo(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


class TestRealVerify(Base):
    BRANCH = "factory/policy-change-20261119T101500Z"

    def setUp(self):
        super().setUp()
        self.base = _init_repo(self.repo)
        self.assertEqual(_git(self.repo, "remote"), "")
        _git(self.repo, "checkout", "-q", "-b", self.BRANCH)

    def commit_change(self, extra=False):
        (self.repo / "policies" / "rules.json").write_text(REPEAT.read_text(encoding="utf-8"),
                                                           encoding="utf-8")
        if extra:
            (self.repo / "README.md").write_text("made up\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "change")

    def test_accepts_matching_commit(self):
        self.commit_change()
        client = pc.GitGhClient(self.repo, worktree=self.repo)
        client.verify(self.BRANCH, self.base, ["policies/rules.json"])

    def test_refuses_extra_path(self):
        self.commit_change(extra=True)
        client = pc.GitGhClient(self.repo, worktree=self.repo)
        with self.assertRaises(pc.ChangeError):
            client.verify(self.BRANCH, self.base, ["policies/rules.json"])


class TestPrBody(Base):
    def test_rules_and_threshold_body(self):
        code, out, err = self.run_main("--rules", str(REPEAT), "--threshold", "0.7")
        self.assertEqual(code, 0, err)
        body = self.client.pr_body
        self.assertIn("threshold: null (code default 0.5) -> 0.7", body)
        self.assertIn("repeat_contact.min_conversations: 3 -> 2", body)
        self.assertNotIn("repeat_contact.window_days", body)
        self.assertIn("version: pc-fixture-1 -> pc-fixture-2", body)
        self.assertNotIn("pc-promise", body)
        self.assertNotIn("pc-sentinel", body)
        self.assertIn("path and cost only", body)
        self.assertIn("recorded experiment, not operational", body)
        self.assertIn("Opened as a draft. Only the owner merges it.", body)
        self.assertIn("warning: proposed rules differ from brands/amazonhelp/guidelines.md", body)
        # The preview numbers in the body are the ones printed.
        for row in ("| escalation_tpr | 1/3 | 2/3 |", "| tier_share.larger | 1/6 | 2/6 |"):
            self.assertIn(row, out)
            self.assertIn(row, body)
        for text in SECRET_TEXT:
            self.assertNotIn(text, body)

    def test_changed_ids_per_section(self):
        code, _, err = self.run_main("--rules", str(TOPIC))
        self.assertEqual(code, 0, err)
        body = self.client.pr_body
        self.assertIn("escalate_always added: pc-zindle", body)
        self.assertNotIn("public_reply_rules added", body)
        self.assertNotIn("threshold:", body)
        self.assertNotIn("recorded experiment", body)
        for text in SECRET_TEXT:
            self.assertNotIn(text, body)

    def test_reply_rule_ids(self):
        code, _, err = self.run_main("--rules", str(REPLY))
        self.assertEqual(code, 0, err)
        self.assertIn("public_reply_rules added: pc-boast", self.client.pr_body)
        self.assertNotIn("escalate_always added", self.client.pr_body)


class TestBrandConformance(Base):
    def brand_bytes(self):
        return {str(p): p.read_bytes() for p in (self.repo / "brands").rglob("*") if p.is_file()}

    def test_rules_proposal_warns(self):
        before = self.brand_bytes()
        code, out, err = self.run_main("--rules", str(REPLY), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: proposed rules differ from brands/amazonhelp/guidelines.md "
                      "(keys=public_reply_rules,version); brands/ is not changed by this "
                      "proposal", out)
        self.assertEqual(self.brand_bytes(), before)

    def test_rules_proposal_warns_on_change_path_too(self):
        before = self.brand_bytes()
        code, _, err = self.run_main("--rules", str(REPLY))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.brand_bytes(), before)

    def test_threshold_only_no_warning(self):
        code, out, err = self.run_main("--threshold", "0.7", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", out + err)


class TestNoBrandFile(Base):
    brand = False

    def test_skipped(self):
        code, out, err = self.run_main("--rules", str(REPLY), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("brand conformance: skipped", out)


class TestDryRun(Base):
    def test_no_calls_no_confirm_no_write(self):
        before = self.snapshot()
        asked = []
        code, out, err = self.run_main("--rules", str(REPEAT), "--threshold", "0.7",
                                       "--dry-run", confirm=lambda p: asked.append(p) or True)
        self.assertEqual(code, 0, err)
        self.assertEqual(asked, [])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.snapshot(), before)


class TestFailure(Base):
    def test_failed_push(self):
        _init_repo(self.repo)
        head_before = _git(self.repo, "symbolic-ref", "HEAD")
        files_before = self.snapshot()
        self.client.fail_on = {"push"}
        code, out, err = self.run_main("--rules", str(REPEAT), "--threshold", "0.7")
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertEqual(self.client.names()[-1], "remove_worktree")
        self.assertNotIn("create_draft_pr", self.client.names())
        self.assertEqual(_git(self.repo, "symbolic-ref", "HEAD"), head_before)
        self.assertEqual(self.snapshot(), files_before)


class TestRealClientArgs(unittest.TestCase):
    BRANCH = "factory/policy-change-20261119T101500Z"

    def test_argv(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("triage.propose_change.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout="f" * 40 + "\n",
                                                           stderr="")
            client = pc.GitGhClient(Path(tmp) / "repo")
            # branch lookups return 1 / no output: the branch is free
            run.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
                argv, 0 if "rev-parse" in argv and "--quiet" not in argv else 1,
                stdout="" if "ls-remote" in argv else "f" * 40 + "\n", stderr="")
            client.capture_base()
            wt = client.add_worktree(self.BRANCH, "f" * 40)
            client.commit(["policies/rules.json"], "msg")
            with self.assertRaises(pc.ChangeError):
                client.verify(self.BRANCH, "f" * 40, ["policies/rules.json"])
            client.push(self.BRANCH)
            client.create_draft_pr(self.BRANCH, "main", "title", "body")
            client.remove_worktree()
            shutil.rmtree(Path(wt).parent, ignore_errors=True)
        self.assertTrue(run.call_args_list)
        pushes = []
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIsInstance(argv, list)
            self.assertFalse(call.kwargs.get("shell", False))
            for bad in ("merge", "--auto", "--force", "-f"):
                self.assertNotIn(bad, argv)
            if "push" in argv:
                pushes.append(argv)
        self.assertEqual(len(pushes), 1)
        self.assertIn("refs/heads/%s:refs/heads/%s" % (self.BRANCH, self.BRANCH), pushes[0])
        self.assertNotIn("main", pushes[0])
        self.assertFalse(any(":refs/heads/main" in a for a in pushes[0]))
        gh = [c.args[0] for c in run.call_args_list if c.args[0][0] == "gh"]
        self.assertEqual(gh[0][:3], ["gh", "pr", "create"])
        self.assertIn("--draft", gh[0])

    def test_interface_has_no_merge_or_approve(self):
        names = [n.lower() for n in dir(pc.ChangeClient)] + \
            [n.lower() for n in dir(pc.GitGhClient)]
        self.assertFalse([n for n in names if "merge" in n or "approve" in n])

    def test_push_refuses_main(self):
        with mock.patch("triage.propose_change.subprocess.run") as run:
            with self.assertRaises(pc.ChangeError):
                pc.GitGhClient(Path("/nonexistent")).push("main")
        run.assert_not_called()


class TestRepoRequired(unittest.TestCase):
    def test_repo_has_no_default(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            pc.main(["--cases", str(CASES), "--threshold", "0.7", "--dry-run"])


@unittest.skipUnless(os.environ.get("TRIAGE_CONFORMANCE") == "1",
                     "opt-in: set TRIAGE_CONFORMANCE=1")
class TestLiveConformance(unittest.TestCase):
    def test_live_rules_load(self):
        # Schema check only, never an oracle.
        load_effective(REPO_ROOT / "policies" / "rules.json")


if __name__ == "__main__":
    unittest.main()
