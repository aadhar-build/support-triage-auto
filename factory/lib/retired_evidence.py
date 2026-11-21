"""Evidence retention for merged jobs whose linked checkout may be removed.

The journal and worker artifacts have the same local trust boundary. The adapter
can describe historical local checks, but cannot use them as an issuer for a
current guardrail verdict. Retain a checkout when its native receipts need it.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

import factory as F
import evidence_adapter as adapter
import retirement_git
from guardrails import read_job_artifact
from observations import _check_local_reference, _open_directory, _open_job


def _job_state(root, folder, job):
    if (not F.valid_reporting_job_id(folder.name) or folder != F.jobs_dir(root) / folder.name
            or job.get("stage") != "merged" or job.get("branch") != f"factory/{folder.name}"
            or job.get("worktree") != f".worktrees/{folder.name}"):
        raise F.FactoryError("unexpected retirement job or checkout assignment")
    stored, raw = adapter._job_state(folder)
    if stored != job:
        raise F.FactoryError("retirement job changed")
    return raw


def _snapshot(folder, job, state, candidate):
    paths = {"issue.md", "intake.md", "spec.md", "build.md", "final-checks.md", "decision.md"}
    number = job.get("round")
    if number:
        paths.update({f"round-{number}/checks.md", f"worker-round-{number}-approver.json"})
        for name in job.get("reviewers") or {}:
            if name not in {"code", "eval", "security"}:
                raise F.FactoryError("unsupported retirement reviewer")
            paths.update({f"round-{number}/review-{name}.md", f"round-{number}/worker-reviewer-{name}.json"})

    artifacts = {}
    with _open_job(folder) as directory:
        job_info = os.fstat(directory)
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count >= 1024:
                    raise F.FactoryError("retirement evidence inventory exceeds the reporting limit")
                if entry.name.startswith("feedback-") and entry.name.endswith(".md"):
                    paths.add(entry.name)
        if len(paths) > 128:
            raise F.FactoryError("retirement review inputs exceed the reporting limit")
        for name in sorted(paths):
            try:
                _check_local_reference(directory, name)
            except ValueError as error:
                raise F.FactoryError("unsafe retirement evidence file") from error
            data = read_job_artifact(folder, name)
            if data is None and os.path.lexists(folder / name):
                raise F.FactoryError("unreadable retirement evidence file")
            if data is not None:
                artifacts[name] = adapter._digest(data)
    return {"schema_version": 1, "job_id": job["id"], "job_sha256": adapter._digest(state),
            "job_directory": [job_info.st_dev, job_info.st_ino], "candidate_sha": candidate,
            "artifacts": artifacts, "verified_native_receipts": []}


def capture(root, folder, job):
    """Return deterministic historical evidence, or refuse loss of live receipt coverage."""
    state = _job_state(root, folder, job)
    spec = read_job_artifact(folder, "spec.md")
    config = F.load_config(root)
    bindings, clean = adapter._bindings(root, config, job, spec)
    head = bindings["candidate_sha"]
    # The reporting reader respects some ambient Git settings. A redirected base
    # must never hide a gate that the controller's actual repository can verify.
    branch_ref = "refs/heads/" + job["branch"]
    base_ref = adapter._base_ref(config, job)
    names = [branch_ref, base_ref] if base_ref else [branch_ref]
    output = retirement_git._git(root, "for-each-ref", "--format=%(refname) %(objectname)", *names)
    refs = dict(line.split(b" ", 1) for line in output.splitlines())
    base = refs.get(os.fsencode(base_ref)) if base_ref else None
    if (not head or refs.get(os.fsencode(branch_ref)) != head.encode("ascii")
            or (base.decode("ascii") if base else None) != bindings["base_sha"]):
        raise F.FactoryError("retirement evidence bindings differ from the controller's Git refs")
    if not clean and (all(bindings.values()) or not adapter._clean_checkout(root, job, head)):
        raise F.FactoryError("retirement requires the assigned clean checkout")
    if adapter._pending_operation(folder, job.get("round")):
        raise F.FactoryError("retirement evidence publication is pending")
    verifier = adapter._installed_validator() if clean else None
    if verifier is not None:
        # An explicit as-of report can include a clock-skewed, future-dated receipt.
        rows, first_gate = adapter._receipt_rows(verifier, folder, job, bindings, datetime.max.replace(tzinfo=timezone.utc))
        if rows or first_gate:
            raise F.FactoryError("retain checkout to preserve local technical receipt evidence")
    else:
        # Missing candidate inputs or an unavailable validator cannot establish
        # that a structured native receipt had zero coverage.
        paths = ["final-checks.md"]
        if job.get("round"):
            paths.append(f"round-{job['round']}/checks.md")
        for path in paths:
            data = read_job_artifact(folder, path)
            if data and data.startswith((b"GATE: PASS\nCANDIDATE: ", b"GATE: FAIL\nCANDIDATE: ")):
                raise F.FactoryError("retain checkout until the native receipt verifier and bindings are available")
    return _snapshot(folder, job, state, head)


def matches_snapshot(root, folder, job, evidence):
    """Revalidate the retained branch, the job inode and all recorded evidence."""
    try:
        state = _job_state(root, folder, job)
        head = retirement_git._commit(root, "refs/heads/" + job["branch"])
        if head is None or adapter._pending_operation(folder, job.get("round")) or not isinstance(evidence, dict):
            return False
        identity = evidence.get("job_directory")
        return (type(evidence.get("schema_version")) is int and isinstance(identity, list)
                and all(type(number) is int for number in identity)
                and evidence == _snapshot(folder, job, state, head))
    except (OSError, ValueError, F.FactoryError):
        return False


def valid_detached(root, folder, job, evidence):
    """Validate retained bytes and branch with no checkout or branch registration."""
    try:
        if not matches_snapshot(root, folder, job, evidence):
            return False
        wt = root / job["worktree"]
        with _open_directory(wt.parent) as directory:
            try:
                os.stat(wt.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return False
        listing = retirement_git._git(root, "worktree", "list", "--porcelain", "-z")
        if not listing or not listing.endswith(b"\0\0"):
            return False
        fields = listing.split(b"\0")
        return os.fsencode(f"worktree {wt}") not in fields and os.fsencode(f"branch refs/heads/{job['branch']}") not in fields
    except (OSError, ValueError, F.FactoryError):
        return False


def reporting_history(root, folder, job, cutoff):
    """Return historical local metadata separately from current technical evidence."""
    if not os.path.lexists(root / ".git/factory-retirements" / (folder.name + ".json")):
        return None
    import worktree_retirement as lifecycle

    try:
        record = lifecycle.read_retirement(root, folder.name)
        if record is None:
            return None
        prepared = adapter._time(record.get("prepared_at"))
        retired = adapter._time(record.get("retired_at"))
        if prepared is not None and prepared > cutoff:
            return None
        if record["status"] == "retired" and prepared and retired and prepared <= retired:
            if retired > cutoff:
                return None
            lifecycle._detached(root, job, record)
            if lifecycle.read_retirement(root, folder.name) == record:
                return {"status": "retired", "verification": "local_archive", "candidate_sha": record["candidate_sha"],
                        "retired_at": record["retired_at"], "verified_native_receipts": 0}
    except (F.FactoryError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass
    return {"status": "unverified"}
