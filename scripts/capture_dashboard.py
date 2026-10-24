"""Render the real full dashboard with synthetic example data (no agent runs).

Run: python scripts/capture_dashboard.py
Requires Playwright Chromium: python -m playwright install chromium
"""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
COST = {'total': 0.78, 'budget': 12.0}
AGENTS = [
    {'id': f'pentester-{i}', 'iterations': 8+i, 'max_iterations': 200,
     'tool_calls': 6+i, 'max_tool_calls': 500} for i in range(4)
] + [{'id': 'validator', 'role': 'validator', 'iterations': 2,
      'max_iterations': 100, 'tool_calls': 3, 'max_tool_calls': 300}]
STATUS = {'type': 'status', 'mode': 'swarm', 'pipeline_state': 'running',
    'current_phase': 'swarm', 'current_program': 'example.test · DEMO DATA',
    'cost': COST, 'findings': {'raw': 0, 'analyzed': 0}, 'browsers': [],
    'graph': {'connected': False},
    'swarm': {'agents': AGENTS, 'shared_memory': {'entries': 12, 'claims': 4,
        'findings': 0, 'scoreboard': [{'agent': f'pentester-{i}', 'score': 0} for i in range(4)]}}}
MESSAGES = [
    ('director', 'all', 'Authorized scope: example.test. Split coverage across application routes, authentication, API behavior, and browser workflows.'),
    ('pentester-0', 'all', 'Claimed application route mapping. I will share discovered endpoints in shared memory.'),
    ('pentester-1', 'all', 'Claimed authentication review. Checking session behavior with the supplied test accounts.'),
    ('pentester-2', 'all', 'Claimed API review. Comparing documented access rules against observed responses.'),
    ('pentester-3', 'all', 'Claimed browser workflows. Mapping forms and navigation within the authorized scope.'),
    ('director', 'all', 'Keep evidence reproducible. Coordinate through claims and notes to avoid duplicate work.'),
    ('pentester-0', 'all', 'Shared route inventory for the team. No confirmed findings submitted yet.'),
    ('validator', 'director', 'Ready to independently review submitted findings and their reproduction steps.'),
]

async def index(request):
    return web.Response(text=(ROOT/'web/frontend.html').read_text(), content_type='text/html')

async def socket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json(STATUS)
    for i, (sender, recipient, content) in enumerate(MESSAGES):
        await ws.send_json({'type': 'agent_message', 'sender': sender, 'recipient': recipient,
            'msg_type': 'directive' if sender == 'director' else 'status', 'content': content,
            'timestamp': (datetime(2026,10,24,10,0,tzinfo=timezone.utc)+timedelta(seconds=i*20)).isoformat()})
    for agent in AGENTS[:4]:
        await ws.send_json({'type': 'llm_start', 'agent': agent['id']})
    async for _ in ws:
        pass
    return ws

async def api(request):
    return web.json_response(COST if request.path.endswith('costs') else {'raw': [], 'analyzed': []})

async def main():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/ws', socket)
    app.router.add_get('/api/{name}', api)
    async with TestServer(app) as server, async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1600, 'height': 1000}, device_scale_factor=2)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(str(server.make_url('/')))
        await page.locator('#infoProgram').filter(has_text='DEMO DATA').wait_for()
        await page.locator('#agent-pentester-3').filter(has_text='thinking').wait_for()
        assert not errors, errors
        await page.screenshot(path=str(ROOT/'docs/fantasma-dashboard.png'), full_page=True)
        await browser.close()
    print('Full dashboard captured with synthetic example data; no model calls or scans.')

if __name__ == '__main__':
    asyncio.run(main())
