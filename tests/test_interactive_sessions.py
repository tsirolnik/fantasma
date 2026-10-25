import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from memory.interactive_sessions import InteractiveSessionStore
from agents.interactive import InteractiveSession
from profiles.loader import load_profile


class SessionStoreTests(unittest.TestCase):
    def test_atomic_save_listing_and_single_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            store=InteractiveSessionStore(directory)
            identifier=store.acquire()
            store.save({'id':identifier,'profile':'interactive','scope':['example.test']})
            self.assertEqual(store.load('latest')['scope'],['example.test'])
            self.assertEqual(store.list()[0]['id'],identifier)
            self.assertEqual(store.path(identifier).stat().st_mode & 0o777,0o600)
            other=InteractiveSessionStore(directory)
            with self.assertRaisesRegex(ValueError,'already open'):other.acquire(identifier)
            store.close()
            other.acquire(identifier)
            other.close()
            Path(directory,'broken.json').write_text('oops')
            self.assertEqual(len(store.list()),1)
            with self.assertRaises(ValueError):store.load('../outside')


class InteractivePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        profile=load_profile('interactive')
        return InteractiveSession(profile,profile.llm,SimpleNamespace(budget=None),[],lambda text: None)

    async def test_round_trip_history_scope_memory_cost_and_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            store=InteractiveSessionStore(directory)
            identifier=store.acquire()
            session=self.session()
            session.attach_store(store)
            await session.handle('/scope example.test')
            session.saved_history=[{'role':'user','content':'hi'},{'role':'assistant','content':'hello'}]
            session.pending_request='inspect the login'
            session.session_status='interrupted'
            session.cost.add_cost('llm_api',.25)
            session.budget=3
            await session.memory.post('pentester','note','test','note:key','observed login')
            session.save()
            store.close()
            store.acquire(identifier)
            restored=self.session()
            with patch('agents.interactive.create_llm_for_agent') as factory:
                restored.attach_store(store,store.load(identifier))
                factory.assert_not_called()
            self.assertEqual(restored.scope,['example.test'])
            self.assertEqual(restored.saved_history,session.saved_history)
            self.assertEqual(restored.cost.get_total(),.25)
            self.assertEqual(restored.budget,3)
            self.assertEqual(restored.pending_request,'inspect the login')
            self.assertEqual(len(restored.memory._entries),1)
            self.assertIsNone(restored.task)
            self.assertIsNone(restored.agent)
            restored.ask=AsyncMock()
            await restored.handle('/continue')
            await restored.task
            self.assertIn('inspect the login',restored.ask.call_args.args[0])
            store.close()

    async def test_success_clears_pending_request_and_saves_completed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store=InteractiveSessionStore(directory);store.acquire()
            session=self.session();session.attach_store(store)
            history=[{'role':'assistant','content':'answer'}]
            session.agent=SimpleNamespace(history=history,execute=AsyncMock(return_value=SimpleNamespace(success=True,reflection='answer',data={})))
            session.ensure_agent=AsyncMock()
            session.start_request('question')
            await session.task
            data=store.load(store.session_id)
            self.assertEqual(data['pending_request'],'')
            self.assertEqual(data['history'],history)
            store.close()

    async def test_sessions_command_is_offline(self):
        from main import async_main,build_parser
        with patch('main.Orchestrator') as orchestrator, patch('commands.sessions.InteractiveSessionStore') as store:
            store.return_value.list.return_value=[]
            await async_main(build_parser().parse_args(['sessions']))
            orchestrator.assert_not_called()
