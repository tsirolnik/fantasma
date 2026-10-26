import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agents.tools import create_search_tools, create_research_tools
from utils.browser_search import search


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_does_not_open_browser(self):
        context = SimpleNamespace(new_page=AsyncMock())
        for query, limit in [('', 5), ('x'*501, 5), ('docs', 0), ('docs', 11), ('docs', True)]:
            self.assertIn('error', await search(context, query, limit))
        context.new_page.assert_not_called()

    async def test_cancellation_closes_search_tab(self):
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        page = SimpleNamespace(goto=wait, close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        task = asyncio.create_task(search(context, 'public documentation'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        page.close.assert_awaited_once()

    async def test_tool_reuses_session_and_respects_browser_disabled(self):
        session = SimpleNamespace(search=AsyncMock(return_value={'status':'ok','results':[]}))
        tool = create_research_tools(session).get('web_search')
        with patch('config.get_settings', return_value=SimpleNamespace(enable_browser=True)):
            await tool.handler({'query': 'public docs', 'max_results': 3})
        session.search.assert_awaited_once_with('public docs', 3)
        with patch('config.get_settings', return_value=SimpleNamespace(enable_browser=False)):
            self.assertEqual((await tool.handler({'query':'public docs'}))['status'], 'unavailable')
        self.assertIsNotNone(create_research_tools().get('run_docker_tool'))
        self.assertNotIn('nuclei', create_research_tools().get('run_docker_tool').description.lower())

    async def test_real_chromium_extracts_local_fixtures_without_network(self):
        from utils.browser import BrowserPool
        pool = BrowserPool()
        await pool.start()
        try:
            session = await pool.create_session('search-fixture')
            context = session._context
            original = await session._ensure_page()
            cases = [
                (200, '<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fdocs%3Fa%3D%252F">Docs &amp; API</a><a class="result__snippet">Public documentation</a></div>'*2, 'ok'),
                (200, '<form id="challenge-form">Challenge</form>', 'blocked'),
                (429, '<p>Rate limited</p>', 'blocked'),
                (200, '<div class="no-results">No results</div>', 'no_results'),
                (200, '<div>Unexpected markup</div>', 'unrecognized_page'),
                (500, '<div>Unavailable</div>', 'failed'),
            ]
            for code, html, expected in cases:
                async def fulfill(route, request, code=code, html=html):
                    await route.fulfill(status=code, content_type='text/html', body=html)
                await context.route('**/*', fulfill)
                result = await session.search('public docs')
                self.assertEqual(result['status'], expected)
                self.assertEqual(context.pages, [original])
                self.assertEqual(original.url, 'about:blank')
                if expected == 'ok':
                    self.assertEqual(len(result['results']), 1)
                    self.assertEqual(result['results'][0]['title'], 'Docs & API')
                    self.assertEqual(result['results'][0]['url'], 'https://example.test/docs?a=%2F')
                await context.unroute('**/*', fulfill)
        finally:
            await pool.stop()

    async def test_public_search_is_available_without_target_scope(self):
        from agents.interactive import InteractivePentester
        from agents.messaging import MessageBus
        from memory.shared import SharedMemory
        from profiles.loader import load_profile
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=None,
            shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        agent._allowed_domains = set()
        self.assertIsNone(agent._check_scope_claim('web_search', {'query': 'public documentation'}))
        self.assertIn('No target scope', agent._check_scope_claim('http_fetch', {'url':'https://example.test'}))
