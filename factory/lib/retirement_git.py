"""Read-only checkout and merge verification for Factory worktree retirement."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import factory as F
from job_links import _get, _live_repository_id, _read, _same_repository, _source, public_repo


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IN_PROGRESS = {
    "AUTO_MERGE", "BISECT_HEAD", "BISECT_LOG", "BISECT_START", "CHERRY_PICK_HEAD", "MERGE_AUTOSTASH",
    "MERGE_HEAD", "REBASE_HEAD", "REVERT_HEAD", "SQUASH_MSG", "rebase-apply", "rebase-merge", "sequencer",
}
_MAIN_OPERATIONS = _IN_PROGRESS - {"AUTO_MERGE", "BISECT_HEAD", "BISECT_LOG", "SQUASH_MSG"}
_ADMIN_ENTRIES = {"COMMIT_EDITMSG", "FETCH_HEAD", "HEAD", "ORIG_HEAD", "commondir", "gitdir", "index", "logs", "refs"}
_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNT_ESCAPE = re.compile(rb"\\(?:040|011|012|134)")
_REFLOG_IDENTITY = re.compile(rb"[^\x00\r\n]* <[^<>\x00\r\n]*> [0-9]+ [+-][0-9]{4}(?:\t[^\x00\r\n]*)?\Z")
_FILE_MODES = {b"100644", b"100755", b"120000"}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_CACHE_DIRECTORY = "__pycache__"
_MAX_GENERATED_ENTRIES = 256
_MAX_GENERATED_BYTES = 64 * 1024 * 1024
_MAX_GENERATED_SECONDS = 15
_MAX_TRACKED_ENTRIES = 8192
_MAX_TRACKED_LIST_BYTES = 4 * 1024 * 1024
_MAX_TRACKED_BYTES = 256 * 1024 * 1024
_DARWIN = sys.platform == "darwin"
_XATTR_SHOWCOMPRESSION = 0x0020  # Darwin exposes otherwise hidden compression attributes.
_PR_FIELDS = (
    "{id,number,url,html_url,state,merged,merged_at,merge_commit_sha,"
    "head:(.head|{sha,ref,repo:(.repo|{id,full_name,url})}),"
    "base:(.base|{sha,ref,repo:(.repo|{id,full_name,url})})}"
)


def git_environment() -> dict[str, str]:
    """Pin Git to physical on-disk state and prevent implicit transport."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return dict(env, GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
                GIT_OPTIONAL_LOCKS="0", GIT_PROTOCOL_FROM_USER="0", GIT_TERMINAL_PROMPT="0")


def _git_command(args: tuple[str, ...]) -> list[str]:
    return ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", *args]


def _git_result(directory: Path, *args: str, stdin: bytes | None = None,
                timeout: float = 8) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _git_command(args), cwd=directory, env=git_environment(),
            input=stdin, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise F.FactoryError("local Git verification is unavailable") from None


