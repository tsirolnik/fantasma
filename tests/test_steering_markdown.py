import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
from agents.interactive import InteractiveSession
from profiles.loader import load_profile
from utils.terminal_markdown import MarkdownMessage, terminal_write


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        profile=load_profile('interactive')
        return InteractiveSession(profile,profile.llm,SimpleNamespace(budget=1),[],lambda text: None)

    async def test_steering_during_startup_is_queued_and_stop_clears_it(self):
        session=self.session()
        session.task=asyncio.create_task(asyncio.sleep(30))
        await session.handle('/steer focus on login')
        self.assertEqual(session.pending_steering,['focus on login'])
        self.assertFalse(session.task.done())
        await session.handle('/stop')
        self.assertEqual(session.pending_steering,[])

    async def test_startup_steering_reaches_agent_before_execution(self):
        session=self.session()
        release=asyncio.Event()
        directives=[]
        async def execute(assignment):
            self.assertEqual(directives,['focus on login'])
            return SimpleNamespace(success=True,reflection='Ready',data={})
        async def ensure():
            await release.wait()
            session.agent=SimpleNamespace(inject_directive=directives.append,execute=execute)
        session.ensure_agent=ensure
        await session.handle('investigate')
        await session.handle('/steer focus on login')
        release.set()
        await session.task
        self.assertEqual(session.pending_steering,[])

    async def test_force_waits_for_cancellation_before_replacement(self):
        session=self.session()
        started=asyncio.Event()
        order=[]
        async def request(text):
            order.append(text)
            if text=='original':
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append('old finished')
        session.ask=request
        await session.handle('original')
        await started.wait()
        await session.handle('/interrupt summarize instead')
        await session.task
        self.assertEqual(order,['original','old finished','summarize instead'])
        await session.stop()

    async def test_plain_text_and_explicit_steer_do_not_cancel(self):
        session=self.session()
        session.agent=SimpleNamespace(inject_directive=Mock(),_sticky_directives=[],_directive_queue=asyncio.Queue())
        session.task=asyncio.create_task(asyncio.sleep(30))
        await session.handle('/steer login only')
        await session.handle('avoid logout')
        self.assertEqual([call.args[0] for call in session.agent.inject_directive.call_args_list],['login only','avoid logout'])
        self.assertFalse(session.task.done())
        await session.stop()

    async def test_empty_interrupt_does_not_stop(self):
        session=self.session()
        session.task=asyncio.create_task(asyncio.sleep(30))
        await session.handle('/interrupt')
        self.assertFalse(session.task.done())
        await session.stop()


class MarkdownTests(unittest.TestCase):
    def test_terminal_renders_markdown_and_pipe_keeps_source(self):
        class Terminal(io.StringIO):
            def isatty(self): return True
        source=MarkdownMessage('## Findings\n\n**Ready**\n\n```python\nprint(1)\n```')
        terminal=Terminal()
        with patch('sys.stdout',terminal):terminal_write(source)
        self.assertNotIn('## Findings',terminal.getvalue())
        self.assertNotIn('```python',terminal.getvalue())
        self.assertIn('Findings',terminal.getvalue())
        pipe=io.StringIO()
        with patch('sys.stdout',pipe):terminal_write(source)
        self.assertEqual(pipe.getvalue(),str(source)+'\n')

    def test_untrusted_terminal_controls_are_removed(self):
        pipe=io.StringIO()
        with patch('sys.stdout',pipe):terminal_write('hello\x1b[2Jworld')
        self.assertEqual(pipe.getvalue(),'helloworld\n')
