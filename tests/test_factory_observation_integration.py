"""Exercise the installed command-line interfaces in disposable repositories."""

import fcntl
import json
import os
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "001-example"


class FactoryCLI:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/fixture/software-factory.git")
        self.factory = self.root / "factory"
        self.factory.mkdir()
        for folder in ("bin", "lib"):
            shutil.copytree(ROOT / "factory" / folder, self.factory / folder,
                            ignore=shutil.ignore_patterns("__pycache__"))
        for filename in ("config.json", "baseline-v1.json"):
            shutil.copy2(ROOT / "factory" / filename, self.factory / filename)
        self.job_dir = self.factory / "jobs" / JOB_ID
        self.state = self.job_dir / "job.json"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def cli(self, command, *args, input=None, success=True, env=None):
        result = subprocess.run([self.factory / "bin" / command, *args], cwd=self.root,
                                input=input, capture_output=True, text=True, timeout=10, env=env)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def write_job(self, *, stage="review", round_number=1, history=None, **extra):
        self.job_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "id": JOB_ID, "issue": 1, "title": "Example", "stage": stage, "round": round_number,
            "factory_version": "v1.2-two-phase", "risk": "low", "surfaces": ["security"],
            "reviewers": {"code": "haiku"}, "pr": 2, "branch": f"factory/{JOB_ID}",
            "worktree": f".worktrees/{JOB_ID}", "revision": 4,
            "history": history or [
                {"at": "2026-11-19T07:00:00Z", "stage": "intake", "note": "created"},
                {"at": "2026-11-19T07:05:00Z", "stage": "build", "note": "round 1"},
                {"at": "2026-11-19T07:10:00Z", "stage": "gate", "note": ""},
                {"at": "2026-11-19T07:15:00Z", "stage": stage, "note": "gate passed"},
            ], **extra,
        }
        self.state.write_text(json.dumps(job, indent=4) + "\n", encoding="utf-8")

    def effort(self, **extra):
        return {
            "schema_version": 1, "event_id": "6d285466-8cbe-4fa9-9c00-3e264990f307",
            "job_id": JOB_ID, "kind": "owner_effort", "origin": "real",
            "occurred_at": "2026-11-19T08:00:00Z",
            "source": {"kind": "owner", "record_id": "effort-01"},
            "data": {"activity": "review", "minutes": 0, "rework": False}, **extra,
        }


