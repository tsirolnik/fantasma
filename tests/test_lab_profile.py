import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from agents.pipeline import AgentPipeline
from memory.session import SessionMemory
from profiles import LabWorkflow
from profiles.loader import load_profile, parse_profile_toml


class LabProfileTests(unittest.TestCase):
    def test_shipped_lab_profile(self):
        profile = load_profile('lab')
        self.assertEqual(profile.topology.mode, 'lab')
        self.assertEqual(profile.workflow.stages, ['recon', 'scan', 'analyze', 'report'])
        self.assertEqual(profile.summary()['workflow']['worker_mode'], 'auto')

    def test_forced_tool_mode_rejects_unsupported_provider(self):
        from unittest.mock import patch
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline.workflow = LabWorkflow(worker_mode="tool")
        with patch('agents.pipeline.create_llm_for_agent', return_value=SimpleNamespace(supports_tool_use=False)):
            with self.assertRaisesRegex(ValueError, 'tool-capable'):
                pipeline._create_workers(None, False)

    def test_workflow_validation(self):
        for stages in ([], ['report'], ['scan', 'recon'], ['scan', 'scan'], ['typo']):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                LabWorkflow(stages=stages)
        with self.assertRaises(ValueError):
            LabWorkflow(max_scan_targets=0)
        with self.assertRaises(ValueError):
            LabWorkflow(worker_mode='typo')
        with self.assertRaisesRegex(ValueError, 'only with topology.mode'):
            parse_profile_toml('[workflow]\nstages = ["scan"]')


class LabExecutionTests(unittest.IsolatedAsyncioTestCase):
    def pipeline(self, stages):
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline.workflow = LabWorkflow(stages=stages, max_scan_targets=1)
        pipeline.cost_tracker = Mock()
        pipeline.cost_tracker.get_total.return_value = 0
        pipeline.cost_tracker.get_per_target_costs.return_value = {}
        pipeline.learning = Mock()
        pipeline.learning.summary.return_value = ''
        pipeline.persistent = Mock()
        pipeline.session = SessionMemory()
        pipeline.graph = None
        pipeline._stop_flag = False
        pipeline._check_pause_stop = AsyncMock(return_value=True)
        pipeline._emit_phase = Mock()
        pipeline._checkpoint = AsyncMock(return_value=True)
        pipeline.director = Mock()
        pipeline.director.evaluate_phase_result = AsyncMock(return_value=SimpleNamespace(decision='continue'))
        finding = SimpleNamespace(target='lab.test', model_dump=lambda **kwargs: {'target': 'lab.test'})
        pipeline.recon = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data={'subdomains': ['lab.test', 'other.lab.test']})))
        pipeline.scanner = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data=[finding])))
        pipeline.analyzer = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data=[finding])))
        pipeline.reporter = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data=[])))
        return pipeline, finding

    async def test_default_workflow_keeps_stage_order_and_target_limit(self):
        pipeline, finding = self.pipeline(load_profile('lab').workflow.stages)
        result = await pipeline._process_program('test', ['lab.test'], 1)
        self.assertEqual(result, [finding])
        self.assertEqual([call.args[0] for call in pipeline._emit_phase.call_args_list],
                         ['recon', 'scan', 'analyze', 'report'])
        assignment = pipeline.scanner.execute.call_args.args[0]
        self.assertEqual(assignment.parameters['targets'], ['lab.test'])
        pipeline.persistent.save_finding.assert_called_once()

    async def test_scan_only_skips_optional_stages_and_persists_raw_results(self):
        pipeline, finding = self.pipeline(['scan'])
        result = await pipeline._process_program('test', ['lab.test'], 1)
        self.assertEqual(result, [finding])
        pipeline.recon.execute.assert_not_awaited()
        pipeline.analyzer.execute.assert_not_awaited()
        pipeline.reporter.execute.assert_not_awaited()
        pipeline._checkpoint.assert_not_awaited()
        assignment = pipeline.scanner.execute.call_args.args[0]
        self.assertEqual(assignment.parameters['recon_data'], {})
        pipeline.persistent.save_finding.assert_called_once()

    async def test_director_abort_still_stops_downstream_stages(self):
        pipeline, _ = self.pipeline(load_profile('lab').workflow.stages)
        pipeline.director.evaluate_phase_result.return_value = SimpleNamespace(decision='abort', reasoning='stop')
        self.assertEqual(await pipeline._process_program('test', ['lab.test'], 1), [])
        pipeline.scanner.execute.assert_not_awaited()
        pipeline.persistent.complete_scan.assert_called_once()


class LabCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_and_legacy_flag_load_same_profile(self):
        from unittest.mock import patch
        from main import build_parser
        from commands.full import run
        class ConstructionReached(Exception):
            pass
        for flags in (['--mode', 'lab'], ['--profile', 'lab']):
            args = build_parser().parse_args(['full', *flags])
            with patch('agents.pipeline.AgentPipeline', side_effect=ConstructionReached) as constructor:
                with self.assertRaises(ConstructionReached):
                    await run(args, None)
            self.assertEqual(constructor.call_args.kwargs['workflow'], load_profile('lab').workflow)
            self.assertEqual(type(constructor.call_args.kwargs['target_provider']),
                             type(load_profile('lab').target_provider))
