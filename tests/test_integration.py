import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from brahma.discovery import Catalog
from brahma.models import Plan, Result, Task
from brahma.quota import normalize_claude, normalize_codex
from brahma.scheduler import tick
from brahma.session import Session
from brahma.store import Store
from test_core import init_repo


class ApprovedQueue(unittest.IsolatedAsyncioTestCase):
    def fixture(self, tmp):
        root = init_repo(Path(tmp) / 'repo')
        command = [sys.executable, '-c', "from pathlib import Path; assert Path('first.txt').read_text() == 'implemented\\n'"]
        (root / 'brahma.toml').write_text('[brahma]\nauto_commit=false\nchecks=' + json.dumps([command]))
        store = Store(Path(tmp) / 'state')
        session = Session(store, root)
        session.set_plan(Plan('Implement behavior', [Task('Change', 'Write implemented', ['first.txt'], ['File contains implemented'])]))
        return root, store, session

    async def test_wait_then_weekly_reset_resumes_review_without_rebuilding_or_resetting_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch('brahma.discovery.discover', return_value=Catalog()):
            root, store, session = self.fixture(tmp)
            reset = time.time() + 7 * 24 * 3600
            store.observe(normalize_claude({'status': 'rejected', 'resetsAt': reset}))
            codex = normalize_codex({'rateLimits': {'primary': {'usedPercent': 5, 'windowDurationMins': 300, 'resetsAt': reset}}})
            calls = []
            async def run(provider, prompt, cwd, role, settings, emit, cancel, timeout, **kwargs):
                calls.append((provider, role))
                self.assertIn('Shared locked feature', prompt)
                self.assertGreater(timeout, 0)
                if role == 'builder':
                    (cwd / 'first.txt').write_text('implemented\n')
                    return Result(provider, 'ok', 'Edited', session_id='builder-checkpoint')
                return Result(provider, 'ok', json.dumps({'verdict': 'pass', 'summary': 'Verified actual change', 'issues': []}))
            try:
                with patch('brahma.providers.doctor', AsyncMock(return_value={'available': True})), patch('brahma.quota.probe_codex', AsyncMock(return_value=codex)), patch('brahma.providers.run', side_effect=run):
                    result = await session.execute()
                    self.assertEqual(result.status, 'waiting')
                    self.assertEqual(calls, [('codex', 'builder')])
                    checkpoint = [json.loads(e['text']) for e in store.events(session.key) if e['kind'] == 'task_state'][-1]
                    self.assertEqual(checkpoint['phase'], 'review')
                    self.assertFalse(checkpoint['active'])
                    self.assertEqual(checkpoint['next_attempt'], 1)
                    self.assertGreater(checkpoint['remaining_seconds'], 3500)
                    self.assertEqual(store.pending()[0]['next_at'], reset)
                    with patch('time.time', return_value=reset + 1):
                        self.assertTrue(await tick(store, now=reset + 1))
                    self.assertEqual(store.session(session.key)['status'], 'ready')
                    self.assertEqual(calls, [('codex', 'builder'), ('claude', 'reviewer')])
                    self.assertEqual(store.pending(), [])
                    self.assertEqual((root / 'first.txt').read_text(), 'base\n')
            finally:
                store.close()

    async def test_changed_checks_invalidate_approval_before_worker(self):
        with tempfile.TemporaryDirectory() as tmp, patch('brahma.discovery.discover', return_value=Catalog()):
            root, store, session = self.fixture(tmp)
            try:
                with patch('brahma.providers.run', AsyncMock()) as worker:
                    (root / 'brahma.toml').write_text('[brahma]\nauto_commit=false\nchecks=[["true"]]')
                    with self.assertRaisesRegex(ValueError, 'Contract changed'):
                        await session.execute()
                worker.assert_not_called()
            finally:
                store.close()

    async def test_explicit_stop_cancels_due_queue_and_detach_cannot_reactivate_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch('brahma.discovery.discover', return_value=Catalog()):
            _, store, session = self.fixture(tmp)
            try:
                contract, _, _ = session.approved_contract()
                store.set(session.key, approved_digest=contract['digest'], status='cancelled')
                session.queue_resume()
                self.assertEqual(len(store.pending()), 1)
                session.stop()
                session.queue_resume()
                with patch.object(Session, 'execute', AsyncMock()) as execute:
                    self.assertFalse(await tick(store, now=time.time() + 100))
                execute.assert_not_called()
                self.assertEqual(store.pending(), [])
            finally:
                store.close()
