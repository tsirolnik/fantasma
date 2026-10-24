import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents.interactive import InteractivePentester, InteractiveSession
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory
from profiles.loader import load_profile


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_answers_return_control_and_history_survives(self):
        class Model:
            supports_tool_use = True
            model = 'fake'
            def __init__(self):
                self.requests = []
            async def chat_with_tools(self, messages, tools, **kwargs):
                self.requests.append(copy.deepcopy(messages))
                return {'content': [{'type': 'text', 'text': 'Ready to help.'}], 'usage': {}}
        model = Model()
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=model,
                                     shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        agent.in_scope = []
        agent._allowed_domains = set()
        first = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Explain your approach'))
        self.assertTrue(first.success)
        self.assertEqual(agent.last_trace.iterations, 1)
        second = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='What did I ask?'))
        self.assertTrue(second.success)
        self.assertIn('Explain your approach', str(model.requests[1]))
        self.assertIn('Ready to help.', str(model.requests[1]))
        self.assertIn('No target scope', agent._check_scope_claim('http_fetch', {'url': 'http://localhost'}))

    async def test_default_command_selects_interactive_without_lab_construction(self):
        from main import build_parser
        from commands.full import run
        for flags in ([], ['--profile', 'interactive']):
            with patch('agents.interactive.run_interactive', new_callable=AsyncMock) as runner, \
                 patch('agents.pipeline.AgentPipeline') as lab:
                await run(build_parser().parse_args(['full', *flags]), None)
                self.assertEqual(runner.call_args.args[0].name, 'interactive')
                lab.assert_not_called()

    async def test_idle_controls_do_not_construct_agent_or_call_provider(self):
        profile = load_profile('interactive')
        output = []
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(targets=None, budget=1), [], output.append)
        with patch('agents.interactive.create_llm_for_agent') as factory:
            await session.handle('/scope localhost')
            await session.handle('/status')
            await session.handle('/quit')
            factory.assert_not_called()
        self.assertEqual(session.scope, ['localhost'])
        self.assertIsNone(session.agent)

    async def test_stop_cancels_work_and_next_request_is_possible(self):
        profile = load_profile('interactive')
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(targets=None, budget=1), [], lambda text: None)
        started = asyncio.Event()
        finished = asyncio.Event()
        async def work(text):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        session.ask = work
        await session.handle('Investigate')
        await started.wait()
        await session.handle('/stop')
        self.assertTrue(finished.is_set())
        self.assertIsNone(session.task)
        await session.handle('Next request')
        await session.handle('/quit')
        self.assertIsNone(session.task)

    async def test_redirect_and_scope_change_while_busy(self):
        profile = load_profile('interactive')
        output = []
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(targets='localhost', budget=1), [], output.append)
        session.agent = Mock()
        session.agent._directive_queue = asyncio.Queue()
        session.agent._sticky_directives = []
        session.task = asyncio.create_task(asyncio.sleep(60))
        await session.handle('Focus on authentication')
        session.agent.inject_directive.assert_called_once_with('Focus on authentication')
        await session.handle('/scope other.test')
        self.assertEqual(session.scope, ['localhost'])
        await session.stop()
