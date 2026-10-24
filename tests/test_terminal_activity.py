import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from utils.terminal_activity import TerminalActivity, tool_label


class ActivityTests(unittest.TestCase):
    def test_rotation_parallel_tools_and_reset(self):
        output = []
        activity = TerminalActivity(output.append)
        with patch('utils.terminal_activity.time.monotonic', return_value=10):
            activity.begin()
            first = activity.toolbar()
        with patch('utils.terminal_activity.time.monotonic', return_value=15):
            self.assertNotEqual(first, activity.toolbar())
        activity.tool_started('agent', 'http_fetch', {})
        activity.tool_started('agent', 'http_fetch', {})
        call = SimpleNamespace(tool_name='http_fetch', input={}, duration_ms=250, error=None, output={})
        activity.tool_finished(call)
        self.assertIn('Running http_fetch', activity.toolbar())
        activity.tool_finished(call)
        self.assertNotIn('Running', activity.toolbar())
        call.error = 'private error text'
        activity.tool_finished(call)
        self.assertIn('failed', output[-1])
        self.assertNotIn('private', output[-1])
        activity.finish()
        self.assertIn('Ready', activity.toolbar())

    def test_preview_omits_credentials_and_control_sequences(self):
        label = tool_label('http_fetch\n', {'url': 'https://user:password@example.test/path?token=secret',
                                           'headers': {'Authorization': 'secret'}})
        self.assertEqual(label, 'http_fetch · example.test')


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_redraw_preserves_partially_typed_input(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agents.interactive import terminal_lines
        activity = TerminalActivity(lambda text: None)
        activity.begin()
        with create_pipe_input() as keyboard:
            prompt = PromptSession(input=keyboard, output=DummyOutput(),
                                   bottom_toolbar=activity.toolbar, refresh_interval=0.01)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('prompt_toolkit.PromptSession', return_value=prompt):
                lines = terminal_lines(SimpleNamespace(activity=activity))
                read = asyncio.create_task(anext(lines))
                keyboard.send_text('keep my ')
                for _ in range(100):
                    if prompt.default_buffer.text == 'keep my ':
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(prompt.default_buffer.text, 'keep my ')
                activity.tool_started('agent', 'http_fetch', {})
                prompt.app.invalidate()
                await asyncio.sleep(0.03)
                self.assertEqual(prompt.default_buffer.text, 'keep my ')
                keyboard.send_text('draft\n')
                self.assertEqual(await asyncio.wait_for(read, 2), 'keep my draft')
                await lines.aclose()
