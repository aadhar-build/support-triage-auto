"""Supervised retirement of one installed factory checkout."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

import factory as F
from evidence_adapter import _reject_constant, _unique_fields, read_reporting_job
from guardrails import read_job_artifact
import retired_evidence
import retirement_git


def _require(condition, reason):
    if not condition:
        raise F.FactoryError(reason)


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _identity(path):
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode), f"expected a real directory at {path}")
    return info.st_dev, info.st_ino


def _inventory(root):
    jobs = F.jobs_dir(root)
    _identity(jobs.parent)
    _identity(jobs)
    result = {}
    for path in sorted(jobs.iterdir()):
        _require(F.valid_reporting_job_id(path.name), "unrecognized entry in factory/jobs; inspect admissions")
        result[path.name] = _identity(path)
    return result


@contextmanager
def _directory_lock(path):
    _identity(path.parent.parent)
    _identity(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise F.FactoryBusy(f"{path.name} is busy") from None
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        _require(_identity(path) == identity, f"{path.name} was replaced while acquiring its lock")
        yield identity
    finally:
        os.close(fd)


@contextmanager
def _locked_jobs(root, job_id):
    jobs = F.jobs_dir(root)
    with ExitStack() as locks:
        locks.enter_context(_directory_lock(jobs))
        initial = _inventory(root)
        _require(job_id in initial, "unknown job")
        for name in initial:
            _require(locks.enter_context(_directory_lock(jobs / name)) == initial[name], "job identity changed")
        _require(_inventory(root) == initial, "job directories changed while acquiring locks")
        yield initial


def _job_states(root, job_id):
    jobs = F.jobs_dir(root)
    states = {}
    for name in _inventory(root):
        d = jobs / name
        # All existing job inodes are locked. An admitted operation may be outside
        # its short critical section and still in the middle of stacking a job.
        F.require_admission(d)
        job = read_reporting_job(d)
        _require(job.get("stage") in F.TRANSITIONS, f"invalid stage for {name}")
        for key in ("stacked_on", "stacked_on_branch"):
            _require(job.get(key) is None or isinstance(job[key], str), f"invalid stack reference on {name}")
        _require(job.get("stacked_on_pr") is None or type(job["stacked_on_pr"]) is int,
                 f"invalid stack PR on {name}")
        states[name] = job
    target = states[job_id]
    _require(target["stage"] == "merged", f"job stage is {target['stage']}")
    _require(target.get("branch") == f"factory/{job_id}" and target.get("worktree") == f".worktrees/{job_id}",
             "job branch or checkout assignment mismatch")
    _require(type(target.get("pr")) is int and target["pr"] > 0, "job lacks a valid PR")
    _require(target.get("origin") != "controlled", "controlled job requires separate handling")
    for name, peer in states.items():
        if name == job_id:
            continue
        _require(peer.get("worktree") != target["worktree"] and peer.get("branch") != target["branch"],
                 f"duplicate checkout or branch assignment on {name}")
        depends = (peer.get("stacked_on") == job_id or peer.get("stacked_on_branch") == target["branch"]
                   or peer.get("stacked_on_pr") == target["pr"])
        _require(not depends or peer["stage"] in F.TERMINAL, f"active stacked dependent {name}")
        if peer["stage"] not in F.TERMINAL:
            _require(peer.get("worktree") == f".worktrees/{name}" and peer.get("branch") == f"factory/{name}",
                     f"ambiguous active checkout assignment on {name}")
    if any(target.get(k) is not None for k in ("stacked_on", "stacked_on_branch", "stacked_on_pr")):
        upstream = states.get(target.get("stacked_on"))
        _require(upstream and upstream["stage"] == "merged" and upstream["id"] != job_id
                 and upstream.get("branch") == target.get("stacked_on_branch")
                 and type(upstream.get("pr")) is int and upstream["pr"] == target.get("stacked_on_pr"),
                 "unresolved upstream stack on target job")
    return target


def _worker_markers(folder):
    def fail(error):
        raise error
    for parent, dirs, files in os.walk(folder, onerror=fail):
        for name in dirs + files:
            path = Path(parent) / name
            _require(not path.is_symlink(), f"job artifact is a symlink: {name}")
            _require(name != F.OPERATION and not name.endswith((".lock", ".pending", ".replacing")),
                     f"active or interrupted work: {name}")


def _inside(link, paths):
    value = os.fsencode(link).removesuffix(b" (deleted)")
    return any(value == path or value.startswith(path + b"/") for path in paths)


def _guarded_inodes(paths):
    """Include inode identities so a bind-mounted alias cannot hide an open file."""
    result = set()

    def add(info):
        result.add((info.st_dev, info.st_ino))

    def fail(error):
        raise error

    for path in paths:
        if not _present(path):
            continue
        for _, dirs, files, fd in os.fwalk(path, onerror=fail, follow_symlinks=False):
            add(os.fstat(fd))
            for name in dirs + files:
                add(os.stat(name, dir_fd=fd, follow_symlinks=False))
    return result


def _uses_path(link, paths, inodes):
    if _inside(os.readlink(link), paths):
        return True
    info = link.stat()
    return (info.st_dev, info.st_ino) in inodes


def _inspect_task(task, paths, inodes, job_id, ancestors):
    try:
        active = _uses_path(task / "cwd", paths, inodes)
    except FileNotFoundError:
        # Kernel threads and zombies can have no cwd. An incomplete view of a
        # live task with userspace arguments must not authorize removal.
        try:
            status = (task / "status").read_bytes()
            dormant = re.search(rb"^State:\s+[ZX](?:\s|$)", status, re.M)
            kernel = re.search(rb"^Kthread:\s+1$", status, re.M)
            _require((dormant or kernel) and not (task / "cmdline").read_bytes(), "cannot inspect a live task cwd")
        except (FileNotFoundError, ProcessLookupError):
            _require(not _present(task), "incomplete task inventory")
        return
    cmd = (task / "cmdline").read_bytes().split(b"\0")
    mentions_job = any(any(path in arg for path in paths) for arg in cmd) or os.fsencode(job_id) in cmd
    _require(not active and not (int(task.name) not in ancestors and mentions_job),
             f"task {task.name} uses this job or checkout")
    for fd in (task / "fd").iterdir():
        try:
            _require(not _uses_path(fd, paths, inodes), f"task {task.name} uses a checkout descriptor")
        except FileNotFoundError:
            _require(not _present(fd), f"cannot inspect descriptor of task {task.name}")
    for line in (task / "maps").read_bytes().splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            raise F.FactoryError("unrecognized process mapping")
        major, minor = fields[3].split(b":")
        mapped = (os.makedev(int(major, 16), int(minor, 16)), int(fields[4]))
        _require(mapped not in inodes and (len(fields) < 6 or not _inside(fields[5], paths)),
                 f"task {task.name} maps a checkout file")


def _idle_processes(root, target, local, proc=Path("/proc"), parked=None):
    """Require visible Linux host namespaces and inspect every visible thread."""
    _require((proc / "self/status").is_file(), "Linux host /proc visibility is required")
    # Linux include/linux/proc_ns.h: PROC_PID_INIT_INO and PROC_USER_INIT_INO.
    # Nested PID and user namespaces cannot establish host-wide visibility.
    for process in ("self", "1"):
        for name, identity in (("pid", "pid:[4026531836]"), ("user", "user:[4026531837]")):
            _require(os.readlink(proc / process / "ns" / name) == identity,
                     "initial Linux PID and user namespaces required")
    capabilities = re.search(rb"^CapEff:\s+([0-9a-fA-F]+)$", (proc / "self/status").read_bytes(), re.M)
    # Linux removes trusted.* names from listxattr results without CAP_SYS_ADMIN.
    # An apparently empty list is insufficient when that namespace is hidden.
    _require(capabilities and int(capabilities[1], 16) & (1 << 21),
             "cannot inventory restricted Linux extended attributes")
    _require(os.readlink(proc / "self/ns/mnt") == os.readlink(proc / "1/ns/mnt"),
             "controller must use the host init mount namespace")
    mounts = (proc / "self/mountinfo").read_bytes().splitlines()
    local_types = {b"ext2", b"ext3", b"ext4", b"xfs", b"btrfs", b"zfs", b"tmpfs"}
    # A local host cannot see another machine's writer on shared storage.
    for path in (root, Path(local["admin"]), root / target["worktree"], root / "factory/jobs" / target["id"]):
        candidates = []
        for line in mounts:
            fields, sep, detail = line.partition(b" - ")
            columns = fields.split()
            _require(sep and len(columns) >= 6 and len(detail.split()) >= 3, "incomplete mount inventory")
            point = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), columns[4]).rstrip(b"/")
            if os.fsencode(path) == point or os.fsencode(path).startswith(point + b"/"):
                candidates.append((len(point), detail.split()[0]))
        _require(candidates and max(candidates)[1] in local_types,
                 "checkout and evidence must use a supported local host filesystem")
    entries = [line.split(b" - ", 1) for line in mounts if len(line.split()) > 5 and line.split()[4] == b"/proc"]
    _require(len(entries) == 1 and len(entries[0]) == 2 and entries[0][1].startswith(b"proc "),
             "cannot verify the host process mount")
    options = entries[0][0].split()[5].split(b",") + entries[0][1].split()[2].split(b",")
    _require(not any(o.startswith(b"hidepid=") and o != b"hidepid=0" for o in options),
             "process mount hides tasks; use the controller host namespace")
    guarded = [root / target["worktree"], root / "factory/jobs" / target["id"], Path(local["admin"])]
    if parked is not None:
        guarded.append(parked)
    for path in (root, root / ".git", root / ".worktrees", root / "factory/jobs"):
        info = path.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0,
                 "repository and job directories must be controlled by the operator")
    paths = [os.fsencode(path) for path in guarded]
    inodes = _guarded_inodes(guarded)
    _require(not _inside(os.getcwd(), paths), "retirement command is running inside the job or checkout")
    ancestors = set()
    pid = os.getpid()
    while pid:
        _require(pid not in ancestors, "invalid process ancestry")
        ancestors.add(pid)
        m = re.search(rb"^PPid:\s+(\d+)$", (proc / str(pid) / "status").read_bytes(), re.M)
        _require(m, "cannot determine process ancestry")
        pid = int(m[1])
    for process in proc.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            for task in (process / "task").iterdir():
                try:
                    _inspect_task(task, paths, inodes, target["id"], ancestors)
                except (FileNotFoundError, ProcessLookupError):
                    _require(not _present(task), f"incomplete task inventory for {task.name}")
        except (FileNotFoundError, ProcessLookupError):
            _require(not _present(process), f"incomplete process inventory for {process.name}")


def _store(root, *, create=False):
    _identity(root / ".git")
    store = root / ".git/factory-retirements"
    if create and not _present(store):
        store.mkdir(mode=0o700)
        F._sync_directory(store.parent)
    if _present(store):
        info = store.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                 "retirement store must be a private controller directory")
    return store


def read_retirement(root, job_id):
    """Read the controller's private receipt. No store or receipt is created."""
    _require(F.valid_reporting_job_id(job_id), "invalid retirement identity")
    if not (root / ".git").is_dir() and not (root / ".git").is_symlink():
        return None
    path = _store(root) / (job_id + ".json")
    if not _present(path):
        return None
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
             info.st_mode & 0o077 == 0 and info.st_nlink == 1, "unsafe retirement record")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        _require(os.path.samestat(info, os.fstat(stream.fileno())), "retirement record was replaced")
        raw = stream.read(16 * 1024 * 1024 + 1)
    _require(len(raw) <= 16 * 1024 * 1024, "oversized retirement record")
    try:
        record = json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise F.FactoryError("malformed retirement record") from exc
    _require(isinstance(record, dict), "invalid retirement record")
    fields = {"schema_version", "job_id", "status", "candidate_sha", "evidence", "local", "merge",
              "prepared_at", "parked", "owner_confirmed_pr"}
    if "discard_generated" in record:
        fields.add("discard_generated")
    if record.get("status") == "retired":
        fields.add("retired_at")
    _require(fields == set(record) and type(record["schema_version"]) is int and
             isinstance(record["parked"], str) and re.fullmatch(r"[0-9a-f]{32}\.checkout", record["parked"]),
             "invalid retirement record fields")
    sha = record.get("candidate_sha")
    _require(record.get("schema_version") == 1 and record.get("job_id") == job_id and
             record.get("status") in ("prepared", "retired") and isinstance(record.get("evidence"), dict) and
             isinstance(sha, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) and
             all(isinstance(record.get(key), dict) and record[key].get("candidate_sha") == sha
                 for key in ("merge", "local", "evidence")), "invalid retirement record")
    if "discard_generated" in record:
        retirement_git.check_generated_manifest(record["discard_generated"], job_id, sha)
        _require(record["discard_generated"]["entries"], "empty approved generated inventory")
    local, merge = record["local"], record["merge"]
    local_fields = {"candidate_sha", "base_sha", "repository", "admin", "worktree", "branch", "base_ref",
                    "worktree_identity", "admin_identity", "index_identity", "admin_files"}
    merge_fields = {"repository", "repository_id", "pr", "pr_id", "candidate_sha", "base_ref", "base_sha",
                    "merge_commit_sha", "job_pr_association", "candidate_merge_identity"}
    _require(set(local) == local_fields and set(merge) == merge_fields and
             all(type(merge[key]) is int and merge[key] > 0 for key in ("pr", "pr_id", "repository_id")) and
             all(isinstance(merge[key], str) and re.fullmatch(r"[a-f0-9]+", merge[key]) and len(merge[key]) == len(sha)
                 for key in ("base_sha", "merge_commit_sha")) and
             local["base_sha"] == merge["base_sha"] and isinstance(merge["base_ref"], str) and
             bool(merge["base_ref"]) and local["base_ref"] == "refs/remotes/origin/" + merge["base_ref"] and
             isinstance(local["admin"], str) and Path(local["admin"]).parent == root / ".git/worktrees" and
             local["branch"] == f"factory/{job_id}" and local["worktree"] == str(root / ".worktrees" / job_id) and
             local["repository"] == merge["repository"] and isinstance(merge["repository"], str) and
             re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", merge["repository"]) and
             merge["job_pr_association"] in ("verified", "local_only") and
             merge["candidate_merge_identity"] in ("exact", "content_equivalent") and
             record["owner_confirmed_pr"] == (f"{merge['pr']}@{sha}"
                 if _confirmation_reasons(merge) else None), "invalid retirement provenance")
    for key, length in (("worktree_identity", 2), ("admin_identity", 2), ("index_identity", 6)):
        value = local[key]
        _require(isinstance(value, list) and len(value) == length and all(type(n) is int and n >= 0 for n in value)
                 and value[1] > 0,
                 "invalid recorded Git identity")
    admin_files = local["admin_files"]
    _require(isinstance(admin_files, list) and all(isinstance(item, list) and len(item) == 2
             and isinstance(item[0], str) and isinstance(item[1], list) and len(item[1]) == 6
             and all(type(n) is int for n in item[1]) for item in admin_files)
             and len(dict(admin_files)) == len(admin_files) and dict(admin_files).get("index") == local["index_identity"],
             "invalid recorded Git administration")
    try:
        times = [datetime.strptime(record[field], "%Y-%m-%dT%H:%M:%SZ")
                 for field in ("prepared_at", "retired_at") if field in record]
    except (ValueError, TypeError) as err:
        raise F.FactoryError("invalid retirement timestamp") from err
    _require(times == sorted(times), "inconsistent retirement timestamps")
    return record


