"""Locking checks for route and gate publication."""
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'factory' / 'lib'))
import factory as F


def script(name):
    loader = importlib.machinery.SourceFileLoader('locking_' + name, str(ROOT / 'factory' / 'bin' / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RouteLocking(unittest.TestCase):
    def test_active_operation_refuses_route_without_changing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / 'factory' / 'jobs' / '001-test'
            d.mkdir(parents=True)
            job = F.new_job('001-test', 1, 'test')
            job.update(risk='low', surfaces=[])
            F.write_job(d, job)
            before = (d / 'job.json').read_bytes()
            route = script('route')
            with patch.object(F, 'repo_root', return_value=root), patch.object(F, 'load_config', return_value={'routing': {'low': {'review': 'model'}}}), patch.object(F, 'require_admission', side_effect=F.FactoryError('active'), create=True):
                self.assertEqual(route.main(['001-test']), 1)
            self.assertEqual((d / 'job.json').read_bytes(), before)

# Reuse the existing gate fixture, which runs the actual entry point with
# deterministic Git/check results.
from tests.test_factory_phases import TestGateCandidateEvidence as GateFixture


class GatePublication(GateFixture):
    def test_interrupted_state_write_leaves_blocking_marker(self):
        with patch.object(F, 'write_job', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.run_gate('', '')
        self.assertTrue((self.d / '.gate-publication.pending').exists())
        with F.job_mutex(self.d):
            with self.assertRaises(F.FactoryError):
                F.require_admission(self.d)

    def test_round_directory_symlink_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.d / 'round-1').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_gate('', '')[0], 1)
        self.assertFalse((outside / 'checks.md').exists())

    def test_concurrent_job_change_rejects_stale_gate(self):
        def git(cmd, **kwargs):
            if cmd[1] == 'status' or cmd[1] == 'fetch':
                return ''
            if cmd[1] == 'rev-parse':
                return 'candidate\n' if cmd[-1] == 'HEAD^{commit}' else 'base\n'
            self.fail(f'unexpected git command: {cmd}')

        def checks(*args, **kwargs):
            changed = F.read_job(self.d)
            changed['reviewers'] = {'other': 'model'}
            F.write_job(self.d, changed)
            return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

        with patch.object(F, 'run', side_effect=git), patch.object(self.gate.subprocess, 'run', side_effect=checks):
            self.assertEqual(self.gate.main(['001-gate']), 1)
        self.assertFalse((self.d / 'round-1' / 'checks.md').exists())
        self.assertEqual(F.read_job(self.d)['stage'], 'gate')