class FactoryRecoveryTransition(FactoryCLI, unittest.TestCase):
    def make_gate(self, round_number=2):
        origin = self.root / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin))
        self.git("remote", "set-url", "origin", str(origin))
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "code.txt").write_text("original\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "base")
        self.git("push", "-q", "origin", "main")
        self.worktree = self.root / ".worktrees" / JOB_ID
        self.git("worktree", "add", "-qb", f"factory/{JOB_ID}", str(self.worktree))
        (self.worktree / "code.txt").write_text("candidate\n")
        self.git("-C", str(self.worktree), "commit", "-qam", "candidate")
        self.candidate = self.git("-C", str(self.worktree), "rev-parse", "HEAD").stdout.strip()
        stages = ["intake", *["build", "gate"] * round_number]
        history = [{"at": f"2020-01-01T00:{i:02d}:00Z", "stage": stage, "note": ""}
                   for i, stage in enumerate(stages)]
        self.write_job(stage="gate", round_number=round_number, history=history)
        (self.job_dir / "spec.md").write_text("## Files\n- code.txt\n")
        (self.factory / "checks.sh").write_text("printf '%s\\n' private-check-output\nexit 1\n")

    def observe_gate_event(self, kind, data, *, event_id="a3f83e7e-92cb-4e68-a985-1bb5ff447e4f", evidence=(),
                           **bindings):
        event = {
            "schema_version": 1, "event_id": event_id, "job_id": JOB_ID, "kind": kind,
            "occurred_at": "2026-11-19T08:00:00Z", "origin": "real",
            "source": {"kind": "controller", "record_id": "gate-fixture"},
            "candidate_sha": self.candidate, "round": 2, "data": data, "evidence": list(evidence), **bindings,
        }
        self.cli("job", "observe", JOB_ID, "-", input=json.dumps(event))
        return event_id

    def run_failing_gate(self):
        # A stub catches any GitHub CLI call even when the command handles its failure.
        gh = self.root / "stub" / "gh"
        gh.parent.mkdir()
        gh.write_text("#!/bin/sh\ntouch gh-called\nexit 1\n")
        gh.chmod(0o755)
        env = dict(os.environ, PATH=f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
        before = json.loads(self.state.read_bytes())
        old_files = self.job_files()
        command = self.cli("gate", JOB_ID, env=env)
        self.assertEqual(command.stderr, "")
        self.assertFalse((self.root / "gh-called").exists())
        self.assertFalse((self.factory / "lib" / "__pycache__").exists())
        current = json.loads(self.state.read_bytes())
        self.assertEqual((current["stage"], current["round"]), ("recovery", before["round"]))
        self.assertEqual(current["history"][:-1], before["history"])
        self.assertEqual(current["history"][-1]["note"], "gate failed; 2 rounds used")
        self.assertEqual({key: value for key, value in current.items()
                          if key not in {"stage", "history", "revision"}},
                         {key: value for key, value in before.items()
                          if key not in {"stage", "history", "revision"}})
        receipt = f"round-{before['round']}/checks.md"
        new_files = self.job_files()
        self.assertIn(b"private-check-output", new_files.pop(receipt))
        self.assertEqual(new_files, old_files)
        lines = command.stdout.splitlines()
        self.assertEqual(len(lines), 2, command.stdout)
        status, question = lines
        self.assertEqual(status, f"{JOB_ID} -> recovery: gate FAIL")
        self.assertTrue(question.endswith("?"))
        self.assertEqual(question.count("?"), 1)
        self.assertIn(JOB_ID, question)
        self.assertNotIn("private-check-output", question)
        return question

    def job_files(self):
        return {str(file.relative_to(self.job_dir)): file.read_bytes()
                for file in self.job_dir.rglob("*") if file.is_file() and file != self.state}

    def test_gate_exhaustion_above_cap_asks_about_follow_up_debt(self):
        self.make_gate(round_number=3)
        legacy = self.job_dir / "round-2" / "checks.md"
        legacy.parent.mkdir()
        legacy.write_text("GATE: FAIL\nprivate-legacy-failure\n")
        question = self.run_failing_gate()
        self.assertIn("follow-up debt", question)
        self.assertNotIn("private-legacy-failure", question)
        self.assertFalse((self.job_dir / "round-1").exists())
        self.assertFalse((self.job_dir / "round-4").exists())

    def test_gate_reports_linked_proposal_without_disclosing_evidence(self):
        self.make_gate()
        evidence = [{"path": "round-2/private-artifact.json", "sha256": "c" * 64}]
        check_id = self.observe_gate_event("check_evidence", {"check_id": "regression", "verdict": "fail"},
                                           event_id="bc6a4bb7-841e-4848-8f6d-ca58f59cd923", evidence=evidence)
        proposal_id = self.observe_gate_event(
            "recovery_decision",
            {"failure_id": "private-failure-id", "proposed_recovery": "split_scope",
             "regression_check_event_id": check_id, "cause": "private-claimed-cause"},
            evidence=evidence,
        )
        question = self.run_failing_gate()
        self.assertIn("reported scope split", question)
        for private in ("private-artifact", "private-failure-id", "private-claimed-cause", check_id, proposal_id):
            self.assertNotIn(private, question)
        recovery = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertEqual(recovery["recommendations"][0]["reported_proposal_event_ids"], [proposal_id])
        self.assertEqual(recovery["decisions"][0]["regression_check"]["event_id"], check_id)
        self.assertIsNone(recovery["decisions"][0]["cause"])

    def test_gate_asks_about_debt_when_recovery_is_reported_unsafe(self):
        self.make_gate()
        self.observe_gate_event("recovery_decision", {"failure_id": "unsafe",
                                "outcome": "no_safe_recovery", "proposed_recovery": "permitted_alternative"})
        question = self.run_failing_gate()
        self.assertIn("follow-up debt", question)
        self.assertNotIn("alternative approach", question)

    def test_gate_ignores_partial_evidence_from_a_corrupt_observation_ledger(self):
        self.make_gate()
        self.observe_gate_event("recovery_decision", {"failure_id": "split", "proposed_recovery": "split_scope"})
        invalid = self.job_dir / "observations" / "96de3bfe-a8b5-421b-8ff1-7c40d15fb3b4.json"
        invalid.write_text("private-corrupt-data")
        question = self.run_failing_gate()
        self.assertIn("until its recovery observations can be checked", question)
        self.assertNotIn("scope split", question)
        self.assertNotIn("private-corrupt-data", question)
        self.assertNotIn(invalid.name, question)

    def test_gate_finishes_while_an_observer_holds_the_ledger_lock(self):
        self.make_gate()
        self.observe_gate_event("recovery_decision", {"failure_id": "locked", "proposed_recovery": "split_scope"})
        fd = os.open(self.job_dir / "observations", os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            question = self.run_failing_gate()
            self.assertIn("until its recovery observations can be checked", question)
            self.assertNotIn("scope split", question)
        finally:
            os.close(fd)

    def test_large_ledger_falls_back_only_for_the_transition(self):
        self.make_gate()
        self.observe_gate_event("recovery_decision", {"failure_id": "large", "proposed_recovery": "split_scope"},
                                candidate_sha=None)
        for number in range(70):
            (self.job_dir / "observations" / f"note-{number}").touch()
        self.assertIn("until its recovery observations can be checked", self.run_failing_gate())
        recovery = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertIn("scope split", recovery["owner_question"])
        self.assertFalse(recovery["diagnostics"])

    def test_gate_ignores_proposal_for_another_head_in_the_same_round(self):
        self.make_gate()
        other = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(other, self.candidate)
        self.observe_gate_event("recovery_decision", {"failure_id": "stale", "proposed_recovery": "split_scope"},
                                candidate_sha=other)
        self.assertIn("follow-up debt", self.run_failing_gate())
        recovery = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertIn("follow-up debt", recovery["owner_question"])
        self.assertEqual(recovery["decisions"][0]["proposed_recovery"], "split_scope")
        card = self.cli("job", "card", JOB_ID).stdout
        self.assertNotIn("scope split", card.partition("## Next step")[2])

    def test_gate_asks_to_check_an_unreadable_observation_ledger(self):
        self.make_gate()
        (self.job_dir / "observations").write_text("private-invalid-ledger")
        question = self.run_failing_gate()
        self.assertIn("until its recovery observations can be checked", question)
        self.assertNotIn("private-invalid-ledger", question)

    def test_gate_before_exhaustion_and_final_gate_keep_their_status_outputs(self):
        self.make_gate(round_number=1)
        output = self.cli("gate", JOB_ID).stdout
        self.assertEqual(output, f"{JOB_ID} -> build: gate FAIL\n")
        before = self.state.read_bytes()
        self.assertEqual((json.loads(before)["stage"], json.loads(before)["round"]), ("build", 2))
        self.assertEqual(self.cli("gate", JOB_ID, "--final").stdout, f"{JOB_ID} final gate FAIL\n")
        self.assertEqual(self.state.read_bytes(), before)


class FactoryObservationIntegration(FactoryCLI, unittest.TestCase):
    def test_fresh_install_exposes_the_intact_frozen_baseline_and_empty_cohort(self):
        baseline = (self.factory / "baseline-v1.json").read_bytes()
        self.assertEqual(self.cli("job", "list").stdout.strip(), "no jobs")
        self.assertEqual(json.loads(self.cli("scorecard", "--json").stdout), {})
        self.assertIn("2026-11-18", self.cli("scorecard").stdout)
        self.assertEqual(json.loads(baseline)["jobs"], 4)
        self.assertEqual((self.factory / "baseline-v1.json").read_bytes(), baseline)
        self.assertFalse((self.factory / "jobs").exists())

    def test_historical_long_job_is_reported_and_accepts_observations(self):
        make_job_id = runpy.run_path(str(self.factory / "lib" / "factory.py"))["make_job_id"]
        job_id = make_job_id(17, " ".join(["a" * 40] * 4))
        self.assertEqual(len(job_id), 167)
        self.write_job(stage="merged", owner_minutes=4, branch=f"factory/{job_id}",
                       worktree=f".worktrees/{job_id}")
        job = json.loads(self.state.read_bytes())
        job["id"] = job_id
        self.state.write_text(json.dumps(job))
        folder = self.job_dir.with_name(job_id)
        self.job_dir.rename(folder)
        before = (folder / "job.json").read_bytes()
        legacy = self.cli("scorecard", "--json")
        self.assertEqual(json.loads(legacy.stdout)["v1.2-two-phase"]["merged"], 1)
        self.assertEqual(legacy.stderr, "")
        event = self.effort(job_id=job_id)
        saved = json.loads(self.cli("job", "observe", job_id, "-", input=json.dumps(event)).stdout)
        self.assertEqual(saved["job_id"], job_id)
        report = json.loads(self.cli("scorecard", "--weekly", "2026-11-19", "--timezone", "UTC",
                                     "--as-of", "2099-11-27T10:00:00Z", "--json").stdout)
        self.assertEqual(report["delivered"]["jobs"], [job_id])
        self.assertEqual(report["owner_effort"]["known_minutes"], 0)
        self.assertEqual(report["diagnostics"], {})
        self.assertEqual((folder / "job.json").read_bytes(), before)

    def test_legacy_record_rejects_invalid_values_without_mutating_state(self):
        cases = [("escaped", "treu"), ("owner_changes", "FALSE"), ("owner_minutes", "unknown"),
                 ("owner_minutes", "nan"), ("owner_minutes", "-1"), ("cost_usd", "inf"),
                 ("cost_usd", "1e999"), ("cost_usd", "-0.01")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.write_job(escaped=True, owner_changes=True, owner_minutes=5, cost_usd=2)
                before = self.state.read_bytes()
                result = self.cli("job", "record", JOB_ID, key, value, success=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REFUSED:", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertEqual(self.state.read_bytes(), before)

    def test_legacy_record_preserves_explicit_zero_and_false(self):
        self.write_job()
        for key, value in [("owner_minutes", "0"), ("cost_usd", "0"), ("escaped", "false"),
                           ("owner_changes", "true")]:
            self.cli("job", "record", JOB_ID, key, value)
        state = json.loads(self.state.read_text())
        self.assertEqual((state["owner_minutes"], state["cost_usd"]), (0, 0))
        self.assertIs(state["escaped"], False)
        self.assertIs(state["owner_changes"], True)

    def test_observe_retries_keep_the_first_ingestion_and_preserve_job_state(self):
        self.write_job()
        before = self.state.read_bytes()
        marker = self.job_dir / ".gate-publication.pending"
        marker.write_bytes(b"round-1\n")
        event = self.effort()
        first = json.loads(self.cli("job", "observe", JOB_ID, "-", input=json.dumps(event)).stdout)
        entry = self.root / "input.json"
        entry.write_text(json.dumps(event), encoding="utf-8")
        self.assertEqual(json.loads(self.cli("job", "observe", JOB_ID, str(entry)).stdout), first)
        self.assertEqual(first["data"]["minutes"], 0)
        self.assertTrue(first["recorded_at"].endswith("Z"))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(marker.read_bytes(), b"round-1\n")
        self.assertEqual(len(list((self.job_dir / "observations").glob("*.json"))), 1)

    def test_observe_refuses_unreadable_or_mismatched_state_before_publishing(self):
        self.write_job()
        valid = self.state.read_bytes()
        marker = self.job_dir / ".gate-publication.pending"
        marker.write_bytes(b"round-1\n")
        cases = {
            "wrong identity": valid.replace(JOB_ID.encode(), b"002-other"),
            "duplicate identity": valid.replace(b'"id":', b'"id": "002-other", "id":', 1),
            "invalid JSON": b"{",
        }
        for case, payload in cases.items():
            with self.subTest(case=case):
                self.state.write_bytes(payload)
                result = self.cli("job", "observe", JOB_ID, "-", input=json.dumps(self.effort()), success=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REFUSED:", result.stdout)
                self.assertFalse((self.job_dir / "observations").exists())
                self.assertEqual(self.state.read_bytes(), payload)
                self.assertEqual(marker.read_bytes(), b"round-1\n")

        self.state.write_bytes(valid)
        observed = json.loads(self.cli("job", "observe", JOB_ID, "-", input=json.dumps(self.effort())).stdout)
        self.assertEqual(observed["job_id"], JOB_ID)
        self.assertEqual(self.state.read_bytes(), valid)
        self.assertEqual(marker.read_bytes(), b"round-1\n")

    def test_observe_refuses_malformed_input_and_job_path_traversal(self):
        self.write_job()
        malformed = self.cli("job", "observe", JOB_ID, "-", input="{malformed", success=False)
        traversal = self.cli("job", "observe", f"../jobs/{JOB_ID}", "-",
                             input=json.dumps(self.effort()), success=False)
        for result in (malformed, traversal):
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("REFUSED:", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertFalse((self.job_dir / "observations").exists())

    def test_observe_rejects_duplicate_json_fields_before_publishing(self):
        self.write_job()
        raw = json.dumps(self.effort()).replace('"minutes": 0', '"minutes": 7, "minutes": 0')
        result = self.cli("job", "observe", JOB_ID, "-", input=raw, success=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.job_dir / "observations").exists())

    def test_card_reads_trusted_requirements_without_running_checks(self):
        self.write_job()
        (self.job_dir / "spec.md").write_text(
            "## Acceptance criteria\n1. A concrete acceptance criterion\n", encoding="utf-8")
        round_dir = self.job_dir / "round-1"
        round_dir.mkdir()
        (round_dir / "checks.md").write_text("GATE: PASS\n", encoding="utf-8")
        sentinel = self.factory / "checks-ran"
        (self.factory / "checks.sh").write_text(f"touch '{sentinel}'\n", encoding="utf-8")
        pre = self.state.read_bytes()
        card = self.cli("job", "card", JOB_ID).stdout
        self.assertIn("A concrete acceptance criterion", card)
        self.assertIn("review:security", card)
        self.assertIn("unverified", card.lower())
        self.assertFalse(sentinel.exists())
        self.assertEqual(self.state.read_bytes(), pre)

    def test_recovery_reports_exhaustion_without_moving_the_job(self):
        self.write_job(stage="recovery", round_number=2, history=[
            {"at": "2026-11-19T07:00:00Z", "stage": "intake", "note": "created"},
            {"at": "2026-11-19T07:15:00Z", "stage": "recovery", "note": "gate failed; 2 rounds used"},
        ])
        pre = self.state.read_bytes()
        recovery = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertIs(recovery["retry_exhausted"], True)
        self.assertEqual(self.state.read_bytes(), pre)

    def test_weekly_requires_explicit_timezone_for_the_cohort(self):
        unknown = json.loads(self.cli("scorecard", "--weekly", "2026-11-19", "--json").stdout)
        self.assertIsNone(unknown["delivered"])
        known = json.loads(self.cli("scorecard", "--weekly", "2026-11-19", "--timezone", "UTC",
                                    "--as-of", "2026-11-27T10:00:00Z", "--json").stdout)
        self.assertEqual(known["period"]["end_exclusive"], "2026-11-26")
        self.assertEqual(known["period"]["as_of"], "2026-11-27T10:00:00Z")
        self.assertEqual(known["delivered"]["count"], 0)
        self.assertIsNone(known["escaped_defects"]["rate"])

    def test_weekly_uses_the_ledger_at_the_requested_recording_cutoff(self):
        self.write_job()
        self.cli("job", "record", JOB_ID, "owner_minutes", "42")
        event = self.effort()
        self.cli("job", "observe", JOB_ID, "-", input=json.dumps(event))
        self.cli("job", "observe", JOB_ID, "-", input=json.dumps(self.effort(
            event_id="d725dbe5-fe6d-458c-b05e-4a6a4c12938b", supersedes_event_id=event["event_id"],
            data={"activity": "review", "minutes": 7, "rework": True},
        )))
        options = ("--weekly", "2026-11-19", "--timezone", "UTC", "--json")
        before = json.loads(self.cli("scorecard", *options, "--as-of", "2026-11-19T09:00:00Z").stdout)
        self.assertIsNone(before["owner_effort"]["known_minutes"])
        self.assertEqual(before["legacy"][JOB_ID]["owner_minutes"], 42)
        current = json.loads(self.cli("scorecard", *options, "--as-of", "2099-11-27T10:00:00Z").stdout)
        self.assertEqual(current["owner_effort"]["known_minutes"], 7)
        self.assertEqual(current["owner_effort"]["rework_minutes"], 7)

    def test_published_owner_handoff_enters_the_weekly_submission_cohort(self):
        options = ("--weekly", "2026-11-19", "--timezone", "UTC", "--json", "--as-of", "2099-11-27T10:00:00Z")
        for note, eligible in (("merge requires human verification of the PR head and base", 1),
                               ("merge conflict with base", 0)):
            with self.subTest(note=note):
                self.write_job(stage="needs-human", history=[
                    {"at": "2026-11-19T07:00:00Z", "stage": "intake", "note": "created"},
                    {"at": "2026-11-19T07:10:00Z", "stage": "merging", "note": "approver approved"},
                    {"at": "2026-11-19T07:15:00Z", "stage": "needs-human", "note": note},
                ])
                report = json.loads(self.cli("scorecard", *options).stdout)
                self.assertEqual(report["acceptance"]["rate"]["eligible"], eligible)
                self.assertIsNone(report["acceptance"]["rate"]["percent"])

    def test_legacy_mode_exposes_unknown_outcomes_and_the_frozen_baseline(self):
        self.write_job(stage="merged", owner_changes=False, escaped=False, owner_minutes=0)
        result = json.loads(self.cli("scorecard", "--json").stdout)["v1.2-two-phase"]
        self.assertIsNone(result["accepted_as_built"])
        self.assertIsNone(result["change_fail"])
        self.assertIsNone(result["first_pass_gate"])
        self.assertEqual(result["owner_minutes_median"], 0)
        self.assertEqual(result["coverage"]["change_fail"]["unknown"], 1)
        historical = self.cli("scorecard").stdout
        self.assertIn("2026-11-18", historical)
        self.assertIn("1 day", historical)
        self.assertEqual((self.factory / "baseline-v1.json").read_bytes(),
                         (ROOT / "factory" / "baseline-v1.json").read_bytes())