def _git(directory: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = _git_result(directory, *args, stdin=stdin)
    if result.returncode:
        raise F.FactoryError("local Git could not verify the checkout or refs")
    return result.stdout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise F.FactoryError("generated cache inspection exceeded its time limit")
    return remaining


def _bounded_git_inventory(directory: Path, deadline: float, *args: str) -> bytes:
    deadline = min(deadline, time.monotonic() + 8)
    output = bytearray()
    with subprocess.Popen(_git_command(args), cwd=directory, env=git_environment(), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            with selectors.DefaultSelector() as reader:
                reader.register(process.stdout, selectors.EVENT_READ)
                while reader.select(_remaining(deadline)):
                    block = os.read(process.stdout.fileno(), min(65536, _MAX_TRACKED_LIST_BYTES + 1 - len(output)))
                    if not block:
                        break
                    output.extend(block)
                    if len(output) > _MAX_TRACKED_LIST_BYTES:
                        raise F.FactoryError("tracked Git inventory exceeds output limits")
                else:
                    raise F.FactoryError("generated cache inspection exceeded its time limit")
            if process.wait(timeout=_remaining(deadline)) or output.count(b"\0") > _MAX_TRACKED_ENTRIES:
                raise F.FactoryError("could not verify the bounded tracked Git inventory")
        except subprocess.TimeoutExpired:
            raise F.FactoryError("generated cache inspection exceeded its time limit") from None
        finally:
            if process.poll() is None:
                process.kill()
    return bytes(output)


def _sha(value):
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _commit(root: Path, ref: str) -> str:
    value = _git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip().decode("ascii")
    if not _sha(value):
        raise F.FactoryError("local Git commit identity is unavailable")
    return value


def _origin(root: Path) -> str:
    raw = _git(root, "config", "--null", "--get-all", "remote.origin.url").split(b"\0")
    resolved = _git(root, "remote", "get-url", "--all", "origin").splitlines()
    if len(raw) != 2 or len(resolved) != 1:
        raise F.FactoryError("a single GitHub origin is required")
    configured = public_repo(os.fsdecode(raw[0]))
    if not configured or public_repo(os.fsdecode(resolved[0])) != configured:
        raise F.FactoryError("a canonical GitHub origin is required")
    return configured


def _directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise F.FactoryError("checkout and Git administration must use physical directories")
    return info


def _job_directory(root: Path, path: Path) -> os.stat_result:
    # Walk the assigned path without following a substituted ancestor, then
    # ensure the opened directory still has the assigned pathname.
    with ExitStack() as descriptors:
        directory = os.open(root, _DIR_FLAGS)
        descriptors.callback(os.close, directory)
        for component in path.parts:
            directory = os.open(component, _DIR_FLAGS, dir_fd=directory)
            descriptors.callback(os.close, directory)
        info = os.fstat(directory)
        if not os.path.samestat(info, _directory(root / path)):
            raise F.FactoryError("assigned Factory job directory changed during verification")
        return info


def _check_mounts(*paths: Path) -> None:
    for path in paths:
        if _directory(path).st_dev != _directory(path.parent).st_dev or os.path.ismount(path):
            raise F.FactoryError("retirement refuses mounted checkout or Git administration paths")
    if not sys.platform.startswith("linux"):
        return
    # Linux same-device bind mounts are indistinguishable by st_dev alone.
    # Check the kernel namespace's pathname records, including quoted names.
    roots = tuple(os.fsencode(path) for path in paths)
    point_count = 0
    try:
        with _MOUNTINFO.open("rb") as mounts:
            for record in mounts:
                fields = record.split(b" - ", 1)
                parts = fields[0].split(b" ")
                if len(fields) != 2 or len(parts) < 6 or not parts[4].startswith(b"/"):
                    raise ValueError
                point_count += 1
                point = _MOUNT_ESCAPE.sub(lambda match: bytes([int(match[0][1:], 8)]), parts[4])
                if any(point == path or point.startswith(path + b"/") for path in roots):
                    raise F.FactoryError("retirement refuses nested mounts in checkout or Git administration")
        if not point_count:
            raise ValueError
    except (OSError, ValueError):
        raise F.FactoryError("local mount information is unavailable") from None


def _file_identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@lru_cache(maxsize=1)
def _darwin_listxattr():
    function = ctypes.CDLL(None, use_errno=True).flistxattr
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    function.restype = ctypes.c_ssize_t
    return function


def _attribute_names(descriptor: int) -> set[str]:
    if hasattr(os, "listxattr"):
        return set(os.listxattr(descriptor))
    if not _DARWIN:
        raise OSError
    list_attributes = _darwin_listxattr()
    size = list_attributes(descriptor, None, 0, _XATTR_SHOWCOMPRESSION)
    if not 0 <= size <= 1024 * 1024:
        raise OSError
    if size == 0:
        return set()
    buffer = ctypes.create_string_buffer(size)
    count = list_attributes(descriptor, buffer, size, _XATTR_SHOWCOMPRESSION)
    raw = buffer.raw[:count]
    if count != size or not raw.endswith(b"\0") or b"\0\0" in raw:
        raise OSError
    return {os.fsdecode(name) for name in raw[:-1].split(b"\0")}


def _require_no_attributes(descriptor: int, symlink: bytes | None = None) -> None:
    try:
        if symlink is not None and not _DARWIN:
            # Linux cannot list xattrs on an O_PATH symlink fd. Anchor the
            # no-follow request to the already-open physical parent instead.
            path = os.fsencode(f"/proc/self/fd/{descriptor}/") + symlink
            names = set(os.listxattr(path, follow_symlinks=False))
        else:
            names = _attribute_names(descriptor)
    except (OSError, AttributeError):
        raise F.FactoryError("extended attribute inventory is unavailable") from None
    if _DARWIN:
        # The macOS kernel generates and protects this origin label. All
        # other names, including arbitrary Apple-prefixed names, block removal.
        names.discard("com.apple.provenance")
    if names:
        raise F.FactoryError("checkout or Git administration has local extended attributes")


def _check_path_attributes(path: Path, flags: int, info: os.stat_result) -> None:
    descriptor = os.open(path, flags)
    try:
        identity = _file_identity(info)
        if _file_identity(os.fstat(descriptor)) != identity:
            raise F.FactoryError("Git administration changed while checking local metadata")
        _require_no_attributes(descriptor)
        if _file_identity(os.fstat(descriptor)) != identity:
            raise F.FactoryError("Git administration changed while checking local metadata")
    finally:
        os.close(descriptor)


def _metadata(path: Path, limit: int = 4096) -> bytes:
    with os.fdopen(os.open(path, _FILE_FLAGS), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise F.FactoryError("unsafe linked checkout metadata")
        _require_no_attributes(stream.fileno())
        data = stream.read(limit + 1)
        if len(data) != before.st_size or _file_identity(path.lstat()) != _file_identity(before):
            raise F.FactoryError("linked checkout metadata changed during verification")
    return data.removesuffix(b"\n")


def _pointer(data: bytes, relative_to: Path) -> Path:
    if not data or b"\n" in data or b"\r" in data or b"\0" in data:
        raise F.FactoryError("invalid linked checkout metadata")
    return Path(os.path.abspath(relative_to / os.fsdecode(data)))


def _registration(root: Path, checkout: Path, branch: str, candidate: str) -> None:
    records = [record.split(b"\0") for record in _git(root, "worktree", "list", "--porcelain", "-z").split(b"\0\0")
               if record]
    expected = [b"worktree " + os.fsencode(checkout), b"HEAD " + candidate.encode("ascii"),
                b"branch " + branch.encode("ascii")]
    matching = [record for record in records if record[0] == expected[0] or expected[2] in record]
    if matching != [expected]:
        raise F.FactoryError("checkout registration is missing, locked, stale, or ambiguous")


def _admin(root: Path, checkout: Path) -> Path:
    worktrees = root / ".git/worktrees"
    _directory(worktrees)
    data = _metadata(checkout / ".git")
    if not data.startswith(b"gitdir: "):
        raise F.FactoryError("checkout must have linked Git administration")
    admin = _pointer(data.removeprefix(b"gitdir: "), checkout)
    if admin.parent != worktrees:
        raise F.FactoryError("linked checkout administration is outside the controller repository")
    _directory(admin)
    if (_pointer(_metadata(admin / "gitdir"), admin) != checkout / ".git"
            or _pointer(_metadata(admin / "commondir"), admin) != root / ".git"
            or _git(checkout, "rev-parse", "--absolute-git-dir").removesuffix(b"\n") != os.fsencode(admin)
            or _git(checkout, "rev-parse", "--show-toplevel").removesuffix(b"\n") != os.fsencode(checkout)):
        raise F.FactoryError("linked checkout and Git administration backlinks do not agree")
    return admin


def _check_admin(root: Path, admin: Path, branch: str, base_ref: str) -> list[int]:
    common = root / ".git"
    entries = {item.name for item in admin.iterdir()}
    if any(name == "locked" or name.endswith(".lock") or name in _IN_PROGRESS for name in entries):
        raise F.FactoryError("linked checkout administration is locked or has a Git operation in progress")
    if entries - _ADMIN_ENTRIES:
        raise F.FactoryError("linked checkout has additional Git administration data")
    if (any(item.name.endswith(".lock") or item.name in _MAIN_OPERATIONS for item in common.iterdir())
            or any(os.path.lexists(common / name) for name in ("info/grafts", branch + ".lock", base_ref + ".lock"))):
        raise F.FactoryError("repository refs are locked or have rewritten ancestry")
    snapshot = []
    admin_info = _directory(admin)
    admin_device = admin_info.st_dev
    _check_path_attributes(admin, _DIR_FLAGS, admin_info)
    for name in sorted(entries):
        item = admin / name
        if name in {"refs", "logs"}:
            directory_info = _directory(item)
            if directory_info.st_dev != admin_device:
                raise F.FactoryError("retirement refuses mounted Git administration paths")
            _check_path_attributes(item, _DIR_FLAGS, directory_info)
            children = {child.name for child in item.iterdir()}
            if children - ({"HEAD"} if name == "logs" else set()):
                raise F.FactoryError("linked checkout has additional Git administration data")
            if not children:
                continue
            item /= "HEAD"
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != admin_device:
            raise F.FactoryError("linked checkout administration contains an unsafe file")
        _check_path_attributes(item, _FILE_FLAGS, info)
        # Move legitimately rewrites the backlink, so keep its identity out
        # of the stable snapshot and verify its live contents separately.
        if name != "gitdir":
            snapshot.append([name, _file_identity(info)])
    return snapshot


def _retained_admin(root: Path, admin: Path, candidate: str) -> None:
    reflog = admin / "logs/HEAD"
    remembered = {candidate.encode("ascii")}
    if os.path.lexists(reflog):
        for line in _metadata(reflog, 1024 * 1024).splitlines():
            parts = line.split(b" ", 2)
            if len(parts) != 3 or not _REFLOG_IDENTITY.fullmatch(parts[2]):
                raise F.FactoryError("linked checkout contains an unverified HEAD reflog")
            remembered.update(parts[:2])
    orig_head = admin / "ORIG_HEAD"
    if os.path.lexists(orig_head):
        remembered.add(_metadata(orig_head))
    fetch_head = admin / "FETCH_HEAD"
    if os.path.lexists(fetch_head):
        for line in _metadata(fetch_head, 1024 * 1024).splitlines():
            if len(line.split(b"\t", 2)) != 3:
                raise F.FactoryError("linked checkout contains an unverified FETCH_HEAD")
            remembered.add(line.split(b"\t", 1)[0])
    if any(len(oid) != len(candidate) or not _sha(oid.decode("ascii")) for oid in remembered):
        raise F.FactoryError("linked checkout contains an unverified local commit pointer")
    remembered.discard(b"0" * len(candidate))
    # Only retained refs may account for an old checkout tip. The checkout's
    # reflog, per-worktree refs, and detached HEADs cannot provide retention.
    if _git(root, "rev-list", "--max-count=1", "--stdin", "--not", "--branches", "--remotes", "--tags",
            stdin=b"\n".join(sorted(remembered)) + b"\n"):
        raise F.FactoryError("linked checkout remembers a commit no retained branch or tag contains")
    message_path = admin / "COMMIT_EDITMSG"
    if os.path.lexists(message_path):
        message = _metadata(message_path, 128 * 1024)
        # Preserve draft commit messages, while recognizing Git's ordinary
        # saved copy of a message already present in a retained commit.
        tips = [candidate.encode("ascii"), *(remembered - {candidate.encode("ascii")})]
        for oid in tips:
            recorded = _git(root, "show", "-s", "--format=%B", oid.decode("ascii")).rstrip(b"\n")
            if message == recorded:
                return
        raise F.FactoryError("linked checkout contains an unverified draft commit message")


def _inventory(checkout: Path, candidate: str, deadline: float | None = None) -> dict[bytes, tuple[bytes, bytes]]:
    tree_args = ("ls-tree", "-r", "-z", "--full-tree", candidate)
    index_args = ("ls-files", "--stage", "-v", "-z")
    if deadline is None:
        tree_records = _git(checkout, *tree_args)
        index_records = _git(checkout, *index_args)
    else:
        tree_records = _bounded_git_inventory(checkout, deadline, *tree_args)
        index_records = _bounded_git_inventory(checkout, deadline, *index_args)
    tree = {}
    for line in tree_records.split(b"\0")[:-1]:
        metadata, path = line.split(b"\t", 1)
        mode, kind, object_id = metadata.split(b" ")
        if mode not in _FILE_MODES or kind != b"blob":
            raise F.FactoryError("checkout has a submodule or an unsupported tracked entry")
        tree[path] = mode, object_id
    index = {}
    for line in index_records.split(b"\0")[:-1]:
        metadata, path = line.split(b"\t", 1)
        flag, mode, object_id, stage = metadata.split(b" ")
        if flag != b"H" or stage != b"0" or path in index:
            raise F.FactoryError("checkout index contains conflicts, hidden files, or unsupported entries")
        index[path] = mode, object_id
    if index != tree:
        raise F.FactoryError("checkout has staged changes")
    return tree


def _reject_external_filters(checkout: Path, paths: dict) -> None:
    result = _git_result(checkout, "config", "--null", "--get-regexp", r"^filter\..*\.(clean|process)$")
    if result.returncode == 1:
        return
    if result.returncode:
        raise F.FactoryError("could not safely inspect the checkout's Git filters")
    drivers = set()
    for row in result.stdout.split(b"\0")[:-1]:
        name, separator, command = row.partition(b"\n")
        if separator and command:
            drivers.add(name.removeprefix(b"filter.").rsplit(b".", 1)[0])
    if not drivers or not paths:
        return
    attrs = _git(checkout, "check-attr", "-z", "--stdin", "filter", stdin=b"\0".join(paths) + b"\0").split(b"\0")
    if len(attrs) != 3 * len(paths) + 1 or any(value in drivers for value in attrs[2::3]):
        raise F.FactoryError("checkout uses an external Git clean or process filter")


def _blob(parent: int, name: bytes, mode: bytes, algorithm: str, *, bounded_info: os.stat_result | None = None,
          deadline: float | None = None) -> bytes:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if bounded_info is not None and _file_identity(before) != _file_identity(bounded_info):
        raise F.FactoryError("checkout changed while verifying tracked contents")
    if mode == b"120000" and stat.S_ISLNK(before.st_mode):
        if _DARWIN:
            descriptor = os.open(name, os.O_RDONLY | os.O_SYMLINK, dir_fd=parent)
            try:
                if _file_identity(os.fstat(descriptor)) != _file_identity(before):
                    raise F.FactoryError("checkout changed while verifying tracked contents")
                _require_no_attributes(descriptor)
            finally:
                os.close(descriptor)
        else:
            _require_no_attributes(parent, name)
        target = os.readlink(name, dir_fd=parent)
        digest = hashlib.new(algorithm, b"blob " + str(len(target)).encode("ascii") + b"\0" + target)
    elif stat.S_ISREG(before.st_mode) and mode == (b"100755" if before.st_mode & stat.S_IXUSR else b"100644"):
        digest = hashlib.new(algorithm, b"blob " + str(before.st_size).encode("ascii") + b"\0")
        with os.fdopen(os.open(name, _FILE_FLAGS, dir_fd=parent), "rb") as stream:
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
                raise F.FactoryError("checkout changed while verifying tracked contents")
            _require_no_attributes(stream.fileno())
            size_remaining = before.st_size
            while chunk := stream.read(1024 * 1024):
                if deadline is not None:
                    _remaining(deadline)
                    size_remaining -= len(chunk)
                    if size_remaining < 0:
                        raise F.FactoryError("checkout changed while verifying tracked contents")
                digest.update(chunk)
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
                raise F.FactoryError("checkout changed while verifying tracked contents")
    else:
        raise F.FactoryError("checkout has a changed or unsupported tracked entry")
    if _file_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _file_identity(before):
        raise F.FactoryError("checkout changed while verifying tracked contents")
    return digest.hexdigest().encode("ascii")


def _generated_identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _generated_mode(mode: int, *, directory: bool) -> bool:
    permissions = stat.S_IMODE(mode)
    if directory:
        return stat.S_ISDIR(mode) and permissions & 0o500 == 0o500 and permissions & ~0o755 == 0
    return stat.S_ISREG(mode) and bool(permissions & 0o400) and permissions & ~0o644 == 0


def _manifest_digest(unsigned: dict) -> str:
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _valid_entry_fields(entry: dict, directory: bool) -> bool:
    fields = {"path", "kind", "identity"} if directory else {"path", "kind", "identity", "size", "sha256"}
    if set(entry) != fields or entry["kind"] != ("directory" if directory else "file"):
        return False
    identity = entry["identity"]
    if (not isinstance(identity, list) or len(identity) != 9
            or any(type(number) is not int or not 0 <= number < 2**64 for number in identity)
            or identity[1] == 0 or identity[2] > 0o177777 or identity[5] == 0
            or not _generated_mode(identity[2], directory=directory)):
        return False
    if directory:
        return True
    return (identity[5] == 1 and type(entry["size"]) is int and entry["size"] == identity[6] > 16
            and isinstance(entry["sha256"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])))


def check_generated_manifest(manifest: dict, job_id: str, candidate_sha: str) -> None:
    """Check the structure and digest of a generated inventory offline."""
    from retirement_bytecode import MAX_PYC_BYTES

    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "job_id", "candidate_sha", "entries", "digest"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*", job_id)
            or manifest["job_id"] != job_id or not _sha(candidate_sha) or manifest["candidate_sha"] != candidate_sha
            or not isinstance(manifest["entries"], list) or len(manifest["entries"]) > _MAX_GENERATED_ENTRIES):
        raise F.FactoryError("invalid generated cache manifest")
    last = ""
    directories = set()
    file_parents = set()
    inodes = set()
    total = 0
    for entry in manifest["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise F.FactoryError("invalid generated cache manifest entry")
        path = entry["path"]
        parts = path.split("/")
        if (path <= last or len(path) > 4096 or any(part in {"", ".", "..", ".git"} for part in parts)
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in path)
                or parts.count(_CACHE_DIRECTORY) != 1):
            raise F.FactoryError("invalid generated cache manifest path")
        last = path
        directory = parts[-1] == _CACHE_DIRECTORY
        if not _valid_entry_fields(entry, directory):
            raise F.FactoryError("invalid generated cache manifest metadata")
        inode = tuple(entry["identity"][:2])
        if inode in inodes:
            raise F.FactoryError("duplicate generated cache manifest identity")
        inodes.add(inode)
        if directory:
            directories.add(path)
        else:
            if len(parts) < 2 or parts[-2] != _CACHE_DIRECTORY or not parts[-1].endswith(".pyc"):
                raise F.FactoryError("invalid generated bytecode manifest path")
            parent = "/".join(parts[:-1])
            file_parents.add(parent)
            total += entry["size"]
            if entry["size"] > MAX_PYC_BYTES or total > _MAX_GENERATED_BYTES:
                raise F.FactoryError("generated cache manifest exceeds inventory limits")
    if directories != file_parents:
        raise F.FactoryError("generated cache manifest has unaccounted directories")
    unsigned = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest["digest"] != _manifest_digest(unsigned):
        raise F.FactoryError("generated cache manifest digest does not match its inventory")


class _GeneratedInventory:
    def __init__(self, checkout: Path, original: Path, tree: dict, info: os.stat_result, algorithm: str, deadline: float):
        self.checkout = checkout
        self.original = original
        self.tree = tree.copy()
        self.algorithm = algorithm
        self.ownership = info.st_uid, info.st_gid
        self.device = info.st_dev
        self.deadline = deadline
        self.total_bytes = 0
        self.tracked_bytes = 0
        self.entries = []
        self.files = []

    def remaining(self) -> float:
        return _remaining(self.deadline)

    def track_file(self, info: os.stat_result) -> None:
        self.tracked_bytes += info.st_size
        if self.tracked_bytes > _MAX_TRACKED_BYTES:
            raise F.FactoryError("tracked checkout contents exceed generated inventory limits")

    def reserve(self, info: os.stat_result, *, directory: bool) -> None:
        self.remaining()
        if len(self.entries) >= _MAX_GENERATED_ENTRIES:
            raise F.FactoryError("generated cache has too many entries")
        if (info.st_dev != self.device or (info.st_uid, info.st_gid) != self.ownership
                or getattr(info, "st_flags", 0) or not _generated_mode(info.st_mode, directory=directory)
                or (not directory and info.st_nlink != 1)):
            raise F.FactoryError("generated cache has unsafe filesystem metadata")

    def directory(self, parent: int, name: bytes, path: bytes, info: os.stat_result) -> None:
        from retirement_bytecode import MAX_PYC_BYTES, inspect_bytecode

        self.reserve(info, directory=True)
        identity = _generated_identity(info)
        directory = os.open(name, _DIR_FLAGS, dir_fd=parent)
        try:
            if _generated_identity(os.fstat(directory)) != identity:
                raise F.FactoryError("generated cache directory changed during verification")
            _require_no_attributes(directory)
            self.entries.append({"kind": "directory", "path": path.decode("utf-8"), "identity": identity})
            with os.scandir(directory) as children:
                first = len(self.files)
                for child in children:
                    file_info = child.stat(follow_symlinks=False)
                    self.reserve(file_info, directory=False)
                    self.total_bytes += file_info.st_size
                    if not 16 < file_info.st_size <= MAX_PYC_BYTES or self.total_bytes > _MAX_GENERATED_BYTES:
                        raise F.FactoryError("generated cache exceeds byte inventory limits")
                    child_name = os.fsencode(child.name)
                    relative = path + b"/" + child_name
                    file = inspect_bytecode(directory, child_name, relative, self.checkout, self.original,
                                            self.tree, self.algorithm)
                    self.remaining()
                    if file["identity"] != _generated_identity(file_info):
                        raise F.FactoryError("generated cache file changed during verification")
                    self.files.append(relative)
                    self.entries.append({"kind": "file", "path": relative.decode("utf-8"), **file})
            if len(self.files) == first:
                raise F.FactoryError("generated cache directory has no reproducible files")
            if (_generated_identity(os.fstat(directory)) != identity
                    or _generated_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity):
                raise F.FactoryError("generated cache directory changed during verification")
        finally:
            os.close(directory)

    def verify_snapshot(self, checkout_fd: int) -> None:
        directories = {}
        for entry in self.entries:
            path = entry["path"].encode("utf-8")
            cache = path if entry["kind"] == "directory" else path.rsplit(b"/", 1)[0]
            directories.setdefault(cache, []).append(entry)
        for path, entries in directories.items():
            with ExitStack() as descriptors:
                directory = checkout_fd
                for name in path.split(b"/"):
                    self.remaining()
                    directory = os.open(name, _DIR_FLAGS, dir_fd=directory)
                    descriptors.callback(os.close, directory)
                expected_dir = entries[0]["identity"]
                for entry in entries[1:]:
                    self.remaining()
                    name = entry["path"].encode("utf-8").rsplit(b"/", 1)[-1]
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if _generated_identity(info) != entry["identity"]:
                        raise F.FactoryError("generated cache file changed during verification")
                if _generated_identity(os.fstat(directory)) != expected_dir:
                    raise F.FactoryError("generated cache directory changed during verification")

    def manifest(self, checkout_fd: int, job_id: str, candidate: str) -> dict:
        if self.files:
            result = _git_result(self.checkout, "check-ignore", "-z", "--stdin", stdin=b"\0".join(self.files) + b"\0",
                                 timeout=min(8, self.remaining()))
            if result.returncode or set(result.stdout.split(b"\0")[:-1]) != set(self.files):
                raise F.FactoryError("a generated cache file is not ignored by Git")
        unsigned = {"schema_version": 1, "job_id": job_id, "candidate_sha": candidate,
                    "entries": sorted(self.entries, key=lambda entry: entry["path"])}
        manifest = {**unsigned, "digest": _manifest_digest(unsigned)}
        check_generated_manifest(manifest, job_id, candidate)
        self.verify_snapshot(checkout_fd)
        self.remaining()
        return manifest


def _scan(directory: int, tree: dict, directories: set, algorithm: str, prefix: bytes = b"", *,
          generated: _GeneratedInventory | None = None) -> None:
    before = os.fstat(directory)
    _require_no_attributes(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            if generated is not None:
                generated.remaining()
            name = os.fsencode(entry.name)
            path = prefix + name
            if path == b".git":
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != before.st_dev:
                raise F.FactoryError("retirement refuses mounted checkout paths")
            if path in directories:
                child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                try:
                    _scan(child, tree, directories, algorithm, path + b"/", generated=generated)
                finally:
                    os.close(child)
            elif path in tree:
                mode, object_id = tree.pop(path)
                if generated is not None:
                    generated.track_file(info)
                if _blob(directory, name, mode, algorithm, bounded_info=info if generated else None,
                         deadline=generated.deadline if generated else None) != object_id:
                    raise F.FactoryError("checkout has modified tracked contents")
            elif generated is not None and name == b"__pycache__":
                generated.directory(directory, name, path, info)
            else:
                raise F.FactoryError("checkout contains untracked or ignored contents")
    if _file_identity(os.fstat(directory)) != _file_identity(before):
        raise F.FactoryError("checkout changed while verifying its contents")


def _verify_contents(checkout: Path, info: os.stat_result, candidate: str, job_id: str,
                     original: Path | None = None) -> dict | None:
    deadline = time.monotonic() + _MAX_GENERATED_SECONDS if original is not None else None
    tree = _inventory(checkout, candidate, deadline)
    _reject_external_filters(checkout, tree)
    algorithm = "sha1" if len(candidate) == 40 else "sha256"
    generated = (_GeneratedInventory(checkout, original, tree, info, algorithm, deadline)
                 if original is not None else None)
    directories = set()
    for path in tree:
        while b"/" in path:
            path = path.rsplit(b"/", 1)[0]
            directories.add(path)
            if generated is not None and len(tree) + len(directories) > _MAX_TRACKED_ENTRIES:
                raise F.FactoryError("tracked checkout paths exceed generated inventory limits")
    # Hash raw files directly. Git status can run external filters or omit
    # files due to index flags, excludes, normalization, or stat shortcuts.
    directory = os.open(checkout, _DIR_FLAGS)
    try:
        if not os.path.samestat(os.fstat(directory), info):
            raise F.FactoryError("checkout directory identity changed during verification")
        _scan(directory, tree, directories, algorithm, generated=generated)
        if tree:
            raise F.FactoryError("checkout has missing tracked contents")
        return generated.manifest(directory, job_id, candidate) if generated is not None else None
    finally:
        os.close(directory)


def _validate(root: Path, folder: Path, job: dict, cfg: dict, parked: Path | None, *,
              allow_generated: bool = False) -> tuple[dict, dict | None]:
    job_id = job.get("id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*", job_id):
        raise F.FactoryError("invalid retirement job identity")
    job_path = Path("factory/jobs") / job_id
    requested_folder = Path(os.path.abspath(folder))
    if requested_folder != Path(os.path.abspath(root)) / job_path:
        raise F.FactoryError("retirement requires the assigned Factory job directory")
    root = root.resolve(strict=True)
    branch = f"refs/heads/factory/{job_id}"
    original = root / ".worktrees" / job_id
    if job.get("branch") != branch.removeprefix("refs/heads/") or job.get("worktree") != f".worktrees/{job_id}":
        raise F.FactoryError("checkout does not use the assigned Factory job paths")
    job_info = _job_directory(root, job_path)
    for path in (root / ".git", original.parent):
        _directory(path)
    checkout = original
    if parked is not None:
        if (parked.parent != root / ".git/factory-retirements" or not parked.name.endswith(".checkout")
                or os.path.lexists(original)):
            raise F.FactoryError("retirement checkout was not parked at its private verified path")
        _directory(parked.parent)
        checkout = parked
    checkout_info = _directory(checkout)
    if (_git(root, "rev-parse", "--absolute-git-dir").removesuffix(b"\n") != os.fsencode(root / ".git")
            or _git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n") != os.fsencode(root)
            or _git(root, "rev-parse", "--is-shallow-repository") != b"false\n"):
        raise F.FactoryError("retirement needs the controller's complete local Git repository")
    base = cfg.get("base_branch")
    if not isinstance(base, str) or not base:
        raise F.FactoryError("the Factory base branch is unavailable")
    base_ref = f"refs/remotes/origin/{base}"
    _git(root, "check-ref-format", base_ref)
    repository = _origin(root)
    admin = _admin(root, checkout)
    admin_info = _directory(admin)
    _check_mounts(checkout, admin)
    admin_files = _check_admin(root, admin, branch, base_ref)
    if _metadata(admin / "HEAD") != b"ref: " + branch.encode("ascii"):
        raise F.FactoryError("checkout is detached or on a different branch")
    candidate = _commit(root, branch)
    base_sha = _commit(root, base_ref)
    _registration(root, checkout, branch, candidate)
    _retained_admin(root, admin, candidate)
    generated = _verify_contents(checkout, checkout_info, candidate, job_id, original if allow_generated else None)
    _check_mounts(checkout, admin)
    if (not os.path.samestat(_job_directory(root, job_path), job_info)
            or not os.path.samestat(_directory(checkout), checkout_info) or not os.path.samestat(_directory(admin), admin_info)
            or _check_admin(root, admin, branch, base_ref) != admin_files or _admin(root, checkout) != admin
            or _commit(root, branch) != candidate or _commit(root, base_ref) != base_sha or _origin(root) != repository):
        raise F.FactoryError("checkout, origin, or refs changed during verification")
    local = {"candidate_sha": candidate, "base_sha": base_sha, "repository": repository, "admin": str(admin),
             "worktree": str(checkout), "branch": job["branch"], "base_ref": base_ref,
             "worktree_identity": [checkout_info.st_dev, checkout_info.st_ino],
             "admin_identity": [admin_info.st_dev, admin_info.st_ino], "index_identity": dict(admin_files)["index"],
             "admin_files": admin_files}
    return local, generated


def validate(root: Path, folder: Path, job: dict, cfg: dict) -> dict:
    """Return stable, JSON-safe identities for the assigned, strictly clean checkout."""
    try:
        return _validate(root, folder, job, cfg, None)[0]
    except (OSError, ValueError, RecursionError):
        raise F.FactoryError("could not safely inspect the checkout and Git administration") from None


def inspect_generated(root: Path, folder: Path, job: dict, cfg: dict) -> tuple[dict, dict]:
    """Inspect the checkout with an opt-in inventory for reproducible Python caches."""
    try:
        return _validate(root, folder, job, cfg, None, allow_generated=True)
    except (OSError, ValueError, RecursionError):
        raise F.FactoryError("could not safely inspect generated checkout contents") from None


def validate_parked(root: Path, folder: Path, job: dict, cfg: dict, original: dict, path: Path, *,
                    expected_generated: dict | None = None) -> None:
    """Recheck the same registered checkout after moving it to private parking."""
    try:
        if expected_generated is not None:
            check_generated_manifest(expected_generated, job["id"], original["candidate_sha"])
        current, generated = _validate(root, folder, job, cfg, path, allow_generated=expected_generated is not None)
    except (OSError, ValueError, RecursionError):
        raise F.FactoryError("could not safely inspect the parked checkout and Git administration") from None
    if current != {**original, "worktree": str(path)}:
        raise F.FactoryError("parked checkout identity changed during retirement")
    if generated != expected_generated:
        raise F.FactoryError("generated cache inventory changed during retirement")


def _ancestor(root: Path, older: str, newer: str) -> bool:
    result = _git_result(root, "merge-base", "--is-ancestor", older, newer)
    if result.returncode not in (0, 1):
        raise F.FactoryError("local commit ancestry is unavailable")
    return result.returncode == 0


def _reachable_merge(root: Path, merge: str, base: str) -> bool:
    if merge == base:
        return True
    if not _ancestor(root, merge, base):
        return False
    # Along the base's first-parent line, the oldest unexcluded commit's
    # first parent must be exactly the merge result, even if later merges exist.
    chain = _git(root, "rev-list", "--first-parent", "--parents", f"{merge}..{base}").splitlines()
    return bool(chain and len(chain[-1].split()) >= 2 and chain[-1].split()[1] == merge.encode("ascii"))


def _parents(root: Path, commit: str) -> list[str]:
    return _git(root, "rev-list", "--parents", "-n", "1", commit).decode("ascii").split()[1:]


def _delta(root: Path, old: str, new: str) -> bytes:
    return _git(root, "diff-tree", "--no-commit-id", "--raw", "-r", "-z", "--no-abbrev", "--no-renames",
                "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", old, new)


def _rewritten_merge(root: Path, candidate: str, merge: str, parent: str) -> bool:
    common = _git(root, "merge-base", "--all", candidate, parent).decode("ascii").splitlines()
    if len(common) != 1 or not _sha(common[0]) or common[0] == candidate:
        return False
    delta = _delta(root, common[0], candidate)
    if not delta:
        return False
    if delta == _delta(root, parent, merge):
        return True  # Squash or the single-commit rebase equivalent.

    # GitHub's rebase merge SHA is the final rebased commit. Require a complete
    # one-for-one linear replay with identical old/new file modes and blobs.
    steps = _git(root, "rev-list", "--first-parent", "--parents", "--max-count=65",
                 f"{common[0]}..{candidate}").decode("ascii").splitlines()
    if not 1 < len(steps) <= 64:
        return False
    rewritten = merge
    for step in steps:
        original = step.split()
        rewritten_parents = _parents(root, rewritten)
        if len(original) != 2 or len(rewritten_parents) != 1:
            return False
        expected = _delta(root, original[1], original[0])
        if not expected or expected != _delta(root, rewritten_parents[0], rewritten):
            return False
        rewritten = rewritten_parents[0]
    return (steps[-1].split()[1] == common[0] and _ancestor(root, common[0], rewritten)
            and delta == _delta(root, rewritten, merge))


def _local_merge(root: Path, candidate: str, merge: str, base: str) -> str:
    if _commit(root, merge) != merge or not _reachable_merge(root, merge, base):
        raise F.FactoryError("local base does not contain the exact GitHub merge outcome")
    if candidate == merge:
        return "exact"
    parents = _parents(root, merge)
    if len(parents) == 2 and parents[1] == candidate and not _ancestor(root, candidate, parents[0]):
        return "exact"
    if len(parents) == 1 and _rewritten_merge(root, candidate, merge, parents[0]):
        # Rewritten commits can prove retained changes. They do not record
        # the candidate SHA GitHub used as input at the time of the merge.
        return "content_equivalent"
    raise F.FactoryError("local base does not prove the GitHub merge retained the candidate")


def _association(root: Path, job: dict, repo: str, repository_id: int, pull: dict, candidate: str) -> str:
    source_present = "source_receipt" in job
    candidate_present = "candidate_receipt" in job
    if not source_present and not candidate_present:
        return "local_only"
    source = _source(root, job)
    if not source or source["repo"] != repo or source["repo_id"] != repository_id:
        raise F.FactoryError("historical repository association is invalid or mismatched")
    if not candidate_present:
        return "local_only"
    receipt = _read(root, job.get("candidate_receipt"))
    if (not receipt or receipt.get("kind") != "candidate" or receipt.get("source") != job["source_receipt"]
            or type(receipt.get("id")) is not int or receipt["id"] != pull["id"]
            or type(receipt.get("pr")) is not int or receipt["pr"] != job["pr"]
            or not _sha(receipt.get("sha")) or not _ancestor(root, receipt["sha"], candidate)):
        raise F.FactoryError("historical candidate association is invalid or mismatched")
    return "verified"


def _merged_at(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def _verify_live_base(root: Path, repo: str, base: str, expected: str) -> None:
    ref_path = "/".join(quote(part, safe="") for part in base.split("/"))
    live = _get(root, f"repos/{repo}/git/ref/heads/{ref_path}", "{ref,object:(.object|{type,sha})}")
    target = live.get("object") if live else None
    if not (live and live.get("ref") == f"refs/heads/{base}" and isinstance(target, dict)
            and target.get("type") == "commit" and target.get("sha") == expected):
        raise F.FactoryError("current GitHub base is unavailable or differs from the local origin-tracking ref")


def verify_merge(root: Path, job: dict, cfg: dict, local: dict) -> dict:
    """Read the exact repository and PR, then independently verify the local merge object."""
    root = root.resolve(strict=True)
    repo = local["repository"]
    number = job.get("pr")
    if type(number) is not int or number <= 0 or _origin(root) != repo:
        raise F.FactoryError("retirement requires the exact recorded GitHub repository and PR")
    repository_id = _live_repository_id(root, repo)
    if repository_id is None:
        raise F.FactoryError("GitHub repository verification is unavailable")
    endpoint = f"repos/{repo}/pulls/{number}"
    pull = _get(root, endpoint, _PR_FIELDS)
    if pull is None:
        raise F.FactoryError("GitHub PR verification is unavailable")
    api = f"https://api.github.com/repos/{repo}"
    head, base = pull.get("head"), pull.get("base")
    if not (type(pull.get("id")) is int and pull["id"] > 0
            and type(pull.get("number")) is int and pull["number"] == number
            and pull.get("url") == f"https://api.github.com/{endpoint}"
            and pull.get("html_url") == f"https://github.com/{repo}/pull/{number}"
            and pull.get("state") == "closed" and pull.get("merged") is True and _merged_at(pull.get("merged_at"))
            and _sha(pull.get("merge_commit_sha")) and isinstance(head, dict) and isinstance(base, dict)
            and head.get("ref") == job["branch"] == local["branch"] and head.get("sha") == local["candidate_sha"]
            and base.get("ref") == cfg["base_branch"] and _sha(base.get("sha"))
            and all(_same_repository(side.get("repo"), repo, api) and side["repo"]["id"] == repository_id
                    for side in (head, base))):
        raise F.FactoryError("GitHub PR does not verify the exact merged candidate and base")
    candidate, merge = local["candidate_sha"], pull["merge_commit_sha"]
    association = _association(root, job, repo, repository_id, pull, candidate)
    candidate_identity = _local_merge(root, candidate, merge, local["base_sha"])
    _verify_live_base(root, repo, base["ref"], local["base_sha"])
    if (_origin(root) != repo or _commit(root, f"refs/heads/{local['branch']}") != candidate
            or _commit(root, local["base_ref"]) != local["base_sha"]):
        raise F.FactoryError("origin or refs changed during merge verification")
    return {"repository": repo, "repository_id": repository_id, "pr": number, "pr_id": pull["id"],
            "candidate_sha": candidate, "base_ref": base["ref"], "base_sha": local["base_sha"],
            "merge_commit_sha": merge, "job_pr_association": association, "candidate_merge_identity": candidate_identity}
