"""Commands reject invalid transitions before making visible changes."""
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'factory' / 'lib'))
import factory as F

loader = importlib.machinery.SourceFileLoader('preflight_job', str(ROOT / 'factory' / 'bin' / 'job'))
spec = importlib.util.spec_from_loader(loader.name, loader)
jobcmd = importlib.util.module_from_spec(spec)
loader.exec_module(jobcmd)


class PreflightEffects(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.d = self.root / 'factory' / 'jobs' / '001-test'
        self.d.mkdir(parents=True)
        self.cfg = {'labels': {'queue': 'queue', 'needs_human': 'human', 'needs_info': 'info'}}
        self.job = F.new_job('001-test', 1, 'test')
        self.job['pr'] = 42
        (self.d / 'intake.md').write_text('DECISION: REJECT\nRISK: low\nSURFACES: none\n\nReason\n')
        (self.d / 'rejection.md').write_text('old reason\n')

    def write_stage(self, stage):
        self.job['stage'] = stage
        F.write_job(self.d, self.job)
        return (self.d / 'job.json').read_bytes()

    def test_intake_in_wrong_stage_has_no_effects_even_when_transition_is_legal(self):
        before = self.write_stage('needs-info')
        with patch.object(F, 'gh_api') as api, patch.object(F, 'load_config', return_value={}):
            with self.assertRaises(F.FactoryError):
                jobcmd.cmd_intake(self.root, self.cfg, ['001-test'])
        api.assert_not_called()
        self.assertEqual((self.d / 'job.json').read_bytes(), before)
        self.assertEqual((self.d / 'rejection.md').read_text(), 'old reason\n')
        self.assertFalse((self.d / F.OPERATION).exists())

    def test_reject_illegal_transition_has_no_effects(self):
        before = self.write_stage('gate')
        with patch.object(F, 'gh_api') as api, patch('sys.stdin', io.StringIO('new reason')):
            with self.assertRaises(F.FactoryError):
                jobcmd.cmd_reject(self.root, self.cfg, ['001-test'])
        api.assert_not_called()
        self.assertEqual((self.d / 'job.json').read_bytes(), before)
        self.assertEqual((self.d / 'rejection.md').read_text(), 'old reason\n')
        self.assertFalse((self.d / F.OPERATION).exists())

    def test_legal_reject_updates_job_and_artifact(self):
        self.write_stage('ready')
        with patch.object(F, 'gh_api'), patch.object(F, 'load_config', return_value={}), patch('sys.stdin', io.StringIO('new reason')):
            jobcmd.cmd_reject(self.root, self.cfg, ['001-test'])
        self.assertEqual(F.read_job(self.d)['stage'], 'rejected')
        self.assertEqual((self.d / 'rejection.md').read_text(), 'new reason\n')

    def test_legal_intake_updates_job(self):
        self.write_stage('intake')
        with patch.object(F, 'gh_api'), patch.object(F, 'load_config', return_value={}):
            jobcmd.cmd_intake(self.root, self.cfg, ['001-test'])
        self.assertEqual(F.read_job(self.d)['stage'], 'rejected')
