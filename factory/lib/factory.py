"""Shared logic for the factory scripts.

Everything deterministic lives here: job state, legal stage moves, parsing
agent files, and the path rules (protected paths, scope, auto-merge
allowlist). Standard library only, so it runs anywhere Python 3.10+ does.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import re
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# Stages and the only moves allowed between them.
# --------------------------------------------------------------------------

TRANSITIONS: dict[str, set[str]] = {
    "intake": {"spec", "needs-info", "rejected"},
    "needs-info": {"intake", "rejected"},
    "spec": {"build", "needs-human"},
    "build": {"gate", "needs-human"},
    "gate": {"review", "build", "recovery"},
    "review": {"approve", "build", "recovery"},
    "recovery": {"spec", "build", "needs-human", "rejected"},
    "approve": {"merging", "needs-human"},
    "merging": {"merged", "needs-human", "build"},
    "needs-human": {"build", "merged", "rejected"},
    "merged": set(),
    "rejected": set(),
}
TERMINAL = {"merged", "rejected"}


class FactoryError(Exception):
    """A refused action. The message is shown to the orchestrator as is."""


# --------------------------------------------------------------------------
# Paths and config
# --------------------------------------------------------------------------


def repo_root(start: Path | None = None) -> Path:
    """The main checkout's root (never a worktree's)."""
    cwd = str(start or Path.cwd())
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=cwd, capture_output=True, text=True,
    )
    if common.returncode != 0:
        raise FactoryError("not inside a git repository")
    return Path(common.stdout.strip()).parent


def load_config(root: Path) -> dict:
    path = root / "factory" / "config.json"
    if not path.exists():
        raise FactoryError(f"missing {path}")
    return json.loads(path.read_text())


def jobs_dir(root: Path) -> Path:
    return root / "factory" / "jobs"


def job_dir(root: Path, job_id: str) -> Path:
    d = jobs_dir(root) / job_id
    if not (d / "job.json").exists():
        raise FactoryError(f"no job {job_id}")
    return d


def slugify(text: str, words: int = 4) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(parts[:words]) or "job"


def make_job_id(issue: int, title: str) -> str:
    return f"{issue:03d}-{slugify(title)}"


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Job state. job.json is written only through these functions.
# --------------------------------------------------------------------------


def read_job(d: Path) -> dict:
    return json.loads((d / "job.json").read_text())


def write_job(d: Path, job: dict) -> None:
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    tmp.replace(d / "job.json")


def new_job(job_id: str, issue: int, title: str) -> dict:
    return {
        "id": job_id,
        "issue": issue,
        "title": title,
        "stage": "intake",
        "round": 0,
        "risk": None,
        "surfaces": [],
        "reviewers": {},
        "branch": f"factory/{job_id}",
        "worktree": f".worktrees/{job_id}",
        "pr": None,
        "history": [{"at": now(), "stage": "intake", "note": "created"}],
    }


def move(job: dict, to: str, note: str = "") -> dict:
    """Move a job to a new stage, refusing illegal moves."""
    frm = job["stage"]
    if to not in TRANSITIONS.get(frm, set()):
        raise FactoryError(f"illegal move {frm} -> {to} for job {job['id']}")
    job["stage"] = to
    job["history"].append({"at": now(), "stage": to, "note": note})
    return job


def next_round_or_recovery(job: dict, max_rounds: int, note: str) -> dict:
    """After a failed gate or a CHANGES review: another build round, or recovery."""
    if job["round"] < max_rounds:
        job["round"] += 1
        return move(job, "build", note)
    return move(job, "recovery", note + f"; {max_rounds} rounds used")


# --------------------------------------------------------------------------
# Parsing agent files
# --------------------------------------------------------------------------

HEADER_RE = re.compile(r"^([A-Z][A-Z-]*):\s*(.*?)\s*$")


def parse_header(text: str) -> dict[str, str]:
    """Read the KEY: value lines at the top of an agent file."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if out:
                break
            continue
        m = HEADER_RE.match(line)
        if not m:
            break
        out[m.group(1)] = m.group(2)
    return out


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def body_after_header(text: str) -> str:
    """Everything after the header block, for posting as a comment."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and (HEADER_RE.match(lines[i]) or lines[i].strip() in {"APPROVE", "ESCALATE"}):
        i += 1
    return "\n".join(lines[i:]).strip()


def parse_surfaces(value: str) -> list[str]:
    items = [s.strip().lower() for s in value.split(",")]
    return [s for s in items if s and s != "none"]


def section(text: str, heading: str) -> str:
    """The body of a '## heading' section."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def allowed_files(spec_text: str) -> list[str]:
    """Paths or globs listed under '## Files' in spec.md, one per bullet."""
    out = []
    for line in section(spec_text, "Files").splitlines():
        m = re.match(r"^\s*[-*]\s+`?([^`\s]+)`?", line)
        if m:
            out.append(m.group(1))
    return out


