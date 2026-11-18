"""Tests for triage.brand_rules. Every brand file here is a made-up fixture under
tests/fixtures/brands/ or tests/fixtures/brand_rules/, or written into a temp dir.
No test reads the owner's live files: every call passes its brands dir and rules path.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from triage.brand_rules import EffectiveRules, brand_path, load_brand_file, load_effective
from triage.context_signals import RepeatContactRule
from triage.policy_check import RulesError, check

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRANDS = HERE / "fixtures" / "brands"
PAIR = HERE / "fixtures" / "brand_rules"

VALID = {
    "public_reply_rules": [{"id": "m-banned", "type": "banned_phrase", "phrases": ["flimflam"]}],
    "escalate_always": [{"id": "m-topic", "phrases": ["made-up topic"]}],
}


def brand_text(block, prose="Made-up prose.\n"):
    return "# Made-up guidelines\n\n%s\n```rules\n%s\n```\n" % (prose, block)


class TestLoadBrandFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, text):
        path = self.dir / "guidelines.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_fixture(self):
        eff = load_brand_file(BRANDS / "beta" / "guidelines.md")
        self.assertIsInstance(eff, EffectiveRules)
        self.assertEqual([r.id for r in eff.rules], ["b-banned", "b-pii"])
        self.assertEqual([t.id for t in eff.topics], ["b-topic"])
        self.assertEqual(eff.repeat_contact, RepeatContactRule(7, 3, "escalate", 72))
        self.assertEqual((eff.version, eff.status), ("fixture-b", "made-up"))

    def test_errors(self):
        ok = json.dumps(VALID)
        topic_no_phrases = dict(VALID, escalate_always=[{"id": "m-topic"}])
        bad_repeat = dict(VALID, repeat_contact={"window_days": 7})
        cases = {
            "no block": ("# Made-up\n\nno rules here\n", "no ```rules block found"),
            "two blocks": (brand_text(ok) + "\n```rules\n" + ok + "\n```\n", "more than one"),
            "rules json fence": ("# Made-up\n\n```rules json\n" + ok + "\n```\n", "malformed"),
            "unclosed": ("# Made-up\n\n```rules\n" + ok + "\n", "unclosed"),
            "empty block": (brand_text("   \n"), "empty"),
            "invalid json": (brand_text('{\n"public_reply_rules": [\n'), "line"),
            "not an object": (brand_text("[1, 2]"), "object"),
            "empty rules": (brand_text(json.dumps(dict(VALID, public_reply_rules=[]))),
                            "'public_reply_rules' is empty"),
            "empty topics": (brand_text(json.dumps(dict(VALID, escalate_always=[]))),
                             "'escalate_always' is empty"),
            "topic without phrases": (brand_text(json.dumps(topic_no_phrases)),
                                      "needs a non-empty 'phrases' list"),
            "malformed repeat_contact": (brand_text(json.dumps(bad_repeat)),
                                         "repeat_contact rule in"),
        }
        for name, (text, problem) in cases.items():
            with self.subTest(name):
                path = self.write(text)
                with self.assertRaises(RulesError) as ctx:
                    load_brand_file(path)
                self.assertIn(str(path), str(ctx.exception))
                self.assertIn(problem, str(ctx.exception))
        with self.subTest("unreadable"):
            missing = self.dir / "nope.md"
            with self.assertRaises(RulesError) as ctx:
                load_brand_file(missing)
            self.assertIn(str(missing), str(ctx.exception))

    def test_invalid_json_line_is_within_block(self):
        path = self.write("# t\n\nprose\n\nmore prose\n\n```rules\n{\n  \"a\": 1,\n  oops\n}\n```\n")
        with self.assertRaises(RulesError) as ctx:
            load_brand_file(path)
        self.assertIn("line 3", str(ctx.exception))

    def test_prose_ignored(self):
        # alpha's prose holds a ```json block with an empty rule list, a phrase from beta
        # and inline rules-like text; only its rules block counts.
        with_prose = load_brand_file(BRANDS / "alpha" / "guidelines.md")
        self.assertEqual([r.id for r in with_prose.rules], ["a-banned", "a-pii"])
        self.assertEqual(check("You can quazzlewump.", [], with_prose.rules), [])
        bare = self.write(brand_text(json.dumps(VALID), prose=""))
        prosy = self.dir / "prosy.md"
        prosy.write_text(brand_text(json.dumps(VALID), prose=(
            "Never say flimflam.\n\n```json\n{\"escalate_always\": []}\n```\n\n"
            "```\nplain fence\n```\n")), encoding="utf-8")
        self.assertEqual(load_brand_file(prosy), load_brand_file(bare))

    def test_json_and_markdown_equal(self):
        from_json = load_effective(PAIR / "rules.json")
        from_md = load_brand_file(PAIR / "brands" / "same" / "guidelines.md")
        self.assertEqual(from_json, from_md)
        self.assertIsNotNone(from_md.repeat_contact)
        self.assertEqual(from_md.version, "fixture-s")
        alpha = load_brand_file(BRANDS / "alpha" / "guidelines.md")
        self.assertIsNone(alpha.repeat_contact)


class TestTwoBrands(unittest.TestCase):
    def test_two_brands_differ(self):
        draft = "Sure, we can zibberflop that for you today."
        alpha = load_brand_file(brand_path("alpha", BRANDS))
        beta = load_brand_file(brand_path("beta", BRANDS))
        self.assertEqual([v.rule_id for v in check(draft, [], alpha.rules)], ["a-banned"])
        self.assertEqual(check(draft, [], beta.rules), [])


class TestBrandPath(unittest.TestCase):
    def test_names(self):
        self.assertEqual(brand_path("alpha", BRANDS),
                         (BRANDS / "alpha" / "guidelines.md").resolve())
        missing_dir = Path(tempfile.gettempdir()) / "made-up-brands-dir-that-does-not-exist"
        for name in ("../x", "Alpha", "a/b", "", "a" * 65, ".hidden", "-x"):
            with self.subTest(name=name):
                with self.assertRaises(RulesError):
                    brand_path(name, missing_dir)  # refused before the dir is touched
        with self.assertRaises(RulesError) as ctx:
            brand_path("gamma", BRANDS)
        self.assertIn("brand 'gamma' not found under", str(ctx.exception))

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            brands = tmp / "brands"
            outside = tmp / "outside"
            (outside / "evil").mkdir(parents=True)
            (outside / "evil" / "guidelines.md").write_text("x", encoding="utf-8")
            brands.mkdir()
            (brands / "dirlink").symlink_to(outside / "evil", target_is_directory=True)
            (brands / "filelink").mkdir()
            (brands / "filelink" / "guidelines.md").symlink_to(outside / "evil" / "guidelines.md")
            (brands / "real").mkdir()
            (brands / "real" / "guidelines.md").write_text("x", encoding="utf-8")
            (brands / "inlink").symlink_to(brands / "real", target_is_directory=True)
            for name in ("dirlink", "filelink"):
                with self.subTest(name=name):
                    with self.assertRaises(RulesError) as ctx:
                        brand_path(name, brands)
                    self.assertIn("outside", str(ctx.exception))
            self.assertEqual(brand_path("inlink", brands),
                             (brands / "real" / "guidelines.md").resolve())


def run_conformance(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "triage.brand_rules", "--conformance", *args],
                          capture_output=True, text=True, env=env, cwd=str(HERE))


class TestConformanceCli(unittest.TestCase):
    def test_equal_pair(self):
        r = run_conformance("--brand", "same", "--rules", str(PAIR / "rules.json"),
                            "--brands-dir", str(PAIR / "brands"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "conformance: ok brand=same")

    def test_version_differs(self):
        r = run_conformance("--brand", "same", "--rules", str(PAIR / "rules_other_version.json"),
                            "--brands-dir", str(PAIR / "brands"))
        self.assertEqual(r.returncode, 1)
        out = r.stdout + r.stderr
        self.assertIn("version", out)
        self.assertNotIn("fixture-s", out)
        self.assertNotIn("snorkelbar", out)

    def test_broken_brand_file(self):
        r = run_conformance("--brand", "broken", "--rules", str(PAIR / "rules.json"),
                            "--brands-dir", str(PAIR / "brands"))
        self.assertEqual(r.returncode, 2)


class TestSourceHygiene(unittest.TestCase):
    def test_test_names_no_live_file(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("policies/" + "rules.json", source)
        self.assertNotIn("evals/" + "gold", source)
        self.assertNotIn("BRANDS" + "_DIR", source)
        self.assertNotIn("REPO_ROOT / " + '"brands"', source)

    def test_module_stdlib_only(self):
        src = (ROOT / "src" / "triage" / "brand_rules.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)
        stdlib = set(sys.stdlib_module_names)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                self.assertTrue(top == "triage" or top in stdlib, mod)


if __name__ == "__main__":
    unittest.main()
