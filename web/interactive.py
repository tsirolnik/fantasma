"""Authenticated web companion for the same conversational terminal session."""
from pathlib import Path
import secrets

from aiohttp import web
from config import get_settings
from web.server import dashboard_auth


class InteractiveWebUI:
    def __init__(self, session):
        self.session = session
        self.runner = None
        self.messages = []
        self.original_write = session.write
        self.app = web.Application(middlewares=[dashboard_auth])
        self.app['dashboard_token'] = get_settings().web_auth_token or secrets.token_urlsafe(32)
        self.app.router.add_get('/', self.index)
        self.app.router.add_post('/login', self.index)
        self.app.router.add_get('/api/status', self.status)
        self.app.router.add_get('/api/profile', self.profile)
        self.app.router.add_post('/api/message', self.message)

    def write(self, text):
        self.messages.append(str(text))
        del self.messages[:-200]
        self.original_write(text)

    async def index(self, request):
        return web.Response(text=Path(__file__).with_name('interactive.html').read_text(), content_type='text/html')

    async def profile(self, request):
        return web.json_response(self.session.profile.summary())

    async def status(self, request):
        s = self.session
        return web.json_response({'running': bool(s.task and not s.task.done()),
            'scope': s.scope, 'cost': s.cost.get_total(), 'findings': s.memory.findings_count,
            'messages': self.messages})

    async def message(self, request):
        try:
            data = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='Expected JSON with a text field')
        text = data.get('text') if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise web.HTTPBadRequest(text='text must contain 1–20000 characters')
        if text.strip() in ('/quit', '/exit'):
            raise web.HTTPBadRequest(text='Use /stop here; close the session from its terminal')
        self.write('you> ' + text)
        await self.session.handle(text)
        return web.json_response({'accepted': True})

    async def start(self):
        self.session.write = self.write
        print(f"Dashboard access token: {self.app['dashboard_token']}", flush=True)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        settings = get_settings()
        await web.TCPSite(self.runner, settings.web_host, settings.web_port).start()
        print(f'Dashboard: http://{settings.web_host}:{settings.web_port}', flush=True)

    async def shutdown(self):
        if self.runner:
            await self.runner.cleanup()
        self.session.write = self.original_write