# --------------------------------------------------------------------------
# Path rules
# --------------------------------------------------------------------------


def matches(path: str, patterns: list[str]) -> bool:
    """fnmatch, where '*' also crosses folders, plus 'dir/' meaning everything under dir."""
    for p in patterns:
        if p.endswith("/") and path.startswith(p):
            return True
        if fnmatch.fnmatchcase(path, p):
            return True
    return False


def protected_violations(changed: list[str], protected: list[str]) -> list[str]:
    return [p for p in changed if matches(p, protected)]


def scope_violations(changed: list[str], allowed: list[str]) -> list[str]:
    return [p for p in changed if not matches(p, allowed)]


def automerge_ok(changed: list[str], allowlist: list[str], risk: str | None, surfaces: list[str]) -> tuple[bool, str]:
    """Blast radius, not confidence: low risk, no sensitive surface, every file on the allowlist."""
    if risk != "low":
        return False, f"risk is {risk}, only low-risk jobs can merge without a human"
    sensitive = [s for s in surfaces if s in {"security", "eval"}]
    if sensitive:
        return False, f"touches the {', '.join(sensitive)} surface"
    outside = [p for p in changed if not matches(p, allowlist)]
    if outside:
        return False, "files outside the auto-merge allowlist: " + ", ".join(outside[:10])
    if not changed:
        return False, "no changed files"
    return True, "every changed file is on the allowlist"


# --------------------------------------------------------------------------
# Small helpers around git and gh
# --------------------------------------------------------------------------


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, stdin: str | None = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, input=stdin)
    if check and res.returncode != 0:
        raise FactoryError(f"{' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[:500]}")
    return res.stdout


def changed_files(worktree: Path, base: str) -> list[str]:
    run(["git", "fetch", "--quiet", "origin", base], cwd=worktree, check=False)
    out = run(["git", "diff", "--name-only", f"origin/{base}...HEAD"], cwd=worktree)
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# GitHub, through the REST API only (`gh api`). High-level `gh issue`/`gh pr`
# commands use GraphQL, which some sandboxes (including Claude Code cloud
# sessions) block, so the factory never depends on them.
# --------------------------------------------------------------------------


def repo_slug(root: Path) -> str:
    url = run(["git", "remote", "get-url", "origin"], cwd=root).strip()
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise FactoryError(f"origin is not a GitHub repo: {url}")
    return m.group(1)


def gh_api(root: Path, method: str, path: str, fields: dict | None = None, check: bool = True):
    """Call the GitHub REST API for this repo. `path` is relative to repos/<owner>/<repo>."""
    cmd = ["gh", "api", "-X", method, f"repos/{repo_slug(root)}/{path}" if path else f"repos/{repo_slug(root)}"]
    for key, value in (fields or {}).items():
        if isinstance(value, list):
            for v in value:
                cmd += ["-f", f"{key}[]={v}"]
        elif isinstance(value, bool):
            cmd += ["-F", f"{key}={'true' if value else 'false'}"]
        else:
            cmd += ["-f", f"{key}={value}"]
    out = run(cmd, check=check)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        return None


def gh_user() -> str:
    return run(["gh", "api", "user", "--jq", ".login"], check=False).strip()
