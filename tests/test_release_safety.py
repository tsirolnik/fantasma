import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from config import Settings
from agents.tool_agent import ToolAgent
from llm.openai_provider import OpenAICompatibleProvider
from utils.docker_runner import run_container
from web.server import create_app


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings_patch = patch('web.server.get_settings', return_value=Settings(_env_file=None))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.bridge = SimpleNamespace(
            get_profile=lambda: {'topology': {'num_teams': 2}},
            clients=[], add_client=Mock(), remove_client=Mock(),
            handle_client_message=AsyncMock(), _broadcast=Mock())
        with contextlib.redirect_stdout(io.StringIO()):
            self.app = create_app(self.bridge)
        self.auth = {'Authorization': 'Bearer ' + self.app['dashboard_token']}
        self.client = TestClient(TestServer(self.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def test_profile_starts_and_requires_auth(self):
        self.assertEqual((await self.client.get('/api/profile')).status, 401)
        response = await self.client.get('/api/profile', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['topology']['num_teams'], 2)

    async def test_login_cookie_opens_dashboard_and_websocket(self):
        response = await self.client.get('/')
        self.assertIn('Access token', await response.text())
        self.assertEqual((await self.client.post('/login', data={'token': 'wrong'})).status, 401)
        response = await self.client.post('/login', data={'token': self.app['dashboard_token']},
                                          headers={'Origin': self.origin})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get('/api/profile')).status, 200)
        ws = await self.client.ws_connect('/ws', headers={'Origin': self.origin})
        await ws.send_json({'type': 'control', 'action': 'set_budget', 'value': 2})
        for _ in range(100):
            if self.bridge.handle_client_message.await_count:
                break
            await asyncio.sleep(.001)
        self.assertEqual(self.bridge.handle_client_message.await_count, 1)
        await ws.close()

    async def test_websocket_rejects_foreign_missing_origin_and_missing_token(self):
        for headers, status in [
            ({**self.auth, 'Origin': 'https://untrusted.example'}, 403),
            (self.auth, 403), ({'Origin': self.origin}, 401),
        ]:
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await self.client.ws_connect('/ws', headers=headers)
            self.assertEqual(cm.exception.status, status)
        self.bridge.handle_client_message.assert_not_awaited()

    async def test_ingest_requires_bearer_and_rejects_foreign_origin(self):
        self.assertEqual((await self.client.post('/api/ingest', json={'type': 'event'})).status, 401)
        self.assertEqual((await self.client.post('/api/ingest', json={'type': 'event'}, headers=self.auth)).status, 200)
        self.assertEqual((await self.client.post('/api/ingest', json={}, headers={**self.auth, 'Origin': 'https://evil.test'})).status, 403)
        self.assertEqual((await self.client.get('/', headers={'Host': 'rebind.example'})).status, 403)


class ScopeTests(unittest.TestCase):
    def test_mixed_hosts_cannot_authorize_external_request(self):
        agent = SimpleNamespace(_allowed_domains={'allowed.test'},
            _extract_domain=ToolAgent._extract_domain, _hosts_in_text=ToolAgent._hosts_in_text)
        for params in (
            {'code': "# https://allowed.test\nrequests.get('https://outside.test')"},
            {'command': 'curl https://allowed.test https://outside.test'},
            {'url': 'https://outside.test', 'data': 'https://allowed.test'},
            {'target': 'allowed.test', 'url': 'https://outside.test'},
            {'domain': 'outside.test'},
            {'code': "requests.get('http://127.0.0.1:9000')"},
            {'code': "requests.get('http://localhost:9000')"},
            {'code': "requests.get('http://allowed.test@outside.test')"},
        ):
            self.assertIn('OUT OF SCOPE', ToolAgent._check_scope_claim(agent, 'execute_code', params))
        self.assertIsNone(ToolAgent._check_scope_claim(agent, 'execute_code', {'code': "requests.get('https://allowed.test')"}))

    def test_explicit_local_scope_keeps_benchmark_supported(self):
        agent = SimpleNamespace(_allowed_domains={'localhost'},
            _extract_domain=ToolAgent._extract_domain, _hosts_in_text=ToolAgent._hosts_in_text,
            _shared_memory=None)
        for params in ({'code': "requests.get('http://localhost:3000')"}, {'url': 'http://localhost:3000'}):
            self.assertIsNone(ToolAgent._check_scope_claim(agent, 'http_fetch', params))

    def test_localhost_claim_is_honored(self):
        memory = SimpleNamespace(coordination=SimpleNamespace(claim_mode='exclusive'),
                                 is_claimed=lambda key: 'worker' if key == 'scan:localhost' else None)
        agent = SimpleNamespace(_allowed_domains={'localhost'}, name='worker',
            _extract_domain=ToolAgent._extract_domain, _hosts_in_text=ToolAgent._hosts_in_text,
            _shared_memory=memory)
        self.assertIsNone(ToolAgent._check_scope_claim(agent, 'http_fetch', {'url': 'http://localhost:3000'}))


class PricingTests(unittest.TestCase):
    def test_paid_endpoint_rejects_missing_and_zero_prices_before_client_creation(self):
        for values in ({}, {'openai_input_price': 1}, {'openai_input_price': 0, 'openai_output_price': 0}):
            with patch('llm.openai_provider.get_settings', return_value=Settings(_env_file=None, **values)), \
                 patch('llm.openai_provider.AsyncOpenAI') as client:
                with self.assertRaisesRegex(ValueError, 'OPENAI_INPUT_PRICE'):
                    OpenAICompatibleProvider()
                client.assert_not_called()

    def test_explicit_free_endpoint_and_configured_paid_pricing(self):
        for values, expected in [({'openai_free_endpoint': True}, 0),
                                 ({'openai_input_price': 2, 'openai_output_price': 4}, 6)]:
            with patch('llm.openai_provider.get_settings', return_value=Settings(_env_file=None, **values)), \
                 patch('llm.openai_provider.AsyncOpenAI'):
                self.assertEqual(OpenAICompatibleProvider().estimate_cost(1000000, 1000000), expected)

    def test_invalid_prices_rejected(self):
        for value in (-1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                Settings(_env_file=None, openai_input_price=value)


class ContainerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, outcome):
        entered = asyncio.Event()
        proc = Mock(returncode=None)
        async def communicate():
            entered.set()
            if proc.returncode is not None:
                return b'', b''
            if outcome == 'success':
                proc.returncode = 0
                return b'output', b''
            await asyncio.Event().wait()
        proc.communicate = AsyncMock(side_effect=communicate)
        proc.kill.side_effect = lambda: setattr(proc, 'returncode', -9)
        cleanup = Mock(returncode=0, communicate=AsyncMock(return_value=(b'', b'')))
        with patch('utils.docker_runner.asyncio.create_subprocess_exec', AsyncMock(side_effect=[proc, cleanup])) as spawn:
            task = asyncio.create_task(run_container('test-image', ['sleep 60'], timeout=.01 if outcome == 'timeout' else 10))
            await entered.wait()
            if outcome == 'cancel':
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                self.assertEqual(result[0], 0 if outcome == 'success' else -1)
            args = spawn.call_args_list[0].args
            name = args[args.index('--name') + 1]
            self.assertTrue(name.startswith('fantasma-tool-'))
            self.assertEqual(spawn.call_args_list[1].args[1:], ('rm', '--force', name))
            if outcome != 'success':
                proc.kill.assert_called_once()

    async def test_success_removes_container(self):
        await self.exercise('success')

    async def test_timeout_removes_container(self):
        await self.exercise('timeout')

    async def test_cancellation_removes_container_and_propagates(self):
        await self.exercise('cancel')
