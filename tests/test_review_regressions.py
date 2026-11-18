import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from brahma import engine
from brahma.config import Settings
from brahma.contracts import prepare_contract
from brahma.discovery import discover
from brahma.models import Plan, Result, Task
from test_core import init_repo


class ReviewedCases(unittest.IsolatedAsyncioTestCase):
    async def test_planning_routes_around_known_wait_without_calling_blocked_provider(self):
        proposal = Plan('Small', [Task('One', 'Implement', ['one.py'], ['Works'])])
        async def gate(provider):
            return {'state': 'waiting' if provider == 'codex' else 'available', 'reason': 'known quota', 'next_at': None}
        with patch.object(engine, '_context', return_value='repository instructions'), patch('brahma.providers.run', AsyncMock(return_value=Result('claude', 'ok', json.dumps(proposal.to_dict())))) as run:
            await engine.plan('Implement', Path('/repo'), Settings(), lambda e: None, asyncio.Event(), provider_gate=gate)
        self.assertEqual(run.await_args.args[0], 'claude')

    async def test_provider_definitions_require_pinning_and_reviewer_sees_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp)/'repo')
            skill = repo/'.agents/skills/example/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: example\n---\nPreserve all unresolved findings.\n')
            catalog = discover(repo, Path(tmp)/'empty-home')
            task = Task('One', 'Implement', ['first.txt'], ['Works'], allowed_skills=['codex/skill/example'])
            plan = Plan('Goal', [task])
            with self.assertRaisesRegex(ValueError, 'pin'):
                prepare_contract(plan, repo, definitions=catalog.entries)
            task.provider = 'claude'
            with self.assertRaisesRegex(ValueError, 'pin'):
                prepare_contract(plan, repo, definitions=catalog.entries)
            task.provider = 'codex'
            prepare_contract(plan, repo, definitions=catalog.entries)
            settings = Settings()
            settings._definition_catalog = catalog.entries
            with patch.object(engine, '_context', return_value='repository instructions'):
                referenced = engine._task_context(repo, task, settings, 'claude', 'reviewer')
                self.assertIn('not invoked by this reviewer', referenced)
                self.assertIn('Preserve all unresolved findings', referenced)
                with self.assertRaisesRegex(ValueError, 'cannot load'):
                    engine._task_context(repo, task, settings, 'claude', 'builder')

    async def test_service_shutdown_preserves_stored_wait_instead_of_unparking(self):
        from types import SimpleNamespace
        from brahma.scheduler import _requeue_interrupted
        from brahma.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state')
            try:
                key = store.create(Path(tmp))
                store.set(key, status='waiting')
                store.enqueue(key, 'generation', 'digest', None, 'Reset unknown')
                session = SimpleNamespace(key=key, generation='generation')
                _requeue_interrupted(store, session, {'digest': 'digest'})
                self.assertIsNone(store.pending()[0]['next_at'])
                self.assertEqual(store.pending()[0]['reason'], 'Reset unknown')
            finally:
                store.close()
