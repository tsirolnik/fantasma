import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents.messaging import MessageBus
from agents.pipeline_console import PipelineConsole
from profiles.loader import load_profile, parse_profile_toml


class ProfileInteractionTests(unittest.TestCase):
    def test_interfaces_are_independent_of_swarm_and_lab(self):
        for topology in ('swarm', 'lab'):
            for mode in ('batch', 'terminal'):
                with self.subTest(topology=topology, mode=mode):
                    profile = parse_profile_toml(f'[topology]\nmode="{topology}"\n[interaction]\nmode="{mode}"')
                    self.assertEqual(profile.interaction.mode, mode)
                    self.assertEqual(profile.topology.mode, topology)
        self.assertEqual(load_profile('interactive').topology.mode, 'pentester')
        self.assertEqual(load_profile('interactive').interaction.mode, 'terminal')

    def test_invalid_modes_and_unsupported_request_limits_fail(self):
        for text in ('[interaction]\nmode="typo"',
                     '[interaction]\nmax_iterations=5',
                     '[dashboard]\nenabled="yes"'):
            with self.assertRaises(ValueError):
                parse_profile_toml(text)

    def test_legacy_interactive_topology_alias(self):
        p = parse_profile_toml('[topology]\nmode="interactive"\nagents_per_team=1')
        self.assertEqual((p.topology.mode, p.interaction.mode), ('pentester', 'terminal'))


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def make_console(self):
        pipeline = SimpleNamespace(bus=MessageBus(), run=AsyncMock(), stop=Mock(),
            pause=Mock(), resume=Mock(), cost_tracker=Mock(get_total=lambda: 0),
            effective_budget=None)
        output = []
        return PipelineConsole(pipeline, {'targets': None, 'budget': 1}, output.append), output

    async def test_waits_for_start_and_does_not_restart_completed_run(self):
        console, _ = self.make_console()
        await console.handle('/status')
        await console.handle('/scope localhost')
        console.pipeline.run.assert_not_awaited()
        await console.handle('/start')
        await console.task
        await console.handle('/start')
        console.pipeline.run.assert_awaited_once_with(targets=['localhost'], budget=1)
        self.assertEqual(console.state, 'completed')

    async def test_stop_cancels_active_pipeline(self):
        console, _ = self.make_console()
        started = asyncio.Event()
        closed = asyncio.Event()
        async def run(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        console.pipeline.run = run
        await console.handle('/start')
        await started.wait()
        await console.handle('/pause')
        console.pipeline.pause.assert_called_once()
        await console.handle('/resume')
        console.pipeline.resume.assert_called_once()
        await console.handle('/stop')
        self.assertTrue(closed.is_set())
        self.assertEqual(console.state, 'stopped')

    async def test_directives_route_to_director_and_tool_worker(self):
        console, _ = self.make_console()
        agent = Mock()
        console.pipeline.bus.register_agent('pentester-0', agent)
        await console.handle('Focus on authentication')
        self.assertEqual(console.pipeline.bus.get_log()[-1].recipient, 'director')
        await console.handle('/tell pentester-0 Stop repeating requests')
        agent.inject_directive.assert_called_once_with('Stop repeating requests')

    async def test_checkpoint_is_async_and_budget_is_validated(self):
        console, _ = self.make_console()
        waiting = asyncio.create_task(console.checkpoint('analyze', []))
        await asyncio.sleep(0)
        await console.handle('/continue')
        self.assertTrue(await waiting)
        await console.handle('/budget nan')
        self.assertIsNone(console.pipeline.effective_budget)
        await console.handle('/budget 2')
        self.assertEqual(console.pipeline.effective_budget, 2)

    async def test_cancelled_lab_worker_cannot_continue_after_stop(self):
        from agents.pipeline import AgentPipeline
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline._check_pause_stop = AsyncMock(return_value=True)
        pipeline._stop_flag = False
        async def worker_result(assignment):
            pipeline._stop_flag = True
            return SimpleNamespace(success=False)
        with self.assertRaises(asyncio.CancelledError):
            await pipeline._execute_lab_worker(SimpleNamespace(execute=worker_result), None)

    async def test_cli_terminal_override_dispatches_lab_to_console(self):
        from main import build_parser
        from commands.full import run
        pipeline = Mock()
        with patch('agents.pipeline.AgentPipeline', return_value=pipeline), \
             patch('agents.pipeline_console.PipelineConsole') as console:
            console.return_value.run = AsyncMock()
            await run(build_parser().parse_args(['full', '--profile', 'lab', '--interaction', 'terminal']), None)
            console.assert_called_once()
            console.return_value.run.assert_awaited_once()
            pipeline.run.assert_not_called()
            self.assertEqual(pipeline.interaction_mode, 'terminal')


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def test_dashboard_independent_and_legacy_web(self):
        for topology in ('swarm', 'lab', 'pentester'):
            profile = parse_profile_toml(f'[topology]\nmode="{topology}"\nagents_per_team=1\n[interaction]\nmode="terminal"\n[dashboard]\nenabled=true')
            self.assertTrue(profile.dashboard.enabled)
            self.assertEqual(profile.interaction.mode, 'terminal')
        legacy = parse_profile_toml('[interaction]\nmode="web"')
        self.assertTrue(legacy.dashboard.enabled)
        self.assertEqual(legacy.interaction.mode, 'batch')

    async def test_terminal_and_web_start_together(self):
        from main import build_parser
        from commands.full import run
        pipeline = Mock()
        with patch('agents.pipeline.AgentPipeline', return_value=pipeline), \
             patch('agents.pipeline_console.PipelineConsole') as console, \
             patch('web.WebUI') as dashboard:
            console.return_value.run = AsyncMock()
            dashboard.return_value.start = AsyncMock()
            dashboard.return_value.shutdown = AsyncMock()
            await run(build_parser().parse_args(['full', '--profile', 'lab', '--interaction', 'terminal', '--web']), None)
            console.return_value.run.assert_awaited_once()
            dashboard.return_value.start.assert_awaited_once()
            dashboard.return_value.shutdown.assert_awaited_once()

    async def test_pentester_accepts_web_flag(self):
        from main import build_parser
        from commands.full import run
        with patch('agents.interactive.run_interactive', new_callable=AsyncMock) as interactive:
            await run(build_parser().parse_args(['full', '--web']), None)
            self.assertTrue(interactive.call_args.args[2].web)

    async def test_interactive_http_auth_and_shared_controls(self):
        from aiohttp.test_utils import TestClient, TestServer
        from agents.interactive import InteractiveSession
        from web.interactive import InteractiveWebUI
        profile = load_profile('interactive')
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(budget=1, dry_run=True), [], write=lambda text: None)
        ui = InteractiveWebUI(session)
        async with TestClient(TestServer(ui.app)) as client:
            response = await client.get('/api/status')
            self.assertEqual(response.status, 401)
            headers = {'Authorization': 'Bearer ' + ui.app['dashboard_token']}
            response = await client.post('/api/message', json={'text': '/scope localhost'}, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(session.scope, ['localhost'])
            response = await client.get('/api/status', headers=headers)
            self.assertEqual((await response.json())['scope'], ['localhost'])
            response = await client.post('/api/message', json={'text': '/stop'}, headers=headers)
            self.assertEqual(response.status, 200)
            response = await client.post('/api/message', json={'text': '/scope other.test'}, headers={**headers, 'Origin': 'https://untrusted.test'})
            self.assertEqual(response.status, 403)
            self.assertEqual(session.scope, ['localhost'])