def _confirmation_reasons(merge):
    reasons = []
    if merge["job_pr_association"] == "local_only":
        reasons.append("historical job-to-PR link is unverified")
    if merge["candidate_merge_identity"] == "content_equivalent":
        reasons.append("rewritten merge contains equivalent changes; original commit identity at merge is unverified")
    return reasons


def _write(path, record):
    F.atomic_write(path, (json.dumps(record, sort_keys=True) + "\n").encode())


def _git(root, *args):
    env = retirement_git.git_environment()
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "protocol.allow=never", *args]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=120)
    _require(result.returncode == 0, f"git {args[0]} refused; inspect the checkout and retirement record")
    return result.stdout


def _detached(root, job, record):
    _require(job["id"] == record["job_id"] and job.get("pr") == record["merge"]["pr"] and
             type(job["pr"]) is int and job["branch"] == record["local"]["branch"] and
             str(root / job["worktree"]) == record["local"]["worktree"], "job association changed")
    wt = root / job["worktree"]
    parked = _store(root) / record["parked"]
    _require(not _present(parked), "parked checkout still exists")
    admin = Path(record["local"]["admin"])
    _require(admin.is_absolute() and admin.parent == root / ".git/worktrees", "invalid recorded Git admin path")
    _identity(wt.parent)
    if _present(admin.parent):
        _identity(admin.parent)
    _require(not _present(wt) and not _present(admin), "checkout or Git registration still exists")
    listing = _git(root, "worktree", "list", "--porcelain", "-z").split(b"\0")
    _require(b"worktree " + os.fsencode(wt) not in listing and
             b"branch refs/heads/" + os.fsencode(job["branch"]) not in listing,
             "branch or path is still registered")
    head = _git(root, "rev-parse", "--verify", "refs/heads/" + job["branch"]).decode().strip()
    _require(head == record["candidate_sha"], "retained branch changed")
    _require(retired_evidence.valid_detached(root, F.jobs_dir(root) / job["id"], job, record["evidence"]),
             "retirement evidence cannot be revalidated")


