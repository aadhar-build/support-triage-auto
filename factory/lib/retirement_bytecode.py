"""Reproduce one native Python cache from its exact, retained Git source blob."""
import hashlib
import importlib.util
import marshal
import os
import re
import stat
import struct
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path

MAX_SOURCE_BYTES = 1024 * 1024
MAX_PYC_BYTES = 4 * 1024 * 1024
_MEMORY_BYTES = 384 * 1024 * 1024
_WALL_SECONDS = 5


def _compiler_child():
    """Run only with isolated trusted stdlib imports. The supplied body stays opaque."""
    import resource

    def limit(which, ceiling):
        finite = [n for n in resource.getrlimit(which) if n != resource.RLIM_INFINITY]
        maximum = min([ceiling, *finite])
        resource.setrlimit(which, (maximum, maximum))

    limit(resource.RLIMIT_CPU, 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    if sys.platform.startswith("linux"):
        limit(resource.RLIMIT_AS, _MEMORY_BYTES)
    elif sys.platform != "darwin":
        return False
    length, level = map(int, sys.argv[1:3])
    if not (0 <= length <= MAX_SOURCE_BYTES and level in (0, 1, 2) and len(sys.argv) == 5):
        return False
    source = sys.stdin.buffer.read(length)
    body = sys.stdin.buffer.read(MAX_PYC_BYTES + 1)
    if len(source) != length or not 0 < len(body) <= MAX_PYC_BYTES - 16:
        return False
    for filename in sys.argv[3:]:
        # CPython marshal shares objects when their reference counts warrant it.
        # py_compile and the import loader can pass a held or temporary code object.
        held = [compile(source, filename, "exec", dont_inherit=True, optimize=level)]
        if marshal.dumps(held[0]) == body or marshal.dumps(held.pop()) == body:
            return True
    return False


if __name__ == "__main__":
    sys.exit(0 if _compiler_child() else 1)


import factory as F

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


def _refuse():
    raise F.FactoryError("bytecode cache is unsafe or cannot be reproduced from its tracked source")


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _check_entry(parent, name, info):
    if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
        _refuse()


def _bytes(stack, parent, name, limit, owner, executable=False):
    from retirement_git import _require_no_attributes

    fd = os.open(name, _FILE, dir_fd=parent)
    stream = stack.enter_context(os.fdopen(fd, "rb"))
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
            or info.st_size > limit or not mode & stat.S_IRUSR or mode & ~0o755
            or bool(mode & stat.S_IXUSR) != executable or (not executable and mode & 0o111)):
        _refuse()
    _require_no_attributes(fd)
    data = stream.read(limit + 1)
    if len(data) != info.st_size:
        _refuse()
    stack.callback(_check_entry, parent, name, info)
    return data, info


def _darwin_resident_bytes(pid):
    # Darwin's RLIMIT_AS is unsupported. proc_taskinfo begins with two uint64
    # fields: virtual bytes and resident bytes. The parent samples the isolated
    # child's RSS while it compiles, in addition to the CPU and input limits.
    import ctypes

    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    data = (ctypes.c_uint64 * 16)()
    count = library.proc_pidinfo(pid, 4, 0, ctypes.byref(data), ctypes.sizeof(data))
    return data[1] if count >= 16 else None


def _reproduces(source, body, level, filenames):
    command = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
               str(len(source)), str(level), *filenames]
    # Never expose checkout paths on sys.path, inherited dynamic loader/Python
    # environment variables, or controller file descriptors.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          cwd="/", env={}, close_fds=True) as child:
        done = threading.Event()

        def watch_resident_memory():
            while not done.wait(0.02):
                try:
                    resident = _darwin_resident_bytes(child.pid)
                except (OSError, AttributeError):
                    resident = None
                if resident is None or resident > _MEMORY_BYTES:
                    child.kill()
                    return

        watcher = None
        try:
            if sys.platform == "darwin":
                watcher = threading.Thread(target=watch_resident_memory, daemon=True)
                watcher.start()
            child.communicate(source + body, timeout=_WALL_SECONDS)
            return child.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        finally:
            done.set()
            if child.poll() is None:
                child.kill()
            child.wait()
            if watcher is not None:
                watcher.join()


def inspect_bytecode(directory_fd, name_bytes, relative_bytes, checkout, original, tree, algorithm):
    """Compare PEP 552 header and complete native marshalled body without loading it."""
    from retirement_git import _require_no_attributes

    tag = sys.implementation.cache_tag
    if sys.implementation.name != "cpython" or not tag or algorithm not in ("sha1", "sha256"):
        _refuse()
    parts = relative_bytes.split(b"/")
    if (len(parts) < 2 or parts[-2:] != [b"__pycache__", name_bytes]
            or any(part in (b"", b".", b"..", b".git") for part in parts)
            or not checkout.is_absolute() or not original.is_absolute()):
        _refuse()
    filename = re.fullmatch(rb"(.+)\." + re.escape(tag.encode("ascii")) + rb"(?:\.opt-([12]))?\.pyc", name_bytes)
    if filename is None:
        _refuse()
    source_name = filename[1] + b".py"
    source_path = b"/".join([*parts[:-2], source_name])
    tracked = tree.get(source_path)
    if not tracked or tracked[0] not in (b"100644", b"100755"):
        _refuse()
    try:
        with ExitStack() as stack:
            parent = os.open(checkout, _DIR)
            stack.callback(os.close, parent)
            root_info = os.fstat(parent)
            owner = root_info.st_uid
            _require_no_attributes(parent)
            if owner != os.geteuid():
                _refuse()
            stack.callback(_check_entry, None, checkout, root_info)
            for part in parts[:-2]:
                child = os.open(part, _DIR, dir_fd=parent)
                stack.callback(os.close, child)
                info = os.fstat(child)
                if info.st_uid != owner or info.st_dev != root_info.st_dev:
                    _refuse()
                _require_no_attributes(child)
                stack.callback(_check_entry, parent, part, info)
                parent = child
            cache_info = os.fstat(directory_fd)
            if (cache_info.st_uid != owner or cache_info.st_dev != root_info.st_dev
                    or not stat.S_ISDIR(cache_info.st_mode) or stat.S_IMODE(cache_info.st_mode) & ~0o755):
                _refuse()
            _check_entry(parent, b"__pycache__", cache_info)
            stack.callback(_check_entry, parent, b"__pycache__", cache_info)
            _require_no_attributes(directory_fd)
            source, info = _bytes(stack, parent, source_name, MAX_SOURCE_BYTES, owner, tracked[0] == b"100755")
            if hashlib.new(algorithm, b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest().encode() != tracked[1]:
                _refuse()
            raw, cache_file = _bytes(stack, directory_fd, name_bytes, MAX_PYC_BYTES, owner)
            if info.st_dev != root_info.st_dev or cache_file.st_dev != root_info.st_dev:
                _refuse()
            if len(raw) <= 16 or raw[:4] != importlib.util.MAGIC_NUMBER:
                _refuse()
            flags = int.from_bytes(raw[4:8], "little")
            if flags == 0:
                expected = struct.pack("<II", int(info.st_mtime) & 0xffffffff, len(source))
            elif flags in (1, 3):
                expected = importlib.util.source_hash(source)
            else:
                _refuse()
            if raw[8:16] != expected:
                _refuse()
            names = [str(original / os.fsdecode(source_path)), os.fsdecode(source_path)]
            if not _reproduces(source, raw[16:], int(filename[2] or 0), names):
                _refuse()
            return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "identity": _identity(cache_file)}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise F.FactoryError("cannot safely inspect Python bytecode cache") from error
