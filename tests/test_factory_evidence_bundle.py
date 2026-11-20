"""Offline bundle checks with disposable factory observations and receipts."""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.test_factory_observation_integration import FactoryCLI, JOB_ID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "factory" / "lib"))
import evidence_bundle as bundle
from observations import append_event


def receipt(kind="check", verdict="pass"):
    return (json.dumps({"schema_version": 1, "kind": kind, "verdict": verdict},
                       sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


class EvidenceBundleTests(FactoryCLI, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.job_dir = self.root / "factory" / "jobs" / JOB_ID
        self.state = self.job_dir / "job.json"
        self.write_job(origin="controlled", title="<script>private</script>")
        self.evidence = self.job_dir / "evidence"
        self.evidence.mkdir()
        self.output = self.root / "export"
        self.ref = "evidence/check-1.json"
        self.candidate = None

    def commit_fixture(self):
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def bind_candidate(self):
        if self.candidate is None:
            self.candidate = self.commit_fixture()
            self.git("branch", f"factory/{JOB_ID}", self.candidate)
            self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        return self.candidate

    def observe(self, refs, verdict="pass", kind="check_evidence", **extra):
        field = {"check_evidence": "check_id", "criterion": "criterion_id", "demo": "demo_id"}[kind]
        append_event(self.job_dir, {
            "schema_version": 1, "job_id": JOB_ID, "event_id": str(uuid4()), "kind": kind,
            "origin": "controlled", "source": {"kind": "fixture", "record_id": "credential-private"},
            "candidate_sha": self.bind_candidate(), "round": 1, "occurred_at": "2026-01-01T00:00:00Z",
            "data": {field: "secret-in-identifier", "verdict": verdict}, "evidence": refs, **extra,
        })

    def add(self, path, payload=None, *, verdict="pass", written=True, **extra):
        payload = receipt(verdict=verdict) if payload is None else payload
        if written:
            (self.job_dir / path).write_bytes(payload)
        self.observe([{"path": path, "sha256": hashlib.sha256(payload).hexdigest()}], verdict, **extra)

    def export(self, *paths):
        return bundle.export_job(self.root, JOB_ID, self.output, paths or [self.ref])

    def test_cli_and_static_links(self):
        self.add(self.ref)
        self.add("evidence/check-2.json", verdict="pending")
        self.add("evidence/check-3.json", written=False)
        self.observe([{"url": "https://example.invalid/private"}], verdict="fail")
        state = self.state.read_bytes()
        result = self.cli("evidence-bundle", JOB_ID, "--output", str(self.output),
                          "--include", self.ref, "--include", "evidence/check-3.json")
        self.assertEqual(result.returncode, 0)
        data = json.loads((self.output / "manifest.json").read_bytes())
        self.assertEqual(data["origin"], "controlled")
        self.assertEqual({r["artifact_status"] for r in data["rows"]}, {"copied", "withheld", "missing"})
        self.assertTrue(all(r["claim_verification"] == "unverified" for r in data["rows"]))
        copied = next(r for r in data["rows"] if r["artifact_status"] == "copied")
        self.assertEqual((self.output / copied["file"]).read_bytes(), receipt())
        self.assertEqual(hashlib.sha256(receipt()).hexdigest(), copied["sha256"])
        page = (self.output / "index.html").read_text()
        self.assertIn("default-src 'none'", page)
        self.assertIn('href="' + copied["file"] + '"', page)
        self.assertEqual(self.state.read_bytes(), state)
        for item in ("private", "secret-in-identifier", "example.invalid", "<script>", "https://"):
            self.assertNotIn(item, page + str(data))
        self.assertEqual({p.name for p in self.output.iterdir()}, {"manifest.json", "index.html", copied["file"]})

    def test_withholds_unreviewed_payloads_and_stale_digest(self):
        unsafe = [b'{"api_key":"sk-fixture"}', b"<script>fixture</script>", b"https://example.invalid/?token=fixture",
                  receipt().rstrip(), b"X" * 65536, receipt(verdict="fail")]
        for i, content in enumerate(unsafe, 1):
            self.add(f"evidence/check-{i}.json", content)
        self.add("evidence/check-7.json")
        (self.evidence / "check-7.json").write_bytes(receipt(verdict="pending"))
        self.export(*(f"evidence/check-{i}.json" for i in range(1, 8)))
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        self.assertEqual(sum(r["artifact_status"] == "stale" for r in rows), 1)
        self.assertEqual(sum(r["artifact_status"] == "withheld" for r in rows), 6)
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_missing_reference_or_unsafe_selection_does_not_create_output(self):
        self.add(self.ref)
        for invalid in (".env", "evidence/check-1.json?token=private", "../job.json", "/etc/passwd",
                        "evidence/check-9.json", "evidence/check-1.html"):
            with self.subTest(invalid=invalid), self.assertRaises(bundle.BundleError):
                self.export(invalid)
            self.assertFalse(self.output.exists())

    def test_evidence_links_and_special_files(self):
        self.add(self.ref)
        target = self.evidence / "check-1.json"
        other = self.root / "outside"
        target.rename(other)
        for attempt in ("symlink", "hardlink", "fifo"):
            with self.subTest(attempt=attempt):
                if attempt == "symlink":
                    target.symlink_to(other)
                elif attempt == "hardlink":
                    os.link(other, target)
                else:
                    os.mkfifo(target)
                self.output = self.root / attempt
                self.export()
                self.assertIn("withheld", (self.output / "manifest.json").read_text())
                self.assertEqual(len(list(self.output.iterdir())), 2)
                target.unlink()
        self.evidence.rename(self.root / "outside-dir")
        self.evidence.symlink_to(self.root / "outside-dir", target_is_directory=True)
        self.output = self.root / "nested"
        self.export()
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_no_existing_output_or_symlinked_ancestor(self):
        self.add(self.ref)
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("keep")
        with self.assertRaises(bundle.BundleError):
            self.export()
        self.assertEqual(list(self.output.iterdir()), [marker])
        alias = self.root / "alias"
        alias.symlink_to(self.output, target_is_directory=True)
        self.output = alias / "new"
        with self.assertRaises(bundle.BundleError):
            self.export()
        self.assertFalse(self.output.exists())
        alias_root = self.root / "alias-root"
        alias_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(bundle.BundleError):
            bundle.export_job(alias_root, JOB_ID, self.root / "bad", [self.ref])

    def test_source_changes_during_copy(self):
        self.add(self.ref)
        real_write = bundle._write_file
        def changing_write(directory, name, content):
            original = real_write(directory, name, content)
            if name.endswith(".json") and name != "manifest.json":
                (self.job_dir / self.ref).write_bytes(receipt(verdict="fail"))
            return original
        with patch.object(bundle, "_write_file", changing_write):
            self.export()
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        self.assertEqual(rows[0]["artifact_status"], "stale")
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_source_changes_during_publication_prevent_a_completed_bundle(self):
        self.add(self.ref)
        original_state = self.state.read_bytes()
        source = self.job_dir / self.ref
        real_write = bundle._write_file
        for changed_path, changed_bytes in (
            (source, receipt(verdict="fail")),
            (self.state, original_state + b"\n"),
        ):
            with self.subTest(path=changed_path.name):
                self.output = self.root / f"export-{changed_path.stem}"
                source.write_bytes(receipt())
                self.state.write_bytes(original_state)

                def changing_write(directory, name, content):
                    result = real_write(directory, name, content)
                    if name == "index.html":
                        changed_path.write_bytes(changed_bytes)
                    return result

                with patch.object(bundle, "_write_file", changing_write), self.assertRaises(bundle.BundleError):
                    self.export()
                self.assertFalse(self.output.exists())

    def test_output_tampering_is_detected_before_index(self):
        self.add(self.ref)
        real_write = bundle._write_file
        def changing_write(directory, name, content):
            original = real_write(directory, name, content)
            if name not in ("index.html", "manifest.json"):
                (self.output / name).write_bytes(b"changed")
            return original
        with patch.object(bundle, "_write_file", changing_write), self.assertRaises(bundle.BundleError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_collision_and_unexpected_files_are_preserved(self):
        self.add(self.ref)
        real_write = bundle._write_file
        for attempt in ("collision", "late-file", "late-replacement"):
            with self.subTest(attempt=attempt):
                self.output = self.root / attempt
                protected = self.output / ("manifest.json" if attempt == "collision" else "extra")
                def interfere(directory, name, content):
                    if name == "manifest.json" and attempt == "collision":
                        protected.write_bytes(b"other owner")
                    result = real_write(directory, name, content)
                    if name == "index.html" and attempt == "late-file":
                        protected.write_bytes(b"other owner")
                    if name == "index.html" and attempt == "late-replacement":
                        protected.write_bytes(b"other owner")
                        protected.replace(self.output / "receipt-001.json")
                    return result
                with patch.object(bundle, "_write_file", interfere), self.assertRaises(bundle.BundleError):
                    self.export()
                if attempt == "late-replacement":
                    protected = self.output / "receipt-001.json"
                self.assertEqual(list(self.output.iterdir()), [protected])
                self.assertEqual(protected.read_bytes(), b"other owner")

    def test_job_state_destinations_and_cross_origin(self):
        self.add(self.ref, origin="real", source={"kind": "controller", "record_id": "synthetic"})
        with self.assertRaises(bundle.BundleError):
            self.export()
        self.add(self.ref, round=2)
        self.output = self.job_dir / "new"
        with self.assertRaises(bundle.BundleError):
            self.export()
        self.output = self.root / "export"
        self.export()
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_binding"], "stale")
        self.assertNotIn("file", rows[0])

    def test_factory_gate_projection_excludes_raw_logs(self):
        folder = self.job_dir / "round-1"
        folder.mkdir()
        candidate = self.bind_candidate().encode()
        spec = b"## Acceptance criteria\n1. Inspect the fixture\n"
        (self.job_dir / "spec.md").write_bytes(spec)
        for case, header in (
            ("canonical", b"GATE: PASS"),
            ("installed", b"GATE: FAIL\nCANDIDATE: " + candidate + b"\nBASE: " + candidate
             + b"\nSPEC-DIGEST: " + hashlib.sha256(spec).hexdigest().encode()),
        ):
            with self.subTest(case=case):
                self.output = self.root / case
                raw = header + b"\n\n## Summary\n- synthetic-private-value\nhttps://fixture.invalid/?token=synthetic\n"
                (folder / "checks.md").write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--gate",
                         "round-1/checks.md@" + digest)
                manifest = json.loads((self.output / "manifest.json").read_text())
                row, = manifest["rows"]
                self.assertEqual(row["artifact_status"], "projected")
                self.assertEqual(row["source_sha256"], digest)
                projection = (self.output / row["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(projection).hexdigest(), row["sha256"])
                self.assertNotEqual(row["sha256"], digest)
                self.assertEqual(json.loads(projection)["verdict"], "pass" if case == "canonical" else "fail")
                for file in self.output.iterdir():
                    self.assertNotIn(b"synthetic-private", file.read_bytes())
                    self.assertNotIn(b"https://", file.read_bytes())
        for path, expected in (("round-2/checks.md", False), ("round-1/checks.md", True)):
            self.output = self.root / "stale-gate"
            result = self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--gate",
                              path + "@" + "d"*64, success=False)
            if expected:
                self.assertEqual(result.returncode, 0)
                row, = json.loads((self.output / "manifest.json").read_text())["rows"]
                self.assertEqual(row["artifact_status"], "stale")
                self.assertEqual(row["reported_verdict"], "unknown")
            else:
                self.assertNotEqual(result.returncode, 0)

    def test_bound_gate_checks_base_and_spec_even_when_candidate_does_not_change(self):
        candidate = self.bind_candidate()
        new_base = self.commit_fixture()
        spec = self.job_dir / "spec.md"
        original = b"## Acceptance criteria\n1. Original fixture\n"
        raw = (f"GATE: PASS\nCANDIDATE: {candidate}\nBASE: {candidate}\n"
               f"SPEC-DIGEST: {hashlib.sha256(original).hexdigest()}\n\n## Summary\n").encode()
        round_dir = self.job_dir / "round-1"
        round_dir.mkdir()
        (round_dir / "checks.md").write_bytes(raw)
        self.add(self.ref, data={
            "check_id": "fixture", "verdict": "pass", "base_sha": candidate,
            "spec_digest": hashlib.sha256(original).hexdigest(),
        })
        for case, base, current_spec, expected in (
            ("changed-base", new_base, original, "stale"),
            ("changed-spec", candidate, original + b"\n", "stale"),
            ("missing-base", None, original, "withheld"),
            ("missing-spec", candidate, None, "withheld"),
        ):
            with self.subTest(case=case):
                if base is None:
                    self.git("update-ref", "-d", "refs/remotes/origin/main")
                else:
                    self.git("update-ref", "refs/remotes/origin/main", base)
                if current_spec is None:
                    spec.unlink(missing_ok=True)
                else:
                    spec.write_bytes(current_spec)
                self.output = self.root / case
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--include", self.ref,
                         "--gate", "round-1/checks.md@" + hashlib.sha256(raw).hexdigest())
                rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
                self.assertEqual({row["artifact_status"] for row in rows}, {expected})
                self.assertEqual({row["kind"] for row in rows}, {"check", "gate"})
                self.assertEqual(next(row for row in rows if row["kind"] == "gate")["reported_verdict"], "unknown")
                self.assertFalse(any("file" in row for row in rows))

    def test_normal_factory_gate_from_writer_to_bundle(self):
        from tests.test_factory_observation_integration import FactoryRecoveryTransition
        FactoryRecoveryTransition.make_gate(self, round_number=2)
        state = json.loads(self.state.read_bytes())
        self.state.write_text(json.dumps({**state, "origin": "controlled"}))
        self.assertIn("gate FAIL", self.cli("gate", JOB_ID).stdout)
        raw = (self.job_dir / "round-2" / "checks.md").read_bytes()
        self.assertIn(b"private-check-output", raw)
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output),
                 "--gate", "round-2/checks.md@" + hashlib.sha256(raw).hexdigest())
        manifest = json.loads((self.output / "manifest.json").read_text())
        row, = manifest["rows"]
        self.assertEqual(row["artifact_status"], "projected")
        self.assertEqual(row["reported_verdict"], "fail")
        self.assertEqual((self.output / row["file"]).read_bytes(), receipt(verdict="fail"))
        self.assertEqual(row["claim_verification"], "unverified")

    def test_selected_native_review_decision_and_final_gate_project_the_verdict_only(self):
        (self.job_dir / "round-1").mkdir()
        reports = {
            "round-1/review-code.md": (b"VERDICT: PASS\r\n", "review", "pass"),
            "round-1/review-security.md": (b"VERDICT: CHANGES\n", "review", "changes"),
            "decision.md": (b"ESCALATE\n", "decision", "escalate"),
            "final-checks.md": (b"GATE: FAIL\n\n## Summary\n", "check", "fail"),
        }
        args = []
        for path, (header, _, _) in reports.items():
            content = header + b"\nprivate reporter prose and https://fixture.invalid/private\n"
            (self.job_dir / path).write_bytes(content)
            args.extend(("--artifact", f"{path}@{hashlib.sha256(content).hexdigest()}"))
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), *args)
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        self.assertEqual(set(reports), {row["reference"] for row in rows})
        for row in rows:
            path = row["reference"]
            _, kind, verdict = reports[path]
            self.assertEqual((row["artifact_status"], row["candidate_binding"], row["claim_verification"]),
                             ("projected", "unverified", "unverified"))
            self.assertEqual(row["source_sha256"], hashlib.sha256((self.job_dir / path).read_bytes()).hexdigest())
            exported = (self.output / row["file"]).read_bytes()
            self.assertEqual(json.loads(exported), {"schema_version": 1, "kind": kind, "verdict": verdict})
            self.assertEqual(row["sha256"], hashlib.sha256(exported).hexdigest())
            self.assertIn(f'href="{row["file"]}"', (self.output / "index.html").read_text())
        for file in self.output.iterdir():
            self.assertNotIn(b"private", file.read_bytes())
            self.assertNotIn(b"https://", file.read_bytes())

    def test_native_selections_require_the_current_round_and_a_supported_slot(self):
        digest = "1" * 64
        for path in ("round-2/review-code.md", "round-1/review-private.md", "spec.md",
                     "round-1/../decision.md", "decision.md?token=private"):
            with self.subTest(path=path):
                result = self.cli("evidence-bundle", JOB_ID, "--output", str(self.output),
                                  "--artifact", f"{path}@{digest}", success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_native_projections_show_unavailable_evidence_without_links(self):
        (self.job_dir / "round-1").mkdir()
        (self.job_dir / "round-1" / "review-code.md").write_bytes(b"VERDICT: PASS extra\n")
        (self.job_dir / "decision.md").write_bytes(b"APPROVE\n")
        args = []
        for path, digest in (
            ("round-1/review-code.md", hashlib.sha256(b"VERDICT: PASS extra\n").hexdigest()),
            ("decision.md", "1" * 64),
            ("round-1/review-security.md", "2" * 64),
        ):
            args.extend(("--artifact", f"{path}@{digest}"))
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), *args)
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        self.assertEqual({row["reference"]: row["artifact_status"] for row in rows}, {
            "round-1/review-code.md": "withheld", "decision.md": "stale", "round-1/review-security.md": "missing",
        })
        self.assertTrue(all(row["reported_verdict"] == "unknown" and "file" not in row for row in rows))
        self.assertEqual({file.name for file in self.output.iterdir()}, {"index.html", "manifest.json"})

    def test_review_header_rejects_conflicts_and_matches_controller_normalization(self):
        target = self.job_dir / "round-1" / "review-code.md"
        target.parent.mkdir()
        for case, content, status, verdict in (
            ("conflict", b"VERDICT: PASS\nVERDICT: Changes\n", "withheld", "unknown"),
            ("middle-conflict", b"VERDICT: PASS\nVERDICT: CHANGES\nVERDICT: PASS\n", "withheld", "unknown"),
            ("valid", b"\nVERDICT:  changes\t\n\nVERDICT: PASS\n", "projected", "changes"),
        ):
            with self.subTest(case=case):
                target.write_bytes(content)
                self.output = self.root / case
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--artifact",
                         f"round-1/review-code.md@{hashlib.sha256(content).hexdigest()}")
                row, = json.loads((self.output / "manifest.json").read_bytes())["rows"]
                self.assertEqual((row["artifact_status"], row["reported_verdict"]), (status, verdict))
                self.assertEqual("file" in row, status == "projected")

    def test_overview_reports_additional_required_reviews_without_their_names(self):
        self.write_job(origin="controlled", reviewers={"code": "haiku", "private-audit": "haiku",
                                                       "private-compliance": "haiku"})
        round_dir = self.job_dir / "round-1"
        round_dir.mkdir()
        (round_dir / "review-private-audit.md").write_text("VERDICT: PASS\nprivate review")
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview")
        manifest = (self.output / "manifest.json").read_text()
        rows = {row["label"]: row for row in json.loads(manifest)["rows"]}
        for label, status in (("Additional review 1", "withheld"), ("Additional review 2", "missing")):
            row = rows[label]
            self.assertTrue(row["required"])
            self.assertEqual(row["artifact_status"], status)
            self.assertNotIn("reference", row)
        page = (self.output / "index.html").read_text()
        self.assertNotIn("private-audit", manifest + page)
        self.assertNotIn("private-compliance", manifest + page)

    def test_overview_states_when_criteria_are_unavailable(self):
        spec = self.job_dir / "spec.md"
        for case, content, status in (
            ("absent", None, "missing"),
            ("invalid-encoding", b"\xff", "withheld"),
            ("oversized", b"x" * (256 * 1024 + 1), "withheld"),
        ):
            with self.subTest(case=case):
                if content is not None:
                    spec.write_bytes(content)
                self.output = self.root / case
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview")
                rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
                row, = [row for row in rows if row.get("label") == "Acceptance criteria unavailable"]
                self.assertEqual((row["kind"], row["artifact_status"]), ("criterion", status))
                self.assertTrue(row["required"])

    def test_overview_links_a_check_observation_to_its_current_criterion(self):
        candidate = self.bind_candidate()
        spec = b"## Acceptance criteria\n1. [private-criterion] Inspect the fixture\n"
        (self.job_dir / "spec.md").write_bytes(spec)
        digest = hashlib.sha256(spec).hexdigest()
        self.add(self.ref, data={
            "check_id": "criterion:private-criterion", "verdict": "pass",
            "base_sha": candidate, "spec_digest": digest,
        })
        for case, expected in (("current", "copied"), ("changed-spec", "missing")):
            with self.subTest(case=case):
                self.output = self.root / case
                if case == "changed-spec":
                    (self.job_dir / "spec.md").write_bytes(spec + b"\n")
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview",
                         "--include", self.ref)
                manifest = (self.output / "manifest.json").read_text()
                row, = [row for row in json.loads(manifest)["rows"] if row.get("label") == "Criterion 1"]
                self.assertEqual((row["artifact_status"], row["claim_verification"]), (expected, "unverified"))
                self.assertEqual("file" in row, expected == "copied")
                self.assertNotIn("private-criterion", manifest)

    def test_overview_links_current_check_without_optional_bindings_and_rejects_stale_candidate(self):
        self.bind_candidate()
        (self.job_dir / "spec.md").write_text("## Acceptance criteria\n1. [fixture] Inspect the fixture\n")
        self.add(self.ref, data={"check_id": "criterion:fixture", "verdict": "pass"})
        for case, expected in (("current", "copied"), ("changed-candidate", "missing")):
            with self.subTest(case=case):
                self.output = self.root / case
                if case == "changed-candidate":
                    self.git("update-ref", f"refs/heads/factory/{JOB_ID}", self.commit_fixture())
                self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview",
                         "--include", self.ref)
                rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
                row, = [row for row in rows if row.get("label") == "Criterion 1"]
                self.assertEqual((row["artifact_status"], row["claim_verification"]), (expected, "unverified"))
                self.assertEqual("file" in row, expected == "copied")

    def test_overview_opens_without_a_selected_receipt_and_marks_missing_evidence(self):
        self.write_job(stage="needs-human", origin="controlled", title="private draft")
        (self.job_dir / "spec.md").write_text(
            "## Acceptance criteria\n1. [private-first] Private first criterion\n"
            "2. [private-second] Private second criterion\n"
        )
        (self.job_dir / "round-1").mkdir()
        (self.job_dir / "round-1" / "review-code.md").write_bytes(b"VERDICT: PASS\nprivate prose\n")
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview")
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        self.assertEqual(manifest["overview"], {"stage": "needs-human", "round": 1, "candidate_sha": None})
        rows = {row["label"]: row for row in manifest["rows"]}
        for label in ("Criterion 1", "Criterion 2", "Working-software demo", "Security review"):
            self.assertEqual(rows[label]["artifact_status"], "missing")
        self.assertEqual(rows["Code review"]["artifact_status"], "withheld")
        self.assertTrue(rows["Security review"]["required"])
        self.assertFalse(rows["Eval review"]["required"])
        self.assertTrue(all("file" not in row and row["reported_verdict"] == "unknown" for row in rows.values()))
        page = (self.output / "index.html").read_text()
        self.assertIn("needs-human", page)
        for private in ("private draft", "private-first", "private-second", "Private first criterion", "private prose"):
            self.assertNotIn(private, page + str(manifest))
        self.assertEqual({file.name for file in self.output.iterdir()}, {"index.html", "manifest.json"})

    def test_overview_connects_a_selected_review_to_its_requirement_without_duplicate_rows(self):
        (self.job_dir / "round-1").mkdir()
        review = b"VERDICT: CHANGES\nprivate review\n"
        (self.job_dir / "round-1" / "review-security.md").write_bytes(review)
        self.cli("evidence-bundle", JOB_ID, "--output", str(self.output), "--overview", "--artifact",
                 f"round-1/review-security.md@{hashlib.sha256(review).hexdigest()}")
        rows = json.loads((self.output / "manifest.json").read_bytes())["rows"]
        row, = [row for row in rows if row.get("label") == "Security review"]
        self.assertTrue(row["required"])
        self.assertEqual((row["artifact_status"], row["reported_verdict"]), ("projected", "changes"))
        self.assertTrue((self.output / row["file"]).exists())

    def test_mobile_viewport_and_only_the_pinned_stylesheet(self):
        import base64
        import re
        self.add(self.ref)
        self.export()
        page = (self.output / "index.html").read_text()
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)
        css = re.search(r"<style>(.+?)</style>", page, flags=re.S)[1]
        digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
        self.assertIn(f"style-src 'sha256-{digest}'", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("'unsafe-inline'", page)
        self.assertIn('data-label="Evidence"', page)
