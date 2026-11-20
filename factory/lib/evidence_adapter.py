"""Read the local job, ledger, and any exact receipts supported by this install."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import factory as F
from job_links import public_repo, verified_links
from guardrails import evaluate_guardrails, read_job_artifact
from observations import ObservationError, _artifact_path, _open_job, _url, load_events


_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CRITERION = re.compile(r"\s*(\d{1,6})\.\s+(?:\[([a-zA-Z0-9_.-]{1,48})\]\s+)?(.+)")
_CHECKS = "checks"
_FINAL_CHECKS = "final_checks"
_FINAL_RECEIPT = re.compile(
    rb"GATE: PASS\nCANDIDATE: (?P<candidate_sha>[0-9a-f]{40}|[0-9a-f]{64})\n"
    rb"BASE: (?P<base_sha>[0-9a-f]{40}|[0-9a-f]{64})\nSPEC-DIGEST: (?P<spec_digest>[0-9a-f]{64})\n\n"
)
_TRACKER_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9-]+/(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*"
)


def tracker_url(value: str | None) -> str | None:
    return value if isinstance(value, str) and _TRACKER_URL.fullmatch(value) else None


def _report_artifact_path(value: str | None) -> str | None:
    try:
        return _artifact_path(value, "report evidence path")
    except ObservationError:
        return None


def local_reference(job_id: str, path: str) -> str | None:
    if not isinstance(job_id, str) or not F.valid_reporting_job_id(job_id):
        return None
    if path := _report_artifact_path(path):
        return f"factory/jobs/{job_id}/{path}"
    return None


def _stable_evidence_url(value: str | None) -> str | None:
    try:
        return _url(value, "report evidence link")
    except ObservationError:
        return None


def has_evidence_reference(ref: dict) -> bool:
    return isinstance(ref, dict) and bool(
        _report_artifact_path(ref.get("path")) or _stable_evidence_url(ref.get("url"))
    )


def evidence_reference(ref: dict, job_id: str, *, trusted_urls=(), label="Evidence") -> str | None:
    if not isinstance(ref, dict):
        return None
    url = ref.get("url")
    if url in trusted_urls and _stable_evidence_url(url):
        return f"[{label}]({url})"
    path = local_reference(job_id, ref.get("path"))
    if path:
        return f"Local {label.lower()} reference `{path}`"
    return None


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("ambiguous job state")
    return fields


def _reject_constant(value):
    raise ValueError("nonfinite job state")


def _job_state(folder: Path) -> tuple[dict, bytes]:
    state = read_job_artifact(folder, "job.json")
    try:
        job = (json.loads(state, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
               if state is not None else None)
    except (ValueError, RecursionError):
        job = None
    return F.validate_reporting_job(job, folder.name), state


def read_reporting_job(folder: Path) -> dict:
    return _job_state(folder)[0]


def _commit(root: Path, ref: str) -> str | None:
    result = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    return result if result and _COMMIT.fullmatch(result) else None


def _git(root: Path, *args: str) -> str | None:
    # Reports must stay offline even if Git needs an object in a partial clone.
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
           "GIT_PROTOCOL_FROM_USER": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never", *args],
            cwd=root, env=env, capture_output=True, text=True, timeout=5,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return result.stdout.removesuffix("\n") if result.returncode == 0 else None


def github_repo(root: Path) -> str | None:
    remote = _git(root, "remote", "get-url", "origin")
    return public_repo(remote)


def _base_ref(cfg: dict, job: dict) -> str | None:
    if stacked := job.get("stacked_on_branch"):
        return f"refs/heads/{stacked}"
    if base := cfg.get("base_branch"):
        return f"refs/remotes/origin/{base}"
    return None


def _bindings(root: Path, cfg: dict, job: dict, spec: bytes | None) -> tuple[dict, bool]:
    branch = f"factory/{job['id']}"
    sha = _commit(root, f"refs/heads/{branch}") if job.get("branch") == branch else None
    base_ref = _base_ref(cfg, job)
    base = _commit(root, base_ref) if sha and base_ref else None
    round_number = job.get("round")
    bindings = dict(candidate_sha=sha, base_sha=base, spec_digest=_digest(spec) if spec is not None else None,
                    round=round_number if type(round_number) is int and round_number > 0 else None)
    # A local branch tip can be shown without its checkout. The installed receipt
    # verifier additionally requires the assigned, clean checkout at that tip.
    return bindings, bool(all(bindings.values()) and _clean_checkout(root, job, sha))


def _clean_checkout(root: Path, job: dict, sha: str | None) -> bool:
    worktree = root / ".worktrees" / job["id"]
    return bool(sha and job.get("worktree") == str(worktree.relative_to(root))
                and worktree.is_dir() and not worktree.is_symlink() and not worktree.parent.is_symlink()
                and _git(worktree, "rev-parse", "--show-toplevel") == str(worktree.resolve())
                and _git(worktree, "symbolic-ref", "--quiet", "HEAD") == f"refs/heads/factory/{job['id']}"
                and _commit(worktree, "HEAD") == sha
                and _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all") == "")


def _required(cfg: dict, job: dict, spec: bytes | None, number: int | None, cutoff: datetime, *,
              unique_criteria: bool = False) -> list[dict]:
    reviews = cfg.get("routing", {})
    surfaces = cfg.get("surface_reviewers", {})
    expected = set(reviews.get(job.get("risk"), {}))
    for surface in job.get("surfaces") or []:
        expected.update(surfaces.get(surface, {}))
    expected.update(job.get("reviewers") or {})
    potential = {name for route in [*reviews.values(), *surfaces.values()] for name in route}
    round_dir = f"round-{number}" if number else None
    rows = [dict(id=_CHECKS, legacy_paths=[f"{round_dir}/checks.md"] if round_dir else []),
            dict(id="approval", legacy_paths=["decision.md"])]
    merge_steps = [step for step in job.get("history", []) if step.get("stage") in ("merging", "merged")]
    if (any((when := _time(step.get("at"))) is None or when <= cutoff for step in merge_steps)
            or not merge_steps and job.get("stage") in ("merging", "merged")):
        rows.append(dict(id=_FINAL_CHECKS, label="Final gate after syncing with the base",
                         legacy_paths=["final-checks.md"]))
    for reviewer in sorted(expected | potential):
        if re.fullmatch(r"[a-zA-Z0-9_-]+", reviewer):
            rows.append(dict(id=f"review:{reviewer}", required=reviewer in expected,
                             legacy_paths=[f"{round_dir}/review-{reviewer}.md"] if round_dir else []))
    if spec is not None:
        try:
            criteria = F.section_text(spec.decode("utf-8"), "Acceptance criteria")
        except UnicodeError:
            return rows
        seen = set()
        for line in criteria.splitlines():
            match = _CRITERION.fullmatch(line)
            if match:
                position, identifier, label = match.groups()
                check_id = f"criterion:{identifier or position}"
                if check_id in seen:
                    if unique_criteria:
                        raise F.FactoryError("The spec has a duplicate acceptance criterion ID.")
                    continue
                row = dict(id=check_id, label=label)
                if unique_criteria:
                    row["criterion_identity"] = "explicit" if identifier else "position"
                rows.append(row)
                seen.add(check_id)
    return rows


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (AttributeError, ValueError, OverflowError):
        return None


def _published_gate(history, number: int, verdict: str):
    """Match the round receipt to its committed gate transition, not a file label."""
    seen = 0
    for index, step in enumerate(history[:-1]):
        if step.get("stage") == "build":
            seen += 1
        if seen != number or step.get("stage") != "gate":
            continue
        result = history[index + 1]
        note = result.get("note") or ""
        passed = verdict == "pass" and result.get("stage") == "review" and note == "gate passed"
        failed = (verdict == "fail" and result.get("stage") in {"build", "recovery"}
                  and re.fullmatch(r"gate failed(?:; \d+ rounds used)?", note))
        attempted, occurred = _time(step.get("at")), _time(result.get("at"))
        if (passed or failed) and attempted and occurred and attempted <= occurred:
            return step["at"], result["at"]
    return None


def _published_final(history: list, number: int, as_of: datetime) -> str | None:
    round_number, merging = 0, False
    for step in history:
        if step.get("stage") == "build":
            round_number, merging = round_number + 1, False
        elif round_number == number and step.get("stage") == "merging":
            merging = True
        elif (round_number == number and merging and step.get("stage") == "needs-human"
              and step.get("note") == "merge requires human verification of the PR head and base"):
            when = _time(step.get("at"))
            if when is not None and when <= as_of:
                return step["at"]
    return None


def _installed_validator():
    """The stabilization install has pure receipt/audit readers in its launcher."""
    if not hasattr(F, "GATE_PENDING") or not hasattr(F, "OPERATION"):
        return None
    launcher = Path(__file__).resolve().parent.parent / "bin" / "job"
    if not launcher.is_file() or launcher.is_symlink():
        return None
    main = sys.modules["__main__"]
    if Path(getattr(main, "__file__", "")).resolve() == launcher:
        module = main
    else:
        loader = importlib.machinery.SourceFileLoader("_factory_reporting_launcher", str(launcher))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    readers = ("_read_audit", "_validate_audit", "_review_paths", "_review_input_files",
               "_job_input_digest", "_artifact_verdict_line", "_require_gate_receipt", "_approval")
    return module if all(callable(getattr(module, name, None)) for name in readers) else None


def _pending_operation(folder: Path, number: int) -> bool:
    if any(os.path.lexists(folder / name) for name in (F.GATE_PENDING, F.OPERATION)):
        return True
    prerequisites = ("worker-intake.json", "worker-spec-writer.json", f"worker-round-{number}-builder.json")
    if not all(_worker_idle(folder, audit) for audit in prerequisites):
        return True
    try:
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries, start=1):
                if count >= 1024 or re.fullmatch(r"worker-round-\d+-builder\.json\.pending", entry.name):
                    return True
    except OSError:
        return True
    return False


def _worker_idle(folder: Path, audit: str) -> bool:
    return not any(os.path.lexists(folder / f"{audit}{suffix}") for suffix in (".pending", ".lock", ".replacing"))


_WORKER_DIR = "worker-events-v1"
_WORKER_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_WORKER_BASE = {"kind", "job_id", "origin", "round", "input_sha", "base_sha", "candidate_sha",
                "spec_digest", "policy_digest", "occurred_at"}
_WORKER_REFUSALS = {"protected_path_refused", "scope_refused"}
_WORKER_RECOVERY_REFUSALS = {"recovery_" + name for name in _WORKER_REFUSALS}


def _local_worker_event(event, name, job):
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        return None
    kind = event["kind"]
    recovered = kind == "builder_publication_recovered"
    recovery_refused = kind in _WORKER_RECOVERY_REFUSALS
    if not (recovered or recovery_refused or kind in _WORKER_REFUSALS):
        return None
    digests = {"audit_digest", "artifact_digest"} if recovered else set()
    position = {"worktree_position"} if recovery_refused else set()
    if set(event) != _WORKER_BASE | digests | position:
        return None
    if recovery_refused and event["worktree_position"] not in ("input", "candidate"):
        return None
    origin = job.get("origin") if job.get("origin") in ("controlled", "real") else "unknown"
    number, stamp = event["round"], event["occurred_at"]
    if (event["job_id"] != job["id"] or event["origin"] != origin
            or type(number) is not int or not 0 < number <= (job.get("round") or 0)
            or not isinstance(stamp, str) or not _WORKER_TIME.fullmatch(stamp) or _time(stamp) is None
            or not all(isinstance(event[k], str) and _COMMIT.fullmatch(event[k])
                       for k in ("input_sha", "base_sha", "candidate_sha"))
            or event["input_sha"] == event["candidate_sha"]
            or not all(isinstance(event[k], str) and re.fullmatch(r"[0-9a-f]{64}", event[k])
                       for k in {"spec_digest", "policy_digest"} | digests)):
        return None
    identity = [_WORKER_DIR, kind, job["id"], origin, str(number), event["input_sha"],
                event["candidate_sha"], event["base_sha"], event["spec_digest"]]
    identity += ([event["audit_digest"], event["artifact_digest"]] if recovered
                 else [event["policy_digest"], event.get("worktree_position", "")])
    if name != _digest("\0".join(identity).encode()) + ".json":
        return None
    return {**event, "verification": "unverified"}


def read_worker_receipts(folder: Path, job: dict, *, as_of: datetime | None = None) -> tuple[list, list]:
    """Read a bounded selection. Same-UID worker files have no authenticated issuer."""
    cutoff = as_of or datetime.now(timezone.utc)
    diagnostics, rows = set(), []
    invalid = "Some worker receipts are unreadable or invalid."
    try:
        with _open_job(folder) as job_fd:
            try:
                fd = os.open(_WORKER_DIR, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY, dir_fd=job_fd)
            except FileNotFoundError:
                return [], []
            try:
                with os.scandir(fd) as entries:
                    count = 0
                    for entry in entries:
                        if entry.name == ".staging":
                            continue
                        if count >= 1024:
                            diagnostics.add("The worker receipt inventory exceeds the 1024-file reporting limit.")
                            break
                        count += 1
                        if not re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                            diagnostics.add(invalid)
                            continue
                        raw = read_job_artifact(folder, f"{_WORKER_DIR}/{entry.name}/receipt.json", max_bytes=4096)
                        try:
                            body = json.loads(raw, object_pairs_hook=_unique_fields) if raw else None
                        except (ValueError, RecursionError):
                            body = None
                        event = _local_worker_event(body, entry.name, job)
                        if event is None:
                            diagnostics.add(invalid)
                            continue
                        if _time(event["occurred_at"]) > cutoff:
                            continue
                        if (event["kind"] == "builder_publication_recovered"
                                and not _worker_idle(folder, f"worker-round-{event['round']}-builder.json")):
                            continue
                        rows.append(event)
            finally:
                os.close(fd)
    except OSError:
        diagnostics.add(invalid)
    return sorted(rows, key=lambda row: (row["occurred_at"], row["candidate_sha"], row["kind"])), sorted(diagnostics)


def _receipt_event(job: dict, bindings: dict, check: str, verdict: str, when: str, *references: tuple[str, str]) -> dict:
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([job["id"], bindings, check, references], sort_keys=True))
    return {"schema_version": 1, "event_id": str(identifier), "job_id": job["id"], "kind": "check_evidence",
            "origin": "real", "occurred_at": when, "recorded_at": when,
            "source": {"kind": "reviewer" if check.startswith("review:") else "controller",
                       "record_id": references[0][0]},
            "candidate_sha": bindings["candidate_sha"], "round": bindings["round"],
            "evidence": [{"path": name, "sha256": digest} for name, digest in references],
            "data": dict(check_id=check, verdict=verdict, base_sha=bindings["base_sha"],
                         spec_digest=bindings["spec_digest"])}


def _final_receipt_event(verifier, folder: Path, job: dict, bindings: dict, as_of: datetime) -> dict | None:
    when = _published_final(job.get("history") or [], bindings["round"], as_of)
    if not when:
        return None
    path = "final-checks.md"
    content = read_job_artifact(folder, path)
    actual = _FINAL_RECEIPT.match(content or b"")
    if actual is None:
        return None
    captured = {**bindings, **{field: value.decode("ascii") for field, value in actual.groupdict().items()}}
    if captured == bindings:
        try:
            verifier._require_gate_receipt(folder, path, job, bindings["candidate_sha"], bindings["base_sha"],
                                           bindings["spec_digest"], data=content)
        except F.FactoryError:
            return None
    return _receipt_event(job, captured, _FINAL_CHECKS, "pass", when, (path, _digest(content)))


def _receipt_rows(verifier, folder: Path, job: dict, bindings: dict, as_of: datetime):
    number, head, base, digest = (bindings[k] for k in ("round", "candidate_sha", "base_sha", "spec_digest"))
    if _pending_operation(folder, number):
        return [], None
    final = _final_receipt_event(verifier, folder, job, bindings, as_of)
    rows = [final] if final is not None else []
    path = f"round-{number}/checks.md"
    checks = read_job_artifact(folder, path)
    if checks is None:
        return rows, None
    for verdict in ("pass", "fail"):
        prefix = f"GATE: {verdict.upper()}\nCANDIDATE: {head}\nBASE: {base}\nSPEC-DIGEST: {digest}\n\n".encode("ascii")
        if checks.startswith(prefix):
            break
    else:
        return rows, None
    timing = _published_gate(job.get("history") or [], number, verdict)
    if not timing or _time(timing[1]) > as_of:
        return rows, None
    rows.append(_receipt_event(job, bindings, _CHECKS, verdict, timing[1], (path, _digest(checks))))
    first = dict(attempted_at=timing[0], status=verdict, verified=True) if number == 1 else None
    if verdict == "fail" or not job.get("reviewers"):
        return rows, first
    try:
        reviewers = verifier._review_paths(job)
        files = verifier._review_input_files(folder, path)
        # Check immutable candidate inputs and the exact gate bytes read above.
        if files[path] != checks or _digest(files["spec.md"]) != digest:
            return [], None
        verifier._require_gate_receipt(folder, path, job, head, base, digest, data=files[path])
        inputs = {name: _digest(data) for name, data in files.items()}
        inputs["job.json"] = verifier._job_input_digest(job)
    except (F.FactoryError, KeyError, ValueError):
        return rows, first
    for name, (artifact, audit) in reviewers.items():
        if not _worker_idle(folder, audit):
            continue
        try:
            record, audit_digest, _ = verifier._read_audit(folder, audit, f"reviewer-{name}", head)
            when = _time(record.get("at"))
            if when is None or when < _time(timing[1]) or when > as_of:
                continue
            body, artifact_digest, _ = verifier._validate_audit(folder, audit, record, artifact, inputs, base)
        except F.FactoryError:
            continue
        line = verifier._artifact_verdict_line(body)
        if line in ("VERDICT: PASS", "VERDICT: CHANGES") and _worker_idle(folder, audit):
            rows.append(_receipt_event(job, bindings, f"review:{name}", "pass" if line.endswith("PASS") else "fail",
                                       record["at"], (artifact, artifact_digest), (audit, audit_digest)))
    audit = f"worker-round-{number}-approver.json"
    if _worker_idle(folder, audit) and all(_worker_idle(folder, path) for _, path in reviewers.values()):
        try:
            record, audit_digest, _ = verifier._read_audit(folder, audit, "approver", head)
            when = _time(record.get("at"))
            if when is not None and _time(timing[1]) <= when <= as_of:
                body, receipt, approval_spec = verifier._approval(folder, job, reviewers, audit, head, base)
                line = verifier._artifact_verdict_line(body)
                if (line in ("APPROVE", "ESCALATE") and approval_spec == digest and audit_digest == receipt["audit_sha256"]
                        and _worker_idle(folder, audit) and all(_worker_idle(folder, p) for _, p in reviewers.values())):
                    rows.append(_receipt_event(job, bindings, "approval", "pass" if line == "APPROVE" else "fail",
                                               record["at"], ("decision.md", receipt["artifact_sha256"]), (audit, audit_digest)))
        except F.FactoryError:
            pass
    return rows, first


def collect_local_guardrails(root, cfg, folder, job, state, spec, events, diagnostics, candidate,
                             clean, required, cutoff):
    """Evaluate an as-of snapshot with the existing offline independent receipt adapter."""
    receipt_events, first_gate = [], None
    verifier = _installed_validator() if clean else None
    unstable = bindings_unstable = False
    if verifier is not None:
        snapshot = _receipt_rows(verifier, folder, job, candidate, cutoff)
        repeated = _receipt_rows(verifier, folder, job, candidate, cutoff)
        final_candidate, final_clean = _bindings(root, cfg, job, spec)
        bindings_unstable = (candidate != final_candidate or state != read_job_artifact(folder, "job.json")
                             or spec != read_job_artifact(folder, "spec.md"))
        unstable = bindings_unstable or snapshot != repeated or clean != final_clean
        if not unstable:
            receipt_events, first_gate = repeated
    verified = {event["event_id"]: event for event in receipt_events
                if event["candidate_sha"] == candidate["candidate_sha"]
                and event["data"]["base_sha"] == candidate["base_sha"]
                and event["data"]["spec_digest"] == candidate["spec_digest"]}

    def verify(event, ref, content):
        independent = verified.get(event["event_id"])
        if independent != event or ref not in independent["evidence"] or content is None:
            return False
        return (not _pending_operation(folder, candidate["round"])
                and all(_worker_idle(folder, item["path"]) for item in independent["evidence"]
                        if item["path"].endswith(".json"))
                and _digest(content) == ref["sha256"])

    guardrails = evaluate_guardrails(folder, job, [*events, *receipt_events], required,
                                     {**candidate, "as_of": cutoff, "diagnostics": diagnostics, "unstable": unstable,
                                      "bindings_unstable": bindings_unstable, "verify_evidence": verify})
    from retired_evidence import reporting_history
    retirement = reporting_history(root, folder, job, cutoff)
    if retirement is not None:
        guardrails["retirement"] = retirement
    if first_gate and not any(row["id"] == _CHECKS and row["status"] == first_gate["status"]
                              for row in guardrails["requirements"]):
        first_gate = None
    return guardrails, first_gate


def reporting_record(root: Path, cfg: dict, folder: Path, *, as_of: datetime | None = None,
                     verify_github_links=False) -> dict:
    """Collect as-of observations and read local technical evidence independently."""
    cutoff = as_of or datetime.now(timezone.utc)
    job, state = _job_state(folder)
    events, diagnostics = load_events(folder, as_of=cutoff, include_ancestry=True)
    if job.get("origin") != "controlled":
        events = [event for event in events if event["origin"] == "real"]
    spec = read_job_artifact(folder, "spec.md")
    candidate, clean = _bindings(root, cfg, job, spec)
    required = _required(cfg, job, spec, candidate["round"], cutoff)
    guardrails, first_gate = collect_local_guardrails(root, cfg, folder, job, state, spec, events, diagnostics,
                                                     candidate, clean, required, cutoff)
    source, commit_link = None, None
    if verify_github_links and job.get("origin") != "controlled":
        source, commit_link = verified_links(root, job, candidate["candidate_sha"], github_repo)
        if source and state != read_job_artifact(folder, "job.json"):
            source, commit_link = None, None
        elif commit_link and _commit(root, f"refs/heads/factory/{job['id']}") != candidate["candidate_sha"]:
            commit_link = None
    job = {**job, "source_url": source, "candidate_url": commit_link}
    worker_receipts, worker_diagnostics = read_worker_receipts(folder, job, as_of=cutoff)
    return {"job": job, "events": events, "worker_receipts": worker_receipts,
            "worker_receipt_diagnostics": worker_diagnostics, "guardrails": guardrails, "diagnostics": guardrails["diagnostics"],
            "observation_diagnostics": diagnostics, "url": source, "candidate_url": commit_link,
            "first_gate": first_gate}
