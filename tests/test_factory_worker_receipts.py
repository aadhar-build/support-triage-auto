"""Job recovery surfaces bounded worker records without granting source verification."""
import hashlib
import json
import shutil
import unittest

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID


class WorkerReceiptCLI(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_job(origin="controlled")
        self.row = dict(kind="scope_refused", origin="controlled", job_id=JOB_ID, round=1,
                        input_sha="a" * 40, candidate_sha="b" * 40, base_sha="c" * 40,
                        spec_digest="d" * 64, policy_digest="e" * 64,
                        occurred_at="2026-01-10T20:01:02.000000Z")

    def report(self, rows):
        store = self.job_dir / "worker-events-v1"
        store.mkdir(exist_ok=True)
        for path in store.iterdir():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        for row in rows:
            fields = ["worker-events-v1", row["kind"], row["job_id"], row["origin"], str(row["round"]),
                      row["input_sha"], row["candidate_sha"], row["base_sha"], row["spec_digest"]]
            if row["kind"] == "builder_publication_recovered":
                fields.extend([row["audit_digest"], row["artifact_digest"]])
            else:
                fields.extend([row["policy_digest"], row.get("worktree_position", "")])
            name = hashlib.sha256("\0".join(fields).encode()).hexdigest() + ".json"
            (store / name).mkdir()
            (store / name / "receipt.json").write_text(json.dumps(row))
        before = {p.parent.name: p.read_bytes() for p in store.glob("*/receipt.json")}
        result = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertEqual({p.parent.name: p.read_bytes() for p in store.glob("*/receipt.json")}, before)
        return result

    def test_forged_well_formed_local_record_never_authenticates_issuer(self):
        report = self.report([self.row])
        self.assertEqual(report["worker_receipts"], [dict(self.row, verification="unverified")])
        self.assertEqual(report["incident_and_interruption_source_verification"], "unavailable")
        self.assertEqual(report["interruptions"], [])
        self.assertEqual(report["controlled"]["interruptions"], [])

    def test_extra_fields_invalid_bindings_and_wrong_origin_never_reach_operator(self):
        for bad in [dict(debug="private-material"), dict(policy_digest="bad"), dict(job_id="other"),
                    dict(origin="real"), dict(round=True), dict(kind="arbitrary-private-text"),
                    dict(occurred_at="2026-01-10x20:01:02.000000Z"),
                    dict(occurred_at="2026-01-10T20:01:02.000000+02:00")]:
            with self.subTest(bad=bad):
                report = self.report([{**self.row, **bad}])
                self.assertEqual(report["worker_receipts"], [])
                self.assertTrue(report["diagnostics"])
                self.assertNotIn("private", str(report))

    def test_pending_or_active_recovery_is_hidden(self):
        row = dict(self.row, kind="builder_publication_recovered", audit_digest="1"*64, artifact_digest="2"*64)
        for suffix in ("pending", "lock", "replacing"):
            with self.subTest(suffix=suffix):
                marker = self.job_dir / f"worker-round-1-builder.json.{suffix}"
                marker.touch()
                self.assertEqual(self.report([row])["worker_receipts"], [])
                marker.unlink()

    def test_weekly_shows_period_scoped_unverified_receipts_outside_verified_metrics(self):
        opts = ("--weekly", "2026-11-19", "--timezone", "UTC", "--as-of", "2026-11-20T00:00:00Z")
        self.write_job(origin="real")
        current = {**self.row, "origin": "real", "occurred_at": "2026-11-19T09:00:00.000000Z"}
        old = {**current, "candidate_sha": "1"*40, "occurred_at": "2026-11-18T23:59:59.000000Z"}
        later = {**current, "candidate_sha": "2"*40, "occurred_at": "2026-11-20T00:00:01.000000Z"}
        self.report([old, current, later])
        weekly = json.loads(self.cli("scorecard", *opts, "--json").stdout)
        self.assertEqual(weekly["worker_receipts"]["records"], [dict(current, verification="unverified")])
        self.assertEqual(weekly["guardrails"]["confirmed_blocked_observed"], 0)
        self.assertIsNone(weekly["guardrails"]["effective_total"])
        self.assertEqual(weekly["interruptions"]["observed"], 0)
        self.assertIn("Unverified worker receipts", self.cli("scorecard", *opts).stdout)
        self.write_job(origin="controlled")
        self.report([{**current, "origin": "controlled"}])
        weekly = json.loads(self.cli("scorecard", *opts, "--json").stdout)
        self.assertEqual(weekly["worker_receipts"]["records"], [])

    def test_corrupt_sibling_does_not_hide_valid_receipt_or_echo_raw_data(self):
        self.report([self.row])
        (self.job_dir / "worker-events-v1" / ("a" * 64 + ".json")).write_text("private-material")
        report = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertEqual(report["worker_receipts"], [dict(self.row, verification="unverified")])
        self.assertTrue(report["diagnostics"])
        self.assertNotIn("private-material", str(report))

    def test_crashed_staging_does_not_consume_inventory_or_expose_partial_receipts(self):
        self.report([self.row])
        store = self.job_dir / "worker-events-v1"
        stage = store / ".staging"
        stage.mkdir()
        for i in range(1025):
            (stage / str(i)).write_text('{"partial":')
        report = json.loads(self.cli("job", "recovery", JOB_ID).stdout)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(report["worker_receipts"], [dict(self.row, verification="unverified")])
