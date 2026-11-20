"""Observe bounded gate-header prefixes for the read-only retirement explanation."""
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

import factory as F
from guardrails import _MAX_BYTES
from observations import _open_directory, _open_job


# The largest supported header (three 64-character digests) fits in 256 bytes.
_PREFIX_BYTES = 256
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_HEADER = re.compile(
    rb"GATE: (?:PASS|FAIL)\nCANDIDATE: (?P<head>[0-9a-f]{40}|[0-9a-f]{64})\n"
    rb"BASE: (?:[0-9a-f]{40}|[0-9a-f]{64})\nSPEC-DIGEST: [0-9a-f]{64}\n\n"
)
_OBSERVED = {"candidate_match", "candidate_mismatch", "candidate_unknown"}
_UNRESOLVED = {"candidate_unknown", "malformed", "oversized", "unavailable", "round_unknown"}


def _classify(prefix, candidate_sha):
    header = _HEADER.match(prefix)
    if header:
        if not isinstance(candidate_sha, str) or not _SHA.fullmatch(candidate_sha):
            return "candidate_unknown"
        return "candidate_match" if header["head"].decode("ascii") == candidate_sha else "candidate_mismatch"
    if prefix.startswith(b"GATE:") and any(key in prefix for key in (b"\nCANDIDATE:", b"\nBASE:", b"\nSPEC-DIGEST:")):
        return "malformed"
    return "non_native"


def _unchanged(left, right):
    return (os.path.samestat(left, right) and left.st_ctime_ns == right.st_ctime_ns
            and left.st_size == right.st_size and left.st_nlink == right.st_nlink)


def _observe_slot(job_fd, parent, name, candidate_sha):
    located = False
    try:
        with ExitStack() as opened:
            directory = opened.enter_context(_open_directory(parent, job_fd)) if parent else job_fd
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            located = True
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return "unavailable"
            if before.st_size > _MAX_BYTES:
                return "oversized"
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            opened.callback(os.close, fd)
            if not _unchanged(before, os.fstat(fd)):
                return "unavailable"
            length = min(before.st_size, _PREFIX_BYTES)
            prefix = os.read(fd, length)
            if (len(prefix) != length or not _unchanged(before, os.fstat(fd))
                    or not _unchanged(before, os.stat(name, dir_fd=directory, follow_symlinks=False))):
                return "unavailable"
            if parent and not os.path.samestat(os.fstat(directory), os.stat(parent, dir_fd=job_fd, follow_symlinks=False)):
                return "unavailable"
            return _classify(prefix, candidate_sha)
    except FileNotFoundError:
        return "unavailable" if located else "missing"
    except OSError:
        return "unavailable"


def observe_evidence(folder, job, candidate_sha):
    """Count visible native-shaped receipts; base, spec and publication are unverified."""
    folder = Path(folder)
    slots = {"round_gate": "unavailable", "final_gate": "unavailable"}
    if isinstance(job, dict) and folder.name == job.get("id") and F.valid_reporting_job_id(folder.name):
        try:
            with _open_job(folder) as directory:
                number = job.get("round")
                round_status = (_observe_slot(directory, f"round-{number}", "checks.md", candidate_sha)
                                if type(number) is int and 0 < number <= 999999 else "round_unknown")
                final_status = _observe_slot(directory, None, "final-checks.md", candidate_sha)
                with _open_job(folder) as current:
                    if os.path.samestat(os.fstat(directory), os.fstat(current)):
                        slots = {"round_gate": round_status, "final_gate": final_status}
        except (OSError, ValueError):
            pass
    return {
        "native_headers": sum(status in _OBSERVED for status in slots.values()),
        "candidate_matched_headers": sum(status == "candidate_match" for status in slots.values()),
        "candidate_mismatched_headers": sum(status == "candidate_mismatch" for status in slots.values()),
        "unresolved_slots": sum(status in _UNRESOLVED for status in slots.values()),
        "confirmed_retention_receipts": None,
        "confidence": "header_observation_only",
        "retention_assessment": "full_verification_required",
        "slots": slots,
    }
