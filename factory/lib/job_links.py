"""Controller-local receipts for links observed in existing GitHub API responses."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from contextlib import ExitStack

from guardrails import read_job_artifact
from observations import _open_job


_REPO = r"[A-Za-z0-9-]+/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+"
_REMOTE = re.compile(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                     r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_DIRECTORY = "factory-link-receipts"
_INSTANCE = ".factory-link-instance"


def public_repo(remote):
    match = _REMOTE.fullmatch(remote or "")
    return match[1] if match and re.fullmatch(_REPO, match[1]) else None


class PublicGitHubResponse(dict):
    """Repository bound by the local client's pinned public GitHub request."""
    def __init__(self, fields, repository):
        super().__init__(fields)
        self.repository = repository


def _positive(value):
    return type(value) is int and value > 0


def _read(root, token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    raw = read_job_artifact(root / ".git" / _DIRECTORY, token, max_bytes=2048)
    if raw is None or hashlib.sha256(raw).hexdigest() != token:
        return None
    try:
        record = json.loads(raw)
        return record if isinstance(record, dict) else None
    except (ValueError, RecursionError):
        return None


def _store(root, record):
    # Publish the reference in job.json only after a complete private receipt exists.
    # A partial or orphaned write cannot authorize a later report.
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    token = hashlib.sha256(raw).hexdigest()
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    try:
        with ExitStack() as stack:
            git = os.open(root / ".git", flags)
            stack.callback(os.close, git)
            try:
                os.mkdir(_DIRECTORY, 0o700, dir_fd=git)
            except FileExistsError:
                pass
            directory = os.open(_DIRECTORY, flags, dir_fd=git)
            stack.callback(os.close, directory)
            fd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o400, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            os.fsync(git)
    except FileExistsError:
        return token if _read(root, token) == record else None
    except OSError:
        return None
    return token


def _same_repository(obj, repo, api):
    return (isinstance(obj, dict) and _positive(obj.get("id"))
            and obj.get("full_name") == repo and obj.get("url") == api)


def _instance(root, folder, token, *, create=False):
    # One empty inode has a name in the admitted job and a name in the local
    # controller store. Copying old job files or reusing a job ID cannot recreate it.
    anchor = token + ".instance"
    try:
        with _open_job(folder) as job_fd, _open_job(root / ".git" / _DIRECTORY) as store_fd:
            if create:
                os.close(os.open(anchor, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDONLY, 0o400, dir_fd=store_fd))
                os.link(anchor, _INSTANCE, src_dir_fd=store_fd, dst_dir_fd=job_fd, follow_symlinks=False)
                os.fsync(store_fd)
                os.fsync(job_fd)
            owned = os.stat(anchor, dir_fd=store_fd, follow_symlinks=False)
            local = os.stat(_INSTANCE, dir_fd=job_fd, follow_symlinks=False)
            return stat.S_ISREG(owned.st_mode) and owned.st_nlink == 2 and os.path.samestat(owned, local)
    except (ValueError, OSError):
        return False


def _issue(repo, number, response):
    if not isinstance(response, dict) or response.get("pull_request") is not None:
        return None
    api = f"https://api.github.com/repos/{repo}"
    url = f"https://github.com/{repo}/issues/{number}"
    if not (_positive(number) and _positive(response.get("number")) and response["number"] == number
            and _positive(response.get("id")) and response.get("repository_url") == api
            and response.get("html_url") == url and response.get("url") == f"{api}/issues/{number}"):
        return None
    repository = response.get("repository")
    if repository is not None and not _same_repository(repository, repo, api):
        return None
    return {"issue": number, "id": response["id"], "url": url, "repo": repo,
            "repo_id": repository["id"] if repository else None}


def capture_source(root, folder, job, response):
    if not isinstance(response, PublicGitHubResponse):
        return
    facts = _issue(response.repository, job["issue"], response)
    if not facts:
        return
    if facts["repo_id"] is None:
        # Standard issue responses may omit the nested repository. Resolve only
        # the repository from this exact pinned request, never a URL in issue text.
        facts["repo_id"] = _live_repository_id(root, facts["repo"])
        if facts["repo_id"] is None:
            return
    token = _store(root, {**facts, "kind": "issue", "job": job["id"], "instance": uuid.uuid4().hex})
    if token and _instance(root, folder, token, create=True):
        job["source_receipt"] = token


def _source(root, job):
    token = job.get("source_receipt")
    entry = _read(root, token)
    if not entry or entry.get("kind") != "issue" or entry.get("job") != job.get("id"):
        return None
    repo, number = entry.get("repo"), job.get("issue")
    if (not isinstance(repo, str) or not re.fullmatch(_REPO, repo) or not _positive(number)
            or not _positive(entry.get("issue")) or entry["issue"] != number or not _positive(entry.get("id"))
            or not _positive(entry.get("repo_id")) or entry.get("url") != f"https://github.com/{repo}/issues/{number}"
            or not _instance(root, root / "factory" / "jobs" / job["id"], token)):
        return None
    return entry


def _candidate(job, source, response):
    repo, number = source["repo"], job.get("pr")
    if not isinstance(response, dict) or not _positive(number):
        return None
    api = f"https://api.github.com/repos/{repo}"
    head, base = response.get("head"), response.get("base")
    if not (isinstance(head, dict) and isinstance(base, dict) and _positive(response.get("id"))
            and _positive(response.get("number")) and response["number"] == number
            and response.get("url") == f"{api}/pulls/{number}"
            and response.get("html_url") == f"https://github.com/{repo}/pull/{number}"
            and head.get("ref") == job.get("branch") == f"factory/{job['id']}"
            and isinstance(head.get("sha"), str) and _SHA.fullmatch(head["sha"])
            and all(_same_repository(side.get("repo"), repo, api) and side["repo"]["id"] == source["repo_id"]
                    for side in (head, base))):
        return None
    return {"kind": "candidate", "source": job["source_receipt"], "pr": number, "id": response["id"], "sha": head["sha"]}


def capture_candidate(root, job, response):
    if not isinstance(response, PublicGitHubResponse):
        return
    source = _source(root, job)
    if source and source["repo"] == response.repository:
        candidate = _candidate(job, source, response)
        if candidate and (token := _store(root, candidate)):
            job["candidate_receipt"] = token


def _get(root, endpoint, projection):
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
    env.pop("GH_DEBUG", None)
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "--method", "GET",
                                 "--jq", projection, endpoint], cwd=root, env=env, capture_output=True, timeout=5)
        if result.returncode == 0 and len(result.stdout) <= 8192:
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                return data
    except (ValueError, OSError, RecursionError, subprocess.TimeoutExpired):
        pass
    return None


