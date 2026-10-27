import asyncio
import unittest
from types import SimpleNamespace
from agents.interactive import InteractiveSession, InteractivePentester
from agents.messaging import MessageBus
from memory.shared import SharedMemory
from profiles.loader import load_profile


class RequestScopeTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        profile = load_profile('interactive')
        self.output = []
        return InteractiveSession(profile, profile.llm, SimpleNamespace(budget=None), [], self.output.append)

    async def test_approval_waits_and_updates_all_scope_guards(self):
        session = self.session()
        session.agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=None,
            shared_memory=session.memory, methodology=session.profile.methodology)
        session.agent.on_scope_request = session.request_scope
        session.set_scope(['existing.test'])
        tool = session.agent.tools.get('request_scope')
        task = asyncio.create_task(tool.handler({'hosts': ['new.test'], 'reason': 'User requested this host'}))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(session.scope, ['existing.test'])
        self.assertIn('/scope approve', self.output[-1])
        await session.handle('/scope approve')
        self.assertEqual((await task)['status'], 'approved')
        self.assertEqual(session.scope, ['existing.test', 'new.test'])
        self.assertEqual(session.agent._allowed_domains, set(session.scope))
        self.assertEqual(session.memory.allowed_domains, set(session.scope))
        self.assertIsNone(session.scope_request)

    async def test_denial_cancellation_and_no_stale_approval(self):
        session = self.session()
        data = {'hosts': ['new.test'], 'reason': 'Needed'}
        task = asyncio.create_task(session.request_scope(data))
        await asyncio.sleep(0)
        other = await session.request_scope({'hosts': ['other.test'], 'reason': 'Also needed'})
        self.assertEqual(other['status'], 'pending')
        await session.handle('/scope deny')
        self.assertEqual((await task)['status'], 'denied')
        self.assertEqual(session.scope, [])
        session.task = asyncio.create_task(session.request_scope(data))
        await asyncio.sleep(0)
        await session.stop()
        self.assertIsNone(session.scope_request)
        await session.handle('/scope approve')
        self.assertEqual(session.scope, [])
        self.assertEqual(self.output[-1], 'No pending scope request.')

    async def test_invalid_and_already_authorized(self):
        session = self.session()
        for hosts in ([], ['https://example.test/path'], ['a.test,b.test'], [1], ['\x1b[31mfoo']):
            self.assertEqual((await session.request_scope({'hosts': hosts, 'reason': 'Needed'}))['status'], 'invalid')
        session.set_scope(['existing.test'])
        result = await session.request_scope({'hosts': ['existing.test'], 'reason': 'Needed'})
        self.assertEqual(result['status'], 'already_authorized')
        self.assertIsNone(session.scope_request)
