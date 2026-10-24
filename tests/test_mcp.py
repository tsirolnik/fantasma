import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from agents.mcp_runtime import MCPRuntime, attach_mcp_tools, tool_name, environment_refs
from agents.tools import ToolRegistry
from profiles.loader import parse_profile_toml, load_profile
from profiles.mcp import MCPServer

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_example_and_offline_summary(self):
        profile = load_profile(str(ROOT / 'examples/mcp/profile.toml'))
        self.assertEqual(profile.mcp[0].cwd, str(ROOT / 'examples/mcp'))
        self.assertEqual(profile.summary()['mcp'][0]['tools'], ['add'])

    def test_invalid(self):
        for kwargs in ({'name': 'bad space', 'command': 'python'},
                       {'name': 'ok'}, {'name': 'ok', 'transport': 'ftp'},
                       {'name': 'ok', 'command': 'python', 'timeout': float('nan')},
                       {'name': 'ok', 'command': 'python', 'tools': 'all'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MCPServer(**kwargs)
        with self.assertRaises(ValueError):
            parse_profile_toml('[[mcp.servers]]\nname="x"\ncommand="python"\n[[mcp.servers]]\nname="x"\ncommand="python"')

    def test_names_and_missing_secret(self):
        self.assertNotEqual(tool_name('ida', 'a.b'), tool_name('ida', 'a_b'))
        self.assertLessEqual(len(tool_name('x' * 24, 'long' * 100)), 64)
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(ValueError):
            environment_refs({'Authorization': 'TEST_MCP_TOKEN'})


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def spec(self, **kwargs):
        return MCPServer(name='demo', command=sys.executable,
                         args=[str(ROOT / 'examples/mcp/server.py')], **kwargs)

    async def test_stdio_calls_context_and_cleanup(self):
        async with MCPRuntime() as runtime:
            await runtime.connect([self.spec(tools=['add'])])
            async def child():
                registry = ToolRegistry()
                attach_mcp_tools(registry)
                return registry
            registry = await asyncio.create_task(child())
            self.assertEqual(len(registry), 1)
            tool = registry.list_tools()[0]
            self.assertEqual(tool.parameters['required'], ['a', 'b'])
            result = await tool.handler({'a': 2, 'b': 3})
            self.assertEqual(result['structuredContent'], {'result': 5})
            with self.assertRaises(ValueError):
                attach_mcp_tools(registry)
        empty = ToolRegistry()
        attach_mcp_tools(empty)
        self.assertEqual(len(empty), 0)

    async def test_remote_error_and_allowlist_failure(self):
        async with MCPRuntime() as runtime:
            await runtime.connect([self.spec(tools=['fail'])])
            with self.assertRaisesRegex(RuntimeError, 'deliberate demo error'):
                await runtime.tools.list_tools()[0].handler({})
        with self.assertRaisesRegex(ValueError, 'Unknown tools'):
            async with MCPRuntime() as runtime:
                await runtime.connect([self.spec(tools=['missing'])])


    async def test_http_transports_pagination_and_agent_attachment(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from agents.tool_agent import ToolAgent
        from agents.messaging import MessageBus
        from mcp.types import Tool as RemoteTool

        remote = RemoteTool(name="inspect", inputSchema={"type": "object"})
        session = AsyncMock()
        session.list_tools.side_effect = [
            SimpleNamespace(tools=[], nextCursor="page2"),
            SimpleNamespace(tools=[remote], nextCursor=None),
        ]
        closed = []
        @asynccontextmanager
        async def transport(*args, **kwargs):
            try:
                yield (None, None, None)
            finally:
                closed.append(True)
        @asynccontextmanager
        async def client(*args, **kwargs):
            yield session
        for transport_name, target in (("sse", "mcp.client.sse.sse_client"),
                                       ("streamable_http", "mcp.client.streamable_http.streamablehttp_client")):
            session.list_tools.side_effect = [
                SimpleNamespace(tools=[], nextCursor="page2"),
                SimpleNamespace(tools=[remote], nextCursor=None),
            ]
            with patch(target, transport), patch('mcp.ClientSession', client):
                async with MCPRuntime() as runtime:
                    await runtime.connect([MCPServer(name="remote", transport=transport_name, url="http://localhost/mcp")])
                    agent = ToolAgent(MessageBus(), tools=ToolRegistry())
                    self.assertEqual(agent.tools.names(), runtime.tools.names())
                    session.list_tools.assert_called_with(cursor="page2")
        self.assertEqual(len(closed), 2)
