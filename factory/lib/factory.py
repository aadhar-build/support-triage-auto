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


# --------------------------------------------------------------------------
# Text the factory posts on GitHub (after tstack's file-pr and
# leaving-pr-comments skills: problem first, one attribution note)
# --------------------------------------------------------------------------

AGENT_NAME = "Claude Code (software factory)"
_CRITERION_RE = re.compile(r"^\s*\d+\.\s")


def criteria_met(build_text: str) -> tuple[int, int]:
    """(met, total) from build.md's numbered acceptance-criteria lines."""
    lines = [ln for ln in section(build_text, "Acceptance criteria").splitlines() if _CRITERION_RE.match(ln)]
    met = sum(1 for ln in lines if re.search(r"\bmet\b", ln) and not re.search(r"\bnot met\b", ln))
    return met, len(lines)


def pr_body(job: dict, spec_text: str, build_text: str, base_branch: str = "main") -> str:
    """The PR description: the problem, then the fix, then how it was proved."""
    parts = []
    problem = section(spec_text, "Problem statement")
    if problem:
        parts.append(problem)
    fix = section(spec_text, "Summary")
    if fix:
        parts.append(fix)
    met, total = criteria_met(build_text)
    if total:
        proof = (f"**How it was proved:** the builder reports {met} of {total} acceptance criteria met, "
                 "with the test or command for each in `build.md`. The gate and reviews run next.")
    else:
        proof = "**How it was proved:** no build notes yet. The gate and reviews run next."
    parts.append(proof)
    parts.append(f"Closes #{job['issue']}")
    if job.get("stacked_on_pr"):
        parts.append(f"**Stacked on #{job['stacked_on_pr']}**, so this PR shows only its own changes. "
                     f"Merge #{job['stacked_on_pr']} first; GitHub then retargets this PR to `{base_branch}` "
                     "when that branch is deleted.")
    parts.append(f"Created by the software factory in Claude Code, job `{job['id']}`. The spec, build notes, "
                 "gate results and reviews are in the job folder.")
    return "\n\n".join(parts)


def agent_comment(body: str, owner: str) -> str:
    """A comment posted for the owner, opened with one attribution note."""
    return f"> [!NOTE]\n> 🤖 **{AGENT_NAME} responding on behalf of {owner}**\n\n{body.strip()}"


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


def fetch_base(cwd: Path, base: str) -> None:
    """Fetch the base into refs/remotes/origin/<base> with an explicit refspec.

    A bare `git fetch origin <base>` only updates FETCH_HEAD when the clone has
    no fetch refspec (clones of empty repos, some shallow or CI clones), which
    leaves origin/<base> missing or stale.
    """
    run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], cwd=cwd)


def start_ref(job: dict, base: str) -> str:
    """What the job's own changes are measured against: the base, or the job it is stacked on."""
    return job.get("stacked_on_branch") or f"origin/{base}"


def changed_files(worktree: Path, base: str, job: dict | None = None) -> list[str]:
    """Files this job changed. For a stacked job, the upstream job's changes don't count."""
    fetch_base(worktree, base)
    start = start_ref(job or {}, base)
    out = run(["git", "diff", "--name-only", f"{start}...HEAD"], cwd=worktree)
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
