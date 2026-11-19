"""Exercise opt-in verification without calling GitHub or using credentials."""

import json
import os
import unittest

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID


CANDIDATE = "a" * 40
SQUASH = "b" * 40
MERGED_AT = "2026-11-19T07:15:00Z"
DELIVERY = "https://api.github.com/repos/fixture/software-factory/hooks/8/deliveries/12"


class MergeEvidenceIntegration(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_job(stage="merged")
        self.stub = self.root / "stub-bin"
        self.stub.mkdir()
        self.response = self.root / "stub-response.json"
        self.calls = self.root / "github-reads"
        command = self.stub / "gh"
        command.write_text("""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

with Path('github-reads').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
print('fixture-private-auth-output', file=sys.stderr)
response = Path('stub-response.json')
if not response.exists():
    sys.exit(1)
print(response.read_text())
""", encoding="utf-8")
        command.chmod(0o755)
        self.environment = dict(os.environ, PATH=f"{self.stub}{os.pathsep}{os.environ.get('PATH', '')}")

    def observe(self, event):
        return self.cli("job", "observe", JOB_ID, "-", input=json.dumps(event))

    def delivery(self, url=DELIVERY, sha=CANDIDATE, event_id="6d285466-8cbe-4fa9-9c00-3e264990f307"):
        self.observe({
            "schema_version": 1, "event_id": event_id, "job_id": JOB_ID, "kind": "check_evidence",
            "origin": "real", "occurred_at": MERGED_AT, "candidate_sha": sha, "round": 1,
            "source": {"kind": "controller", "record_id": "github-delivery-12"},
            "evidence": [{"url": url}], "data": {"check_id": "merged_candidate", "verdict": "pass"},
        })

    def escaped(self):
        self.observe({
            "schema_version": 1, "event_id": "8cac21ec-e9f8-4229-a000-bba23324371e", "job_id": JOB_ID,
            "kind": "defect", "origin": "real", "occurred_at": "2026-11-20T08:00:00Z",
            "candidate_sha": CANDIDATE, "source": {"kind": "owner", "record_id": "defect-1"},
            "data": {"defect_id": "defect-1", "origin": "escaped", "discovered_at": "2026-11-20T08:00:00Z"},
        })

    def local_candidate(self):
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit",
                 "--allow-empty", "-qm", "Candidate")
        self.git("branch", f"factory/{JOB_ID}", "HEAD")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def github(self, **changes):
        self.response.write_text(json.dumps({
            "id": 12, "event": "pull_request", "action": "closed", "repository_id": 321,
            "delivered_at": "2026-11-19T07:15:01Z", "payload_action": "closed", "repo_id": 321,
            "repository": "fixture/software-factory", "pr": 2, "pr_number": 2,
            "merged": True, "merged_at": MERGED_AT, "merge_commit_sha": SQUASH,
            "candidate_sha": CANDIDATE, "head_ref": f"factory/{JOB_ID}",
            "head_repository": "fixture/software-factory", "base_ref": "main",
            "base_repository": "fixture/software-factory", "base_sha": "c" * 40,
            **changes,
        }), encoding="utf-8")

    def weekly(self, *, verify=True):
        options = ["--weekly", "2026-11-19", "--timezone", "UTC", "--as-of", "2099-11-27T10:00:00Z",
                   "--defect-window-days", "7", "--json"]
        if verify:
            options.append("--verify-github-merges")
        result = self.cli("scorecard", *options, env=self.environment)
        self.assertNotIn("fixture-private-auth-output", result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_squash_delivery_identifies_affected_change_only_when_opted_in(self):
        self.assertNotEqual(self.local_candidate(), CANDIDATE)
        self.delivery()
        self.escaped()
        self.github()
        offline = self.weekly(verify=False)
        self.assertFalse(self.calls.exists())
        self.assertEqual(offline["escaped_defects"]["known_affected_jobs"], [])
        report = self.weekly()
        self.assertEqual(report["escaped_defects"]["known_affected_jobs"], [JOB_ID])
        self.assertEqual(report["merge_verification"][JOB_ID]["candidate_sha"], CANDIDATE)
        self.assertEqual(report["merge_verification"][JOB_ID]["merge_commit_sha"], SQUASH)
        argv = json.loads(self.calls.read_text().strip())
        self.assertEqual(argv[:5], ["api", "--hostname", "github.com", "--method", "GET"])
        self.assertEqual(argv[-1], "repos/fixture/software-factory/hooks/8/deliveries/12")

    def test_verified_merge_inherits_the_round_when_controller_history_and_head_agree(self):
        candidate = self.local_candidate()
        self.delivery(sha=candidate)
        self.github(candidate_sha=candidate)
        report = self.weekly()
        self.assertEqual(report["merge_verification"][JOB_ID]["round"], 1)

    def test_different_head_sha_stays_unknown(self):
        self.delivery()
        self.escaped()
        self.github(candidate_sha="e" * 40)
        report = self.weekly()
        self.assertEqual(report["merge_verification"][JOB_ID]["status"], "mismatch")
        self.assertEqual(report["escaped_defects"]["known_affected_jobs"], [])

    def test_spoofed_merger_reference_never_calls_gh(self):
        self.delivery(DELIVERY.replace("api.github.com/", "api.github.com.invalid/"))
        report = self.weekly()
        self.assertEqual(report["merge_verification"][JOB_ID]["status"], "reference_unverified")
        self.assertFalse(self.calls.exists())

    def test_competing_delivery_references_do_not_select_a_winner(self):
        self.delivery()
        self.delivery(DELIVERY.replace("/12", "/13"), event_id="d725dbe5-fe6d-458c-b05e-4a6a4c12938b")
        self.github()
        report = self.weekly()
        self.assertEqual(report["merge_verification"][JOB_ID]["status"], "ambiguous")
        self.assertFalse(self.calls.exists())

    def test_expired_or_inaccessible_delivery_keeps_unknown_without_auth_output(self):
        self.delivery()
        self.escaped()
        report = self.weekly()
        self.assertEqual(report["merge_verification"][JOB_ID]["status"], "unavailable")
        self.assertEqual(report["escaped_defects"]["known_affected_jobs"], [])

    def test_wrong_repo_delivery_and_post_cutoff_delivery_stay_unknown(self):
        self.delivery()
        for changes in ({"repository": "other/software-factory"}, {"delivered_at": "2100-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.github(**changes)
                report = self.weekly()
                self.assertEqual(report["merge_verification"][JOB_ID]["status"], "mismatch")
