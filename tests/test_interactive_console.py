"""Quiet console output without losing file diagnostics or interactive controls."""
import asyncio
import io
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agents.interactive import InteractiveSession
from profiles.loader import load_profile, parse_profile_toml
from utils.console_logging import configure_console_logging


class ConsoleLoggingTests(unittest.TestCase):
    def test_quiet_console_keeps_file_logs_and_diagnostics_restore_output(self):
        root = logging.getLogger()
        old_level = root.level
        output = io.StringIO()
        console = logging.StreamHandler(output)
        console.set_name('fantasma.console')
        with tempfile.NamedTemporaryFile() as file:
            disk = logging.FileHandler(file.name)
            with patch.object(root, 'handlers', [console, disk]):
                try:
                    root.setLevel(logging.INFO)
                    configure_console_logging(quiet=True)
                    root.info('hidden diagnostic')
                    root.error('hidden traceback')
                    disk.flush()
                    self.assertEqual(output.getvalue(), '')
                    self.assertIn('hidden diagnostic', file.read().decode())
                    configure_console_logging(quiet=False, debug=True)
                    root.debug('visible diagnostic')
                    self.assertIn('visible diagnostic', output.getvalue())
                finally:
                    root.setLevel(old_level)
                    disk.close()

    def test_profile_diagnostics_requires_boolean(self):
        with self.assertRaises(ValueError):
            parse_profile_toml('[interaction]\ndiagnostics="false"')


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_precedes_work_and_response_printed_once(self):
        output = []
        profile = load_profile('interactive')
        session = InteractiveSession(profile, profile.llm,
            SimpleNamespace(budget=1), [], output.append)
        release = asyncio.Event()
        async def execute(assignment):
            await release.wait()
            return SimpleNamespace(success=True, reflection='Hello!', data={})
        session.agent = SimpleNamespace(execute=execute)
        session.ensure_agent = AsyncMock()
        await session.handle('hi')
        self.assertEqual(len(output), 1)
        self.assertIn('Thinking', output[0])
        release.set()
        await session.task
        self.assertEqual(output.count('Hello!'), 1)
        self.assertFalse(session.diagnostics)

    async def test_flags_and_profile_enable_diagnostics(self):
        profile = load_profile('interactive')
        for flag in ('diagnostics', 'verbose'):
            session = InteractiveSession(profile, profile.llm,
                SimpleNamespace(budget=1, **{flag: True}), [])
            self.assertTrue(session.diagnostics)
        profile.interaction.diagnostics = True
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(budget=1), [])
        self.assertTrue(session.diagnostics)
