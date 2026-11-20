"""Report selected checkout shapes without opening or authorizing removal of artifacts."""

from __future__ import annotations

import os
import re
import stat
import time
from contextlib import ExitStack
from pathlib import Path

import factory as F
import retirement_git as Git


_MAX_DIRECTORIES = 256
_MAX_ENTRIES = 4096
_MAX_DEPTH = 16
_MAX_SECONDS = 5
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_BUILD_ROOTS = {(b"dist",), (b"build",), (b"web", b"dist"), (b"web", b"build")}
_KINDS = ("node_modules_roots", "build_output_roots", "python_cache_roots", "unclassified_roots_or_entries",
          "tracked_shape_mismatches")


class _Observation:
    def __init__(self):
        self.deadline = time.monotonic() + _MAX_SECONDS
        self.counts = dict.fromkeys(_KINDS, 0)
        self.inspected = {"directories": 0, "entries": 0}
        self.conditions = set()

    def time_left(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.conditions.add("deadline")
            return 0
        return remaining

    def output(self) -> dict:
        if self.conditions:
            coverage = "partial" if self.inspected["directories"] else "unresolved"
        else:
            coverage = "complete"
        return {"coverage": coverage, "counts_are_lower_bounds": True, "observed": self.counts,
                "inspected": self.inspected, "limiting_conditions": sorted(self.conditions),
                "limits": {"directories": _MAX_DIRECTORIES, "entries": _MAX_ENTRIES, "depth": _MAX_DEPTH,
                           "git_output_bytes": 2 * Git._MAX_TRACKED_LIST_BYTES, "seconds": _MAX_SECONDS}}


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _directory(stack: ExitStack, parent: int, name: str | bytes) -> int:
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    stack.callback(os.close, descriptor)
    return descriptor


def _check_registration(root: Path, checkout: Path, git_fd: int, branch: str) -> tuple:
    gitdir = Git._metadata(checkout / ".git")
    if not gitdir.startswith(b"gitdir: "):
        raise F.FactoryError("unverified checkout registration")
    admin = Git._pointer(gitdir.removeprefix(b"gitdir: "), checkout)
    if admin.parent != root / ".git/worktrees":
        raise F.FactoryError("unverified checkout registration")
    with ExitStack() as descriptors:
        worktrees = _directory(descriptors, git_fd, "worktrees")
        directory = _directory(descriptors, worktrees, admin.name)
        before = os.fstat(directory)
        index = os.stat("index", dir_fd=directory, follow_symlinks=False)
        if (Git._pointer(Git._metadata(admin / "gitdir"), admin) != checkout / ".git"
                or Git._pointer(Git._metadata(admin / "commondir"), admin) != root / ".git"
                or Git._metadata(admin / "HEAD") != b"ref: refs/heads/" + branch.encode("ascii")
                or not stat.S_ISREG(index.st_mode)
                or _identity(admin.lstat()) != _identity(before)):
            raise F.FactoryError("unverified checkout registration")
        return admin, _identity(before), _identity(index)


def _candidate_matches(root: Path, branch: str, sha: str, observation: _Observation) -> bool:
    remaining = observation.time_left()
    if not remaining:
        return False
    try:
        result = Git._git_result(root, "rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}^{{commit}}",
                                 timeout=remaining)
    except F.FactoryError:
        observation.conditions.add("candidate_unavailable")
        observation.time_left()
        return False
    if result.returncode:
        observation.conditions.add("candidate_unavailable")
        return False
    if result.stdout != sha.encode("ascii") + b"\n":
        observation.conditions.add("candidate_changed")
        return False
    return True


def _tracked_children(inventory: dict, observation: _Observation) -> dict:
    children = {(): {}}
    for name, (mode, _) in inventory.items():
        if not observation.time_left():
            return {}
        parts = tuple(name.split(b"/"))
        # A deeper known path remains recognizable as a directory at the
        # boundary, without constructing expectations for unseen descendants.
        for index, part in enumerate(parts[:_MAX_DEPTH + 1]):
            kind = b"dir" if index + 1 != len(parts) else mode
            children.setdefault(parts[:index], {})[part] = kind
    return children


def _untracked(directory: bool, path: tuple[bytes, ...]) -> str:
    if directory:
        if path[-1] == b"node_modules":
            return "node_modules_roots"
        if path[-1] == b"__pycache__":
            return "python_cache_roots"
        if path in _BUILD_ROOTS:
            return "build_output_roots"
    return "unclassified_roots_or_entries"


def _shape_matches(mode: int, expected: bytes) -> bool:
    if expected == b"dir":
        return stat.S_ISDIR(mode)
    if expected == b"120000":
        return stat.S_ISLNK(mode)
    return stat.S_ISREG(mode)


def _shape_mismatch(observation: _Observation, count: int = 1) -> None:
    if count:
        observation.counts["tracked_shape_mismatches"] += count
        observation.conditions.add("tracked_shape_mismatch")


def _scan_directory(descriptor: int, path: tuple[bytes, ...], expected: dict, observation: _Observation) -> None:
    if not observation.time_left():
        return
    if len(path) > _MAX_DEPTH:
        observation.conditions.add("depth_limit")
        return
    if observation.inspected["directories"] >= _MAX_DIRECTORIES:
        observation.conditions.add("directory_limit")
        return
    current = expected.get(path, {})
    before = _identity(os.fstat(descriptor))
    seen = set()
    try:
        with os.scandir(descriptor) as entries:
            observation.inspected["directories"] += 1
            for entry in entries:
                if not observation.time_left():
                    break
                name = os.fsencode(entry.name)
                if not path and name == b".git":
                    continue
                if observation.inspected["entries"] >= _MAX_ENTRIES:
                    observation.conditions.add("entry_limit")
                    break
                observation.inspected["entries"] += 1
                kind = current.get(name)
                seen.add(name)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    observation.conditions.add("entry_unavailable")
                    continue
                child_path = (*path, name)
                if info.st_dev != before[0]:
                    observation.conditions.add("mount_unsafe_or_unavailable")
                elif kind is None:
                    observation.counts[_untracked(stat.S_ISDIR(info.st_mode), child_path)] += 1
                elif not _shape_matches(info.st_mode, kind):
                    _shape_mismatch(observation)
                elif kind == b"dir":
                    _scan_known_directory(descriptor, name, child_path, info, expected, observation)
            else:
                _shape_mismatch(observation, len(current.keys() - seen))
    except OSError:
        observation.conditions.add("directory_unavailable")
    if _identity(os.fstat(descriptor)) != before:
        observation.conditions.add("directory_changed")


def _scan_known_directory(parent: int, name: bytes, path: tuple[bytes, ...], before: os.stat_result,
                          expected: dict, observation: _Observation) -> None:
    if len(path) > _MAX_DEPTH:
        observation.conditions.add("depth_limit")
        return
    if observation.inspected["directories"] >= _MAX_DIRECTORIES:
        observation.conditions.add("directory_limit")
        return
    try:
        with ExitStack() as descriptors:
            descriptor = _directory(descriptors, parent, name)
            if _identity(os.fstat(descriptor)) != _identity(before):
                observation.conditions.add("directory_changed")
                return
            _scan_directory(descriptor, path, expected, observation)
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(before):
                observation.conditions.add("directory_changed")
    except OSError:
        observation.conditions.add("directory_unavailable")


def _scan_checkout(descriptor: int, expected: dict, observation: _Observation) -> None:
    _scan_directory(descriptor, (), expected, observation)


def _mount_safe(path: Path, observation: _Observation) -> bool:
    try:
        Git._check_mounts(path)
    except (F.FactoryError, OSError):
        observation.conditions.add("mount_unsafe_or_unavailable")
        return False
    return bool(observation.time_left())


def observe_checkout(root: Path, job: dict, candidate_sha: str) -> dict:
    """Return lower bounds over inspected paths, with no artifact contents or filenames."""
    observation = _Observation()
    if not observation.time_left():
        return observation.output()
    job_id = job.get("id")
    if (not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*", job_id)
            or job.get("branch") != f"factory/{job_id}" or job.get("worktree") != f".worktrees/{job_id}"
            or not Git._sha(candidate_sha)):
        observation.conditions.add("unsafe_checkout")
        return observation.output()
    branch = job["branch"]
    checkout = root / ".worktrees" / job_id
    try:
        with ExitStack() as descriptors:
            if not root.is_absolute() or root.resolve(strict=True) != root:
                raise F.FactoryError("unverified checkout path")
            directory = os.open(root, _DIRECTORY)
            descriptors.callback(os.close, directory)
            git = _directory(descriptors, directory, ".git")
            worktrees = _directory(descriptors, directory, ".worktrees")
            descriptor = _directory(descriptors, worktrees, job_id)
            before = _identity(os.fstat(descriptor))
            registration = _check_registration(root, checkout, git, branch)
            if not _mount_safe(checkout, observation) or not _candidate_matches(root, branch, candidate_sha, observation):
                return observation.output()
            try:
                inventory = Git._inventory(checkout, candidate_sha, observation.deadline)
            except (F.FactoryError, OSError, ValueError):
                observation.conditions.add("git_inventory_unavailable")
                observation.time_left()
                return observation.output()
            if _identity(os.stat(job_id, dir_fd=worktrees, follow_symlinks=False)) != before:
                observation.conditions.add("directory_changed")
                return observation.output()
            expected = _tracked_children(inventory, observation)
            if observation.time_left():
                _scan_checkout(descriptor, expected, observation)
            if _identity(checkout.lstat()) != before:
                observation.conditions.add("directory_changed")
            if _check_registration(root, checkout, git, branch) != registration:
                observation.conditions.add("git_inventory_changed")
            _mount_safe(checkout, observation)
            _candidate_matches(root, branch, candidate_sha, observation)
    except (F.FactoryError, OSError, ValueError, RecursionError):
        observation.conditions.add("unsafe_checkout")
    return observation.output()
