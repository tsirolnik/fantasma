"""Installed CLI dispatch contracts; wheel smoke checks also run during release."""
import unittest
from unittest.mock import patch

from main import async_main, build_parser


class CLITests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_profiles_does_not_create_orchestrator(self):
        args = build_parser().parse_args(['profiles', 'inspect', 'interactive'])
        with patch('main.Orchestrator') as orchestrator, patch('profiles.__main__.main') as inspect:
            await async_main(args)
        orchestrator.assert_not_called()
        inspect.assert_called_once_with(['inspect', 'interactive'])

    def test_cli_identity(self):
        self.assertEqual(build_parser().prog, 'fantasma')
        with self.assertRaises(SystemExit) as exited, patch('sys.stdout'):
            build_parser().parse_args(['--version'])
        self.assertEqual(exited.exception.code, 0)


class RenameCompatibilityTests(unittest.TestCase):
    def test_legacy_database_path_and_new_name_precedence(self):
        from memory.store import _default_db_path
        from pathlib import Path
        with patch.dict('os.environ', {'SWARMHOUND_DB_PATH': '/tmp/legacy-test.sqlite'}, clear=True):
            self.assertEqual(_default_db_path(), Path('/tmp/legacy-test.sqlite'))
            with patch.dict('os.environ', {'FANTASMA_DB_PATH': '/tmp/current-test.sqlite'}):
                self.assertEqual(_default_db_path(), Path('/tmp/current-test.sqlite'))

    def test_legacy_tool_host_rewrite_and_new_name_precedence(self):
        from utils.docker_runner import _apply_host_rewrite
        with patch.dict('os.environ', {'SWARMHOUND_TOOL_HOST_REWRITE': 'localhost=legacy'}, clear=True):
            self.assertEqual(_apply_host_rewrite(['localhost:3000']), ['legacy:3000'])
            with patch.dict('os.environ', {'FANTASMA_TOOL_HOST_REWRITE': 'localhost=current'}):
                self.assertEqual(_apply_host_rewrite(['localhost:3000']), ['current:3000'])