def _live_repository_id(root, repo):
    endpoint = f"repos/{repo}"
    obj = _get(root, endpoint, "{id,full_name,url,html_url}")
    if (_same_repository(obj, repo, f"https://api.github.com/{endpoint}")
            and obj.get("html_url") == f"https://github.com/{repo}"):
        return obj["id"]
    return None


def _live_links(root, job, candidate_sha, source):
    repo = source["repo"]
    endpoint = f"repos/{repo}"
    if _live_repository_id(root, repo) != source["repo_id"]:
        return None, None
    issue = _get(root, f"{endpoint}/issues/{source['issue']}",
                 "{id,number,url,html_url,repository_url,pull_request:(.pull_request!=null),"
                 "repository:(.repository | if . then {id,full_name,url} else null end)}")
    if issue is None or issue.get("pull_request") is not False:
        return None, None
    facts = _issue(repo, source["issue"], {**issue, "pull_request": None})
    if not facts or facts["id"] != source["id"] or facts["repo_id"] not in (None, source["repo_id"]):
        return None, None
    candidate = _read(root, job.get("candidate_receipt"))
    if (not candidate or not isinstance(candidate_sha, str) or not _SHA.fullmatch(candidate_sha)
            or not isinstance(candidate.get("sha"), str) or not _SHA.fullmatch(candidate["sha"])
            or not _positive(candidate.get("id")) or candidate.get("source") != job["source_receipt"]
            or not _positive(job.get("pr")) or candidate.get("pr") != job["pr"]):
        return source["url"], None
    pull = _get(root, f"{endpoint}/pulls/{job['pr']}",
                "{id,number,url,html_url,base:{repo:(.base.repo|{id,full_name,url})},"
                "head:{ref:.head.ref,sha:.head.sha,repo:(.head.repo|{id,full_name,url})}}")
    # The immutable PR identity binds every round. GitHub must report the
    # exact current local head, including after a subsequent push.
    expected = {**candidate, "sha": candidate_sha}
    if _candidate(job, source, pull) == expected and _read(root, job["candidate_receipt"]) == candidate:
        return source["url"], f"https://github.com/{repo}/commit/{candidate_sha}"
    return source["url"], None


def verified_links(root, job, candidate_sha, current_repo):
    source = _source(root, job)
    if not source or current_repo(root) != source["repo"]:
        return None, None
    links = _live_links(root, job, candidate_sha, source)
    # All network reads are complete. Recheck the admission, its immutable
    # receipt, and current origin even when only the issue could be linked.
    if links[0] and _source(root, job) == source and current_repo(root) == source["repo"]:
        return links
    return None, None
