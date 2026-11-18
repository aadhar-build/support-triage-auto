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

import triage.incidents as incidents

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "drift_config.json"


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
