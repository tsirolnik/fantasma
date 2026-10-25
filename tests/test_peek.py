import asyncio
import unittest
from types import SimpleNamespace
from agents.interactive import InteractiveSession
from agents.tool_agent import ToolCall
from profiles.loader import load_profile
from utils.terminal_activity import TerminalActivity


class PeekTests(unittest.TestCase):
    def test_inspect_running_completed_failed_and_interrupted(self):
        activity = TerminalActivity(lambda text: None)
        activity.begin()
        activity.llm_started('p', 'test-model', 2, 5)
        self.assertIn('Waiting for model test-model', activity.peek())
        activity.tool_started('p', 'http_fetch', {'url': 'https://example.test/a'})
        activity.tool_started('p', 'http_fetch', {'url': 'https://example.test/b'})
        self.assertIn('running', activity.peek(1))
        activity.tool_finished(ToolCall('http_fetch', {'url': 'https://example.test/b'}, output={'status': 200}, duration_ms=250))
        self.assertEqual(activity.calls[0]['status'], 'running')
        self.assertEqual(activity.calls[1]['status'], 'done')
        self.assertIn('200', activity.peek(2))
        activity.finish()
        self.assertIn('interrupted', activity.peek(1))
        self.assertIn('No retained', activity.peek(900))

    def test_previews_are_bounded_and_redact_common_credentials(self):
        preview = TerminalActivity.preview({'Authorization': 'secret-a', 'nested': {'api_key':'secret-b'},
            'url': 'https://user:secret-c@example.test/?token=secret-d',
            'value': 'Bearer secret-e'})
        for secret in ['secret-a','secret-b','secret-c','secret-d','secret-e']:
            self.assertNotIn(secret, preview)
        self.assertLess(len(TerminalActivity.preview('x'*10000)), 4050)

    def test_live_inspector_closes_without_dumping_details(self):
        output=[]
        activity=TerminalActivity(output.append)
        activity.live_inspector=True
        activity.tool_started('p','example',{'detail':'only in panel'})
        output.clear()
        activity.toggle()
        self.assertIn('only in panel',activity.toolbar())
        activity.toggle()
        self.assertNotIn('only in panel',activity.toolbar())
        self.assertEqual(output,[])

    def test_plain_output_close_is_only_a_confirmation(self):
        output=[]
        activity=TerminalActivity(output.append)
        activity.set_inspection(False)
        self.assertEqual(output,['Inspector closed. Compact tool output restored.'])

    def test_inspector_follows_tail_but_respects_manual_scroll(self):
        from unittest.mock import patch
        import os
        activity=TerminalActivity(lambda text: None)
        activity.live_inspector=True
        activity.set_inspection(True)
        with patch('shutil.get_terminal_size', return_value=os.terminal_size((80, 15))), \
             patch.object(activity, 'peek', return_value='\n'.join(f'line {n}' for n in range(50))):
            self.assertIn('line 49',activity.toolbar())
            activity.scroll_inspection(-8)
            offset=activity.inspect_offset
            self.assertFalse(activity.follow_tail)
            self.assertNotIn('line 49',activity.toolbar())
            with patch.object(activity,'peek',return_value='\n'.join(f'line {n}' for n in range(60))):
                activity.toolbar()
                self.assertEqual(activity.inspect_offset,offset)
                activity.follow_tail=True
                self.assertIn('line 59',activity.toolbar())
            activity.set_inspection(False)
            activity.set_inspection(True)
            self.assertTrue(activity.follow_tail)

    def test_long_output_preserves_and_displays_tail(self):
        from unittest.mock import patch
        import os
        activity=TerminalActivity(lambda text: None)
        output='START\n'+('long output '*800)+'FINAL RESULT'
        preview=activity.preview(output)
        self.assertIn('START',preview)
        self.assertTrue(preview.endswith('FINAL RESULT'))
        self.assertLessEqual(len(preview),4000)
        activity.tool_finished(ToolCall('test',{},output=output))
        activity.live_inspector=True
        activity.set_inspection(True)
        with patch('shutil.get_terminal_size', return_value=os.terminal_size((80,24))):
            self.assertIn('FINAL RESULT',activity.toolbar())

    def test_expanded_output_is_opt_in(self):
        out=[]
        activity=TerminalActivity(out.append)
        activity.tool_started('p','example',{'detail':'visible-in-peek'})
        self.assertNotIn('visible-in-peek',''.join(out))
        activity.toggle()
        self.assertIn('visible-in-peek',''.join(out))
        activity.tool_finished(ToolCall('example',{'detail':'visible-in-peek'},error='failed request'))
        self.assertIn('failed request',out[-1])


class PeekCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_peek_does_not_redirect_or_stop_work(self):
        output=[]
        profile=load_profile('interactive')
        session=InteractiveSession(profile,profile.llm,SimpleNamespace(budget=1),[],output.append)
        session.task=asyncio.create_task(asyncio.sleep(30))
        try:
            await session.handle('/peek')
            await session.handle('/peek on')
            self.assertTrue(session.activity.expanded)
            self.assertFalse(session.task.done())
            self.assertIsNone(session.agent)
            await session.handle('/peek off')
            self.assertFalse(session.activity.expanded)
        finally:
            await session.stop()

    async def test_control_o_keeps_the_input_draft(self):
        from unittest.mock import patch
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agents.interactive import terminal_lines
        activity=TerminalActivity(lambda text: None)
        made=[]
        with create_pipe_input() as keyboard:
            def factory(**kwargs):
                prompt=PromptSession(input=keyboard,output=DummyOutput(),**kwargs)
                made.append(prompt)
                return prompt
            with patch('sys.stdin.isatty',return_value=True), patch('sys.stdout.isatty',return_value=True), \
                 patch('prompt_toolkit.PromptSession',side_effect=factory):
                lines=terminal_lines(SimpleNamespace(activity=activity))
                task=asyncio.create_task(anext(lines))
                keyboard.send_text('my draft\x0f')
                for _ in range(100):
                    if activity.expanded:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(activity.expanded)
                self.assertEqual(made[0].default_buffer.text,'my draft')
                keyboard.send_text('\x0f')
                for _ in range(100):
                    if not activity.expanded:
                        break
                    await asyncio.sleep(.01)
                self.assertFalse(activity.expanded)
                self.assertEqual(made[0].default_buffer.text,'my draft')
                keyboard.send_text('\x0f')
                for _ in range(100):
                    if activity.expanded:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(activity.expanded)
                keyboard.send_text('\x1b')
                for _ in range(200):
                    if not activity.expanded:
                        break
                    await asyncio.sleep(.01)
                self.assertFalse(activity.expanded)
                self.assertEqual(made[0].default_buffer.text,'my draft')
                keyboard.send_text('\n')
                self.assertEqual(await asyncio.wait_for(task,2),'my draft')
                await lines.aclose()