def _complete(root, path, job, record):
    _detached(root, job, record)
    F._sync_directory(root / ".worktrees")
    for directory in (root / ".git/worktrees", root / ".git"):
        if directory.exists():
            F._sync_directory(directory)
    final = {**record, "status": "retired", "retired_at": F.now()}
    _write(path, final)
    _require(read_retirement(root, job["id"]) == final, "completed retirement record failed read-back")
    return {"job_id": job["id"], "status": "retired", "candidate_sha": record["candidate_sha"]}


PREVIEW_SECONDS = 5
INVENTORY_SECONDS = 30


def _preview_git(directory, *args):
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-c", "protocol.allow=never", *args],
        cwd=directory, env=retirement_git.git_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=2, check=True,
    )
    return result.stdout.removesuffix(b"\n")


def preview_root():
    common = _preview_git(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(os.fsdecode(common)).parent


def _preview_head(root, branch):
    raw = _preview_git(root, "rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}^{{commit}}")
    _require(re.fullmatch(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})", raw), "local candidate ref is unavailable")
    return raw.decode("ascii")


def _preview_snapshot(root, job_id):
    """Observe a fixed set of small files for one job without acquiring any lock."""
    folder = F.jobs_dir(root) / job_id
    folder_id = _identity(folder)
    raw = read_job_artifact(folder, "job.json", max_bytes=64 * 1024)
    _require(raw is not None, "job state is inaccessible, unsafe, or exceeds the 64 KiB preview limit")
    job = F.validate_reporting_job(json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_reject_constant), job_id)
    _require(job.get("stage") in F.TRANSITIONS, "job stage is unknown")
    if job["stage"] != "merged":
        return (folder_id, raw), {"status": "blocked", "reason": f"job stage is {job['stage']}"}
    branch = f"factory/{job_id}"
    _require(job.get("branch") == branch and job.get("worktree") == f".worktrees/{job_id}",
             "local job assignment is ambiguous")
    _require(type(job.get("pr")) is int and job["pr"] > 0, "job lacks a valid PR")
    if job.get("origin") == "controlled":
        return (folder_id, raw), {"status": "blocked", "reason": "controlled job requires separate handling"}
    if _present(_store(root) / (job_id + ".json")):
        return (folder_id, raw), {"status": "inconclusive", "reason": "existing retirement journal requires inspection"}
    if any(_present(folder / name) for name in (F.OPERATION, F.GATE_PENDING)):
        return (folder_id, raw), {"status": "blocked", "reason": "job operation is active or interrupted"}

    wt = root / job["worktree"]
    wt_id = _identity(wt)
    pointer = read_job_artifact(wt, ".git", max_bytes=4096)
    _require(pointer and pointer.startswith(b"gitdir: "), "linked checkout pointer is unavailable")
    admin = retirement_git._pointer(pointer.removesuffix(b"\n")[8:], wt)
    _require(admin.parent == root / ".git/worktrees", "checkout points outside the expected Git administration")
    _identity(admin.parent)
    admin_id = _identity(admin)
    back = read_job_artifact(admin, "gitdir", max_bytes=4096)
    common = read_job_artifact(admin, "commondir", max_bytes=4096)
    head = read_job_artifact(admin, "HEAD", max_bytes=4096)
    _require(back and retirement_git._pointer(back.removesuffix(b"\n"), admin) == wt / ".git" and
             common and retirement_git._pointer(common.removesuffix(b"\n"), admin) == root / ".git" and
             head == f"ref: refs/heads/{branch}\n".encode("ascii"), "local checkout registration is ambiguous")
    if any(_present(admin / name) for name in ("locked", "index.lock")):
        return (folder_id, raw, wt_id, admin_id), {"status": "blocked", "reason": "local checkout has a Git lock marker"}
    candidate = _preview_head(root, branch)
    return (folder_id, raw, wt_id, admin_id, pointer, back, common, head), {
        "status": "screened locally", "pr": job["pr"], "candidate_sha": candidate,
    }


