"""Disposable native CPython cache fixtures for the retirement content validator."""
import hashlib
import importlib.machinery
import os
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory/lib"))
import factory as F
import retirement_bytecode as bytecode


class BytecodeProofTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "checkout"
        self.pkg = self.root / "pkg"
        self.pkg.mkdir(parents=True)
        self.src = self.pkg / "example.py"
        self.src.write_bytes(b"# coding: latin-1\n\"caf\xe9\"\nraise RuntimeError('must not run')\n")
        self.cache = self.pkg / "__pycache__"
        self.tree = {b"pkg/example.py": (b"100644", self.oid(self.src.read_bytes()))}

    @staticmethod
    def oid(raw):
        return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest().encode()

    def inspect(self, file, original=None):
        with_fd = os.open(self.cache, os.O_DIRECTORY)
        try:
            return bytecode.inspect_bytecode(with_fd, os.fsencode(file.name),
                                             os.fsencode(file.relative_to(self.root)),
                                             self.root, original or self.root, self.tree, "sha1")
        finally:
            os.close(with_fd)

    def compile_cache(self, mode=py_compile.PycInvalidationMode.TIMESTAMP, **kwargs):
        return Path(py_compile.compile(str(self.src), doraise=True, invalidation_mode=mode, **kwargs))

    def loader_cache(self, name):
        loader = importlib.machinery.SourceFileLoader(name, str(self.src))
        previous = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = False
            loader.get_code(name)
        finally:
            sys.dont_write_bytecode = previous
        file, = self.cache.iterdir()
        return file

    def test_standard_importlib_loader_cache_is_reproducible_after_parking(self):
        file = self.loader_cache("example")
        expected = file.read_bytes()
        original = self.root
        parked = self.root.with_name("parked")
        self.root.rename(parked)
        self.root, self.cache = parked, parked / "pkg/__pycache__"
        proof = self.inspect(self.cache / file.name, original)
        self.assertEqual(proof["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(proof["size"], len(expected))

    def test_checked_and_unchecked_hash_and_optimize_levels_match_tracked_source(self):
        for mode in (py_compile.PycInvalidationMode.CHECKED_HASH, py_compile.PycInvalidationMode.UNCHECKED_HASH):
            for level in (0, 1, 2):
                with self.subTest(mode=mode, level=level):
                    file = self.compile_cache(mode, optimize=level, dfile="pkg/example.py")
                    self.assertEqual(self.inspect(file)["size"], file.stat().st_size)

    def test_refuses_modified_payload_header_filename_or_source(self):
        file = self.compile_cache(py_compile.PycInvalidationMode.CHECKED_HASH)
        raw = file.read_bytes()
        for content in (raw + b"extra", raw[:4] + (2).to_bytes(4, "little") + raw[8:],
                        raw[:9] + bytes([raw[9] ^ 1]) + raw[10:], raw[:20], b"x"*16):
            with self.subTest(content=content[:16]):
                file.write_bytes(content)
                with self.assertRaises(F.FactoryError):
                    self.inspect(file)
        file.write_bytes(raw)
        other = file.with_name("example.cpython-999.pyc")
        file.rename(other)
        with self.assertRaises(F.FactoryError):
            self.inspect(other)
        other.rename(file)
        self.src.write_bytes(b"locally edited\n")
        with self.assertRaises(F.FactoryError):
            self.inspect(file)

    def test_refuses_hardlinks_symlinks_and_extra_writable_or_executable_cache(self):
        file = self.compile_cache()
        extra = self.root / "link"
        os.link(file, extra)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        extra.unlink()
        file.chmod(0o666)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        file.chmod(0o755)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        file.rename(extra)
        file.symlink_to(extra)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)

    def test_source_must_be_a_regular_tracked_blob(self):
        file = self.compile_cache()
        outside = self.root.parent / "original"
        self.src.rename(outside)
        self.src.symlink_to(outside)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        self.src.unlink()
        outside.rename(self.src)
        self.tree.clear()
        with self.assertRaises(F.FactoryError):
            self.inspect(file)

    def test_empty_tracked_package_initializer_is_reproducible(self):
        self.src = self.pkg / "__init__.py"
        self.src.write_bytes(b"")
        self.tree = {b"pkg/__init__.py": (b"100644", self.oid(b""))}
        file = self.loader_cache("pkg")
        self.assertEqual(self.inspect(file)["size"], file.stat().st_size)

    def test_stale_timestamp_and_out_of_scope_embedded_filename_are_refused(self):
        file = self.compile_cache()
        before = self.src.stat()
        os.utime(self.src, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        file = self.compile_cache(dfile="/outside/with-the-same-name.py")
        with self.assertRaises(F.FactoryError):
            self.inspect(file)

    def test_replacement_during_proof_is_detected(self):
        from unittest.mock import patch
        file = self.compile_cache()

        original = bytecode._reproduces
        def replace(*args):
            replacement = self.root / "new"
            replacement.write_bytes(file.read_bytes())
            replacement.replace(file)
            return original(*args)

        with patch.object(bytecode, "_reproduces", side_effect=replace):
            with self.assertRaises(F.FactoryError):
                self.inspect(file)

    def test_size_limits_and_private_cache_attributes_are_refused(self):
        import subprocess
        file = self.compile_cache()
        if hasattr(os, "setxattr"):
            os.setxattr(file, "user.retirement-test", b"private")
        else:
            subprocess.run(["/usr/bin/xattr", "-w", "user.retirement-test", "private", str(file)], check=True)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        file.unlink()
        file = self.compile_cache()
        with file.open("r+b") as stream:
            stream.truncate(bytecode.MAX_PYC_BYTES + 1)
        with self.assertRaises(F.FactoryError):
            self.inspect(file)
        self.src.write_bytes(b"#" * (bytecode.MAX_SOURCE_BYTES + 1))
        self.tree[b"pkg/example.py"] = (b"100644", self.oid(self.src.read_bytes()))
        file = self.compile_cache()
        with self.assertRaises(F.FactoryError):
            self.inspect(file)

    def test_source_larger_than_pipe_buffer_reproduces(self):
        self.src.write_bytes(b"#" + b" " * (256 * 1024) + b"\nx = 1\n")
        self.tree[b"pkg/example.py"] = (b"100644", self.oid(self.src.read_bytes()))
        file = self.loader_cache("example")
        self.assertEqual(self.inspect(file)["size"], file.stat().st_size)
