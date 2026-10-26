import asyncio
import unittest
from types import SimpleNamespace
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from agents.interactive import InteractiveSession
from profiles.loader import load_profile
from utils.terminal_console import TerminalConsole
from utils.terminal_markdown import MarkdownMessage


class ScreenOutput(DummyOutput):
    def get_size(self):
        return Size(rows=30, columns=120)


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def console(self, keyboard):
        profile = load_profile('interactive')
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(budget=2), [], lambda text: None)
        console = TerminalConsole(session, input=keyboard, output=ScreenOutput())
        session.write = console.write
        return console

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('Console did not respond to input')

    async def test_keyboard_inspection_multiline_steer_and_stop_keep_draft(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            app = asyncio.create_task(console.app.run_async())
            try:
                await self.until(lambda: console.app.is_running)
                keyboard.send_text('draft\x0f')
                await self.until(lambda: console.session.activity.expanded)
                self.assertEqual(console.composer.text, 'draft')
                keyboard.send_text('\x1b')
                await self.until(lambda: not console.session.activity.expanded)
                keyboard.send_text('\x1b\rsecond line\r')
                text = await asyncio.wait_for(console.queue.get(), 2)
                self.assertEqual(text, 'draft\nsecond line')
                keyboard.send_text('replacement\x18')
                self.assertEqual(await asyncio.wait_for(console.queue.get(), 2), '/interrupt replacement')
                keyboard.send_text('keep me\x03')
                self.assertEqual(await asyncio.wait_for(console.queue.get(), 2), '/stop')
                self.assertEqual(console.composer.text, 'keep me')
            finally:
                console.app.exit()
                await app

    async def test_console_session_quits_cleanly_without_model(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            run = asyncio.create_task(console.run())
            await self.until(lambda: console.app.is_running)
            keyboard.send_text('/status\r/quit\r')
            await asyncio.wait_for(run, 3)
            self.assertIsNone(console.session.agent)
            self.assertTrue(any('Session closed' in text for text, _ in console.entries))

    async def test_render_markdown_resize_and_tool_navigation(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            console.write(MarkdownMessage('**Result**\n\n- First item\n- Second item'))
            console.write('unsafe\x1b[2Jcontrol')
            console.refresh(console.app)
            self.assertIn('Result', console.transcript.text)
            self.assertNotIn('**Result**', console.transcript.text)
            self.assertNotIn('\x1b', console.transcript.text)
            console.session.activity.tool_started('p', 'fixture', {'x': 1})
            console.select_detail(-1)
            self.assertEqual(console.session.activity.inspected_id, 1)
            console.refresh(console.app)
            self.assertIn('fixture', console.details.text)
            console.session.activity.open_context('reasoning')
            console.refresh(console.app)
            self.assertIn('No provider reasoning', console.details.text)
            console.app.output.get_size = lambda: Size(rows=15, columns=60)
            console.refresh(console.app)
            self.assertIn('Result', console.transcript.text)

    async def test_narrow_screen_renders_composer_and_inspector(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            console.app.output.get_size = lambda: Size(rows=24, columns=70)
            console.session.activity.open_context('overview')
            run = asyncio.create_task(console.app.run_async())
            try:
                await self.until(lambda: console.app.renderer.last_rendered_screen is not None)
                screen = console.app.renderer.last_rendered_screen
                rendered = '\n'.join(''.join(screen.data_buffer[y][x].char for x in range(70)) for y in range(24))
                self.assertIn('Conversation', rendered)
                self.assertIn('Context / overview', rendered)
                self.assertIn('Enter send', rendered)
                self.assertNotIn('Window too small', rendered)
            finally:
                console.app.exit()
                await run

    async def test_mac_controls_preserve_draft_and_do_not_quit_from_inspector(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            console.is_macos = True
            console.session.activity.tool_started('p', 'first', {})
            console.session.activity.tool_started('p', 'second', {})
            app = asyncio.create_task(console.app.run_async())
            try:
                await self.until(lambda: console.app.is_running)
                keyboard.send_text('draft\x0f')
                await self.until(lambda: console.app.layout.has_focus(console.details))
                keyboard.send_text('\x1b[D')
                await self.until(lambda: console.session.activity.inspected_id == 2)
                keyboard.send_text('\x1b[D')
                await self.until(lambda: console.session.activity.inspected_id == 1)
                keyboard.send_text('\x1b[C')
                await self.until(lambda: console.session.activity.inspected_id == 2)
                keyboard.send_text('\x15\x04\x0c')
                await self.until(lambda: console.app.layout.has_focus(console.composer))
                self.assertTrue(console.follow)
                self.assertTrue(console.queue.empty())
                self.assertEqual(console.composer.text, 'draft')
                keyboard.send_text('\x0asecond line\r')
                self.assertEqual(await asyncio.wait_for(console.queue.get(), 2), 'draft\nsecond line')
                self.assertIn('Ctrl+J', console.composer_title())
                self.assertIn('Ctrl+O', console.shortcuts())
                self.assertNotIn('F5', console.detail_title())
            finally:
                console.app.exit()
                await app

    async def test_input_follows_conversation_instead_of_fixed_bottom(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            console.write('Latest reply')
            console.session.activity.open_context('overview')
            app = asyncio.create_task(console.app.run_async())
            def rows():
                screen = console.app.renderer.last_rendered_screen
                return [''.join(screen.data_buffer[y][x].char for x in range(120)) for y in range(30)]
            try:
                await self.until(lambda: console.app.renderer.last_rendered_screen is not None)
                keyboard.send_text('My next question')
                await self.until(lambda: console.composer.text == 'My next question')
                await asyncio.sleep(.2)
                before = rows()
                reply_row = next(i for i, row in enumerate(before) if 'Latest reply' in row)
                draft_row = next(i for i, row in enumerate(before) if 'you › My next question' in row)
                self.assertLessEqual(draft_row - reply_row, 2)
                self.assertLess(draft_row, 10)
                keyboard.send_text('\r')
                self.assertEqual(await asyncio.wait_for(console.queue.get(), 2), 'My next question')
                await asyncio.sleep(.2)
                after = rows()
                sent_row = next(i for i, row in enumerate(after) if 'you › My next question' in row)
                self.assertLessEqual(abs(sent_row - draft_row), 1)
                self.assertTrue(any('Context / overview' in row for row in after))
                console.write('\n'.join(f'History line {i}' for i in range(80)) + '\nLATEST RESULT')
                await asyncio.sleep(.2)
                long = rows()
                latest_row = next(i for i, row in enumerate(long) if 'LATEST RESULT' in row)
                input_row = next(i for i, row in enumerate(long) if 'you ›' in row)
                self.assertLessEqual(input_row - latest_row, 2)
                self.assertGreater(input_row, latest_row)
            finally:
                console.app.exit()
                await app

    async def test_native_copy_selection_is_not_captured_by_app(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            self.assertFalse(console.app.mouse_support())

    async def test_terminal_palette_is_inherited_for_text_and_markdown(self):
        with create_pipe_input() as keyboard:
            console = self.console(keyboard)
            for name in ('text-area', 'hint', 'status', 'frame.border'):
                attrs = console.app.style.get_attrs_for_style_str('class:' + name)
                self.assertIn(attrs.color, ('', 'default'))
                self.assertIn(attrs.bgcolor, ('', 'default'))
            self.assertTrue(console.app.style.get_attrs_for_style_str('class:header').reverse)
            console.write(MarkdownMessage('**Readable** text and `inline code`\n\n```python\nprint("hello")\n```'))
            console.refresh(console.app)
            self.assertIn('Readable', console.transcript.text)
            for line in console.lexer.lines:
                for style, _ in line:
                    self.assertNotIn('fg:', style)
                    self.assertNotIn('bg:', style)
