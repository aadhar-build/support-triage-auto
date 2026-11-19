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
from guardrails import evaluate_guardrails, read_job_artifact
from observations import load_events


_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CRITERION = re.compile(r"\s*(\d{1,6})\.\s+(?:\[([a-zA-Z0-9_.-]{1,48})\]\s+)?(.+)")
_CHECKS = "checks"
_FINAL_CHECKS = "final_checks"
_FINAL_RECEIPT = re.compile(
    rb"GATE: PASS\nCANDIDATE: (?P<candidate_sha>[0-9a-f]{40}|[0-9a-f]{64})\n"
    rb"BASE: (?P<base_sha>[0-9a-f]{40}|[0-9a-f]{64})\nSPEC-DIGEST: (?P<spec_digest>[0-9a-f]{64})\n\n"
)
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+?)(?:\.git)?/?"
)


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
    return result.stdout.strip() if result.returncode == 0 else None


def github_repo(root: Path) -> str | None:
    remote = _git(root, "remote", "get-url", "origin")
    match = _GITHUB_REMOTE.fullmatch(remote or "")
    return match[1] if match else None


def _bindings(root: Path, cfg: dict, job: dict, spec: bytes | None) -> tuple[dict, bool]:
    branch = f"factory/{job['id']}"
    sha = _commit(root, f"refs/heads/{branch}") if job.get("branch") == branch else None
    stacked = job.get("stacked_on_branch")
    base_branch = cfg.get("base_branch")
    base_ref = f"refs/heads/{stacked}" if stacked else f"refs/remotes/origin/{base_branch}"
    base = _commit(root, base_ref) if sha and (stacked or base_branch) else None
    round_number = job.get("round")
    bindings = dict(candidate_sha=sha, base_sha=base, spec_digest=_digest(spec) if spec is not None else None,
                    round=round_number if type(round_number) is int and round_number > 0 else None)
    # A local branch tip can be shown without its checkout. The installed receipt
    # verifier additionally requires the assigned, clean checkout at that tip.
    worktree = root / ".worktrees" / job["id"]
    clean = (all(bindings.values()) and job.get("worktree") == str(worktree.relative_to(root))
             and worktree.is_dir() and not worktree.is_symlink() and not worktree.parent.is_symlink()
             and _git(worktree, "rev-parse", "--show-toplevel") == str(worktree.resolve())
             and _git(worktree, "symbolic-ref", "--quiet", "HEAD") == f"refs/heads/{branch}"
             and _commit(worktree, "HEAD") == sha
             and _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all") == "")
    return bindings, bool(clean)


def _required(cfg: dict, job: dict, spec: bytes | None, number: int | None, cutoff: datetime) -> list[dict]:
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
                if check_id not in seen:
                    rows.append(dict(id=check_id, label=label))
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


def reporting_record(root: Path, cfg: dict, folder: Path, *, as_of: datetime | None = None) -> dict:
    """Collect as-of observations and read local technical evidence independently."""
    cutoff = as_of or datetime.now(timezone.utc)
    job, state = _job_state(folder)
    events, diagnostics = load_events(folder, as_of=cutoff, include_ancestry=True)
    if job.get("origin") != "controlled":
        events = [event for event in events if event["origin"] == "real"]
    spec = read_job_artifact(folder, "spec.md")
    candidate, clean = _bindings(root, cfg, job, spec)
    required = _required(cfg, job, spec, candidate["round"], cutoff)
    receipt_events, first_gate = [], None
    verifier = _installed_validator() if clean else None
    unstable = False
    if verifier is not None:
        snapshot = _receipt_rows(verifier, folder, job, candidate, cutoff)
        repeated = _receipt_rows(verifier, folder, job, candidate, cutoff)
        unstable = (snapshot != repeated or state != read_job_artifact(folder, "job.json")
                    or spec != read_job_artifact(folder, "spec.md")
                    or (candidate, clean) != _bindings(root, cfg, job, spec))
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
                                      "verify_evidence": verify})
    if first_gate and not any(row["id"] == _CHECKS and row["status"] == first_gate["status"]
                              for row in guardrails["requirements"]):
        first_gate = None
    artifact_root = folder.relative_to(root).as_posix()
    job = {**job, "artifact_root": artifact_root}
    slug = github_repo(root)
    url = f"{artifact_root}/job.json"
    if slug:
        repo = f"https://github.com/{slug}"
        if type(job.get("pr")) is int and job["pr"] > 0:
            url = f"{repo}/pull/{job['pr']}"
        elif type(job.get("issue")) is int and job["issue"] > 0:
            url = f"{repo}/issues/{job['issue']}"
        if candidate["candidate_sha"]:
            job["candidate_url"] = f"{repo}/commit/{candidate['candidate_sha']}"
    return {"job": job, "events": events, "guardrails": guardrails, "diagnostics": guardrails["diagnostics"],
            "url": url, "first_gate": first_gate}
