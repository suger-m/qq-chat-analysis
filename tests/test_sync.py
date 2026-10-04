"""Snapshot publication regression; no QQ process or real account is used."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


RUNTIME = Path(__file__).resolve().parents[1] / 'scripts/runtime'
ACCOUNT = '112233445'
sys.path.insert(0, str(RUNTIME))
import qqlib

# Run sync_latest in separate interpreters so a threading-only lock cannot pass.
WORKER = r'''
import json, pathlib, subprocess, sys, time
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import qqlib
root = pathlib.Path(sys.argv[2])
name = sys.argv[3]

def wait_for(filename):
    deadline = time.monotonic() + 10
    while not (root / filename).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(filename)
        time.sleep(0.02)

def decrypt(args, **kwargs):
    target = pathlib.Path(args[args.index('--out-dir') + 1])
    (root / (name + '-decrypt')).write_text(str(target), encoding='utf-8')
    if name == 'first':
        wait_for('release-first')
    else:
        wait_for('first-done')
    (target / 'nt_msg.db').write_bytes(name.encode('ascii'))
    return SimpleNamespace(returncode=0, stdout='', stderr='')

subprocess.run = decrypt
(root / (name + '-ready')).touch()
try:
    ok, message = qqlib.sync_latest('112233445')
    result = {'ok': ok, 'message': message}
except Exception as exc:
    result = {'ok': False, 'exception': type(exc).__name__}
finally:
    if name == 'first':
        (root / 'first-done').touch()
print(json.dumps(result, ensure_ascii=True))
'''


def wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


class ConcurrentSyncTests(unittest.TestCase):
    def test_overlapping_processes_do_not_remove_each_others_output(self):
        with tempfile.TemporaryDirectory(prefix='qq-sync-test-') as folder:
            root = Path(folder)
            existing = root / 'output' / ACCOUNT
            existing.mkdir(parents=True)
            (existing / 'nt_msg.db').write_bytes(b'old')
            env = dict(os.environ, QQ_DATA_DIR=str(root),
                       PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            processes = []
            try:
                first = subprocess.Popen(
                    [sys.executable, '-c', WORKER, str(RUNTIME), str(root), 'first'],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8')
                processes.append(first)
                self.assertTrue(wait_for(root / 'first-decrypt'))
                second = subprocess.Popen(
                    [sys.executable, '-c', WORKER, str(RUNTIME), str(root), 'second'],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8')
                processes.append(second)
                self.assertTrue(wait_for(root / 'second-ready'))
                # The old code enters immediately; the fix must wait for first.
                second_entered_early = wait_for(root / 'second-decrypt', timeout=1)
                (root / 'release-first').touch()
                outcomes = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    outcomes.append(json.loads(stdout))
                self.assertTrue(all(item['ok'] for item in outcomes), outcomes)
                self.assertFalse(second_entered_early,
                                 'Two processes entered snapshot publication together')
                paths = [(root / (name + '-decrypt')).read_text(encoding='utf-8')
                         for name in ('first', 'second')]
                self.assertNotEqual(*paths)
                self.assertEqual((existing / 'nt_msg.db').read_bytes(), b'second')
            finally:
                (root / 'release-first').touch()
                (root / 'first-done').touch()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_lock_is_released_when_a_sync_process_is_killed(self):
        with tempfile.TemporaryDirectory(prefix='qq-sync-test-') as folder:
            root = Path(folder)
            env = dict(os.environ, QQ_DATA_DIR=str(root),
                       PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            first = subprocess.Popen(
                [sys.executable, '-c', WORKER, str(RUNTIME), str(root), 'first'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8')
            try:
                self.assertTrue(wait_for(root / 'first-decrypt'))
            finally:
                first.kill()
                first.communicate(timeout=5)
            (root / 'first-done').touch()
            second = subprocess.Popen(
                [sys.executable, '-c', WORKER, str(RUNTIME), str(root), 'second'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8')
            try:
                stdout, stderr = second.communicate(timeout=10)
                self.assertEqual(second.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)['ok'])
                snapshot = root / 'output' / ACCOUNT / 'nt_msg.db'
                self.assertEqual(snapshot.read_bytes(), b'second')
            finally:
                if second.poll() is None:
                    second.kill()
                second.communicate()


class SnapshotFailureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='qq-sync-test-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.output = self.root / 'output'
        self.snapshot = self.output / ACCOUNT
        self.snapshot.mkdir(parents=True)
        (self.snapshot / 'nt_msg.db').write_bytes(b'old')
        for name, value in [('DATA_DIR', str(self.root)),
                            ('OUTPUT_DIR', str(self.output)),
                            ('_MEMO', {'sentinel': 'old'})]:
            patcher = patch.object(qqlib, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def decrypt(args, **kwargs):
        target = Path(args[args.index('--out-dir') + 1])
        (target / 'nt_msg.db').write_bytes(b'new')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def assert_old_snapshot(self):
        self.assertEqual((self.snapshot / 'nt_msg.db').read_bytes(), b'old')
        self.assertEqual(list(self.output.glob('.tmp_*')), [])
        self.assertEqual(qqlib._MEMO, {'sentinel': 'old'})

    def test_decrypt_failure_preserves_snapshot_and_allows_retry(self):
        failed = SimpleNamespace(returncode=1, stdout='', stderr='simulated failure')
        with patch('subprocess.run', return_value=failed):
            self.assertFalse(qqlib.sync_latest(ACCOUNT)[0])
        self.assert_old_snapshot()
        with patch('subprocess.run', side_effect=self.decrypt):
            self.assertTrue(qqlib.sync_latest(ACCOUNT)[0])
        self.assertEqual((self.snapshot / 'nt_msg.db').read_bytes(), b'new')
        self.assertEqual(qqlib._MEMO, {})

    def test_subprocess_timeout_cleans_only_its_own_staging(self):
        unrelated = self.output / '.tmp_other_run'
        unrelated.mkdir()
        sentinel = unrelated / 'nt_msg.db'
        sentinel.write_bytes(b'other')
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('decrypt', 900)):
            ok, message = qqlib.sync_latest(ACCOUNT)
        self.assertFalse(ok)
        self.assertIn('900s', message)
        self.assertEqual((self.snapshot / 'nt_msg.db').read_bytes(), b'old')
        self.assertEqual(sentinel.read_bytes(), b'other')
        self.assertEqual(list(self.output.glob('.tmp_*')), [unrelated])

    def test_subprocess_launch_error_preserves_snapshot(self):
        with patch('subprocess.run', side_effect=FileNotFoundError('simulated executable')):
            self.assertFalse(qqlib.sync_latest(ACCOUNT)[0])
        self.assert_old_snapshot()

    def test_failed_publication_restores_the_previous_snapshot(self):
        real_replace = os.replace

        def replace(source, target):
            if Path(source).name.startswith('.tmp_'):
                raise PermissionError('simulated publication failure')
            return real_replace(source, target)

        with patch('subprocess.run', side_effect=self.decrypt), \
             patch('os.replace', side_effect=replace):
            self.assertFalse(qqlib.sync_latest(ACCOUNT)[0])
        self.assert_old_snapshot()
        self.assertFalse(Path(str(self.snapshot) + '.old').exists())

    def test_open_snapshot_error_returns_failure_without_destroying_data(self):
        with patch('subprocess.run', side_effect=self.decrypt), \
             patch('os.replace', side_effect=PermissionError('simulated open database')):
            self.assertFalse(qqlib.sync_latest(ACCOUNT)[0])
        self.assert_old_snapshot()


if __name__ == '__main__':
    unittest.main()