def _preview(root, job_id):
    result = {"job_id": job_id, "assessment": "eligibility not evaluated",
              "deferred_checks": ["GitHub merge", "checkout contents", "evidence", "dependencies", "active processes and locks"]}
    deadline = time.monotonic() + PREVIEW_SECONDS
    try:
        first = _preview_snapshot(root, job_id)
        _require(time.monotonic() < deadline, "local preview exceeded its time budget")
        second = _preview_snapshot(root, job_id)
        _require(first == second, "local state changed during preview")
        _require(time.monotonic() < deadline, "local preview exceeded its time budget")
        return {**result, **second[1]}
    except (F.FactoryError, OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, (F.FactoryError, TimeoutError)) else "local state is unavailable or changed during preview"
        return {**result, "status": "inconclusive", "reason": reason}


def _inventory_generated(root, job_id):
    # Advisory inspection is lock-free. Never use this result as a retained
    # checkout proof: apply regenerates the inventory in its maintenance window.
    deadline = time.monotonic() + INVENTORY_SECONDS
    initial = _preview_snapshot(root, job_id)
    if initial[1]["status"] != "screened locally":
        return {"job_id": job_id, "assessment": "eligibility not evaluated", **initial[1]}
    _require(time.monotonic() < deadline, "generated inventory exceeded its time budget")
    folder = F.jobs_dir(root) / job_id
    state = read_job_artifact(folder, "job.json", max_bytes=64 * 1024)
    _require(state is not None, "cannot read bounded job state for generated inventory")
    job = F.validate_reporting_job(json.loads(state, object_pairs_hook=_unique_fields, parse_constant=_reject_constant), job_id)
    config = read_job_artifact(root / "factory", "config.json", max_bytes=64 * 1024)
    _require(config is not None, "factory configuration exceeds the inventory limit or cannot be read")
    cfg = json.loads(config, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    _require(isinstance(cfg, dict), "invalid factory configuration")
    local, manifest = retirement_git.inspect_generated(root, folder, job, cfg)
    retirement_git.check_generated_manifest(manifest, job_id, local["candidate_sha"])
    _require(initial[1]["candidate_sha"] == local["candidate_sha"] and initial == _preview_snapshot(root, job_id)
             and config == read_job_artifact(root / "factory", "config.json", max_bytes=64 * 1024),
             "local state changed during generated inventory")
    _require(time.monotonic() < deadline, "generated inventory exceeded its time budget")
    if not manifest["entries"]:
        return {"job_id": job_id, "status": "no generated files", "file_count": 0, "assessment": "eligibility not evaluated"}
    return {"job_id": job_id, "status": "generated inventory", "manifest": manifest, "assessment": "eligibility not evaluated"}


def _inspect_checkout(root, folder, job, cfg, approved_digest):
    if approved_digest is None:
        return retirement_git.validate(root, folder, job, cfg), None
    local, manifest = retirement_git.inspect_generated(root, folder, job, cfg)
    retirement_git.check_generated_manifest(manifest, job["id"], local["candidate_sha"])
    _require(manifest["entries"] and manifest["digest"] == approved_digest,
             "generated inventory differs from the confirmed digest or contains no bytecode")
    return local, manifest


def run(root, job_id, *, apply=False, recover=False, confirm_pr=None,
        inventory_generated=False, discard_generated=None):
    """Preview by default. Recovery can finalize a completed removal, never replay one."""
    _require(F.valid_reporting_job_id(job_id), "invalid job ID")
    _require(sum(bool(mode) for mode in (apply, recover, inventory_generated)) <= 1,
             "apply, recovery and generated inventory are separate operations")
    _require(discard_generated is None or apply and isinstance(discard_generated, str)
             and re.fullmatch(r"[0-9a-f]{64}", discard_generated),
             "generated discard requires --apply and the complete inventory SHA-256")
    _require(confirm_pr is None or apply, "PR confirmation is only valid with --apply")
    _require(root.is_absolute() and root.resolve() == root, "expected canonical repository root")
    if inventory_generated:
        return _inventory_generated(root, job_id)
    if not apply and not recover:
        return _preview(root, job_id)
    with _locked_jobs(root, job_id) as inodes:
        path = _store(root) / (job_id + ".json")
        folder = F.jobs_dir(root) / job_id
        job = _job_states(root, job_id)
        _worker_markers(folder)
        record = read_retirement(root, job_id)
        if record is not None:
            approved = record.get("discard_generated")
            if discard_generated is not None:
                _require(approved and approved["digest"] == discard_generated,
                         "discard digest does not match the existing retirement")
            if record["status"] == "retired":
                _detached(root, job, record)
                return {"job_id": job_id, "status": "already retired", "candidate_sha": record["candidate_sha"]}
            _require(recover, "prepared retirement requires inspection and explicit --recover")
            wt = root / job["worktree"]
            parked = path.parent / record["parked"]
            if _present(parked):
                _require(not _present(wt), "both original and parked checkout paths exist; inspect before recovery")
                with _directory_lock(parked) as held:
                    _require(list(held) == record["local"]["worktree_identity"], "parked checkout identity changed")
                    cfg = F.load_config(root)
                    _require(retired_evidence.matches_snapshot(root, folder, job, record["evidence"]),
                             "job evidence changed during retirement")
                    retirement_git.validate_parked(root, folder, job, cfg, record["local"], parked, expected_generated=approved)
                    _idle_processes(root, job, record["local"], parked=parked)
                    _git(root, "worktree", "move", "--", str(parked), str(wt))
                    F._sync_directory(wt.parent)
                    F._sync_directory(parked.parent)
            if _present(wt):
                # An interruption before Git removal can be cancelled without
                # altering the tree. A partial or changed checkout stays blocked.
                with _directory_lock(wt) as held:
                    _require(list(held) == record["local"]["worktree_identity"], "checkout identity changed")
                    digest = approved["digest"] if approved else None
                    local, restored = _inspect_checkout(root, folder, job, F.load_config(root), digest)
                    _require(restored == approved, "generated inventory changed after retirement was prepared")
                    _require(local == record["local"] and retired_evidence.capture(root, folder, job) == record["evidence"],
                             "checkout or evidence changed after retirement was prepared")
                    _idle_processes(root, job, local)
                    preserved = path.with_name(f"{job_id}.{secrets.token_hex(12)}.cancelled.json")
                    path.rename(preserved)
                    F._sync_directory(path.parent)
                    return {"job_id": job_id, "status": "cancelled", "candidate_sha": record["candidate_sha"]}
            return _complete(root, path, job, record)
        _require(not recover, "no prepared retirement to recover")
        cfg = F.load_config(root)
        wt = root / job["worktree"]
        with _directory_lock(wt) as held:
            local, generated = _inspect_checkout(root, folder, job, cfg, discard_generated)
            _require(tuple(local["worktree_identity"]) == held, "checkout changed after locking")
            _idle_processes(root, job, local)
            evidence = retired_evidence.capture(root, folder, job)
            merge = retirement_git.verify_merge(root, job, cfg, local)
            # Recheck local identities after network I/O, including all job
            # admissions and dependencies. No GitHub mutation is performed.
            _require(inodes == _inventory(root) and job == _job_states(root, job_id) and cfg == F.load_config(root),
                     "factory state changed during retirement checks")
            _worker_markers(folder)
            _require(evidence == retired_evidence.capture(root, folder, job), "job evidence changed")
            _require((local, generated) == _inspect_checkout(root, folder, job, cfg, discard_generated),
                     "Git state or generated inventory changed")
            _idle_processes(root, job, local)
            association = merge.get("job_pr_association")
            _require(association in ("verified", "local_only"), "unknown job-to-PR association")
            identity = merge.get("candidate_merge_identity")
            _require(identity in ("exact", "content_equivalent"), "unknown candidate merge identity")
            reasons = _confirmation_reasons(merge)
            expected = f"{job['pr']}@{local['candidate_sha']}"
            if reasons:
                _require(confirm_pr == expected, ". ".join(reasons) + f". Confirm this job with --confirm-pr {expected}")
            elif confirm_pr is not None:
                _require(confirm_pr == expected, "confirmation belongs to a different candidate or PR")
            _store(root, create=True)
            _require(not _present(path), "retirement record appeared")
            parked = path.parent / (secrets.token_hex(16) + ".checkout")
            _require(not _present(parked), "quarantine path already exists")
            record = dict(schema_version=1, status="prepared", prepared_at=F.now(), job_id=job_id, parked=parked.name,
                          candidate_sha=local["candidate_sha"], local=local, merge=merge, evidence=evidence,
                          owner_confirmed_pr=confirm_pr if reasons else None)
            if generated is not None:
                record["discard_generated"] = generated
            _write(path, record)
            _require(read_retirement(root, job_id) == record, "prepared retirement record failed read-back")
            # Move first, without force. New writes at the public job path can
            # no longer enter the directory Git will remove. A process holding
            # the original inode or file descriptors is detected again below.
            _git(root, "worktree", "move", "--", str(wt), str(parked))
            F._sync_directory(wt.parent)
            F._sync_directory(parked.parent)
            _idle_processes(root, job, local, parked=parked)
            _require(retired_evidence.matches_snapshot(root, folder, job, evidence),
                     "job evidence changed while parking checkout")
            _require(inodes == _inventory(root) and job == _job_states(root, job_id) and cfg == F.load_config(root),
                     "factory changed while parking checkout; inspect prepared retirement")
            _worker_markers(folder)
            retirement_git.validate_parked(root, folder, job, cfg, local, parked, expected_generated=generated)
            _git(root, "worktree", "remove", "--", str(parked))
            return _complete(root, path, job, record)
