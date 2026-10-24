import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, MethodType

from agents.tool_agent import ToolAgent
from memory.mechanisms import Mechanism, MechanismEffect, build_mechanisms
from memory.shared import SharedMemory
from profiles.loader import load_profile


class CustomMechanismTests(unittest.TestCase):
    def test_external_file_resolves_beside_profile_and_has_fresh_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = Path(__file__).resolve().parents[1] / 'examples/mechanisms'
            for name in ('coverage.py', 'profile.toml'):
                (root / name).write_text((example / name).read_text())
            profile = load_profile(str(root / 'profile.toml'))
            first, = build_mechanisms(profile.mechanisms)
            second, = build_mechanisms(profile.mechanisms)
            finding = SimpleNamespace(target='authorized.test')
            self.assertEqual(first.on_submit('a', finding, False, 'rejected').score_delta, 0)
            self.assertEqual(first.on_submit('a', finding, True, None).score_delta, 7)
            self.assertEqual(first.on_submit('a', finding, True, None).score_delta, 0)
            self.assertEqual(second.on_submit('a', finding, True, None).score_delta, 7)

    def test_custom_class_must_be_mechanism(self):
        with self.assertRaisesRegex(ValueError, 'Mechanism subclass'):
            build_mechanisms(['collections:Counter'])

    def test_bad_arguments_report_constructor_error(self):
        with self.assertRaisesRegex(ValueError, 'Bad parameters'):
            build_mechanisms([{'class': 'memory.mechanisms:FirstFinderBonus', 'typo': 3}])


class TurnHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_context_modes_apply_turn_effect_once(self):
        class Tick(Mechanism):
            def on_turn(self, agent_id):
                return MechanismEffect(3, 'tick', 'custom turn feedback')

        for rebuilt in (False, True):
            with self.subTest(rebuilt=rebuilt):
                memory = SharedMemory()
                memory.mechanisms = [Tick()]
                agent = SimpleNamespace(
                    _shared_memory=memory, agent_id='worker', name='worker',
                    max_iterations=10, _endpoint_hits={}, _rate_limited=set(), _scope_violations=0,
                    _directive_queue=asyncio.Queue(), _raw_notes='',
                    _distilled=('', ''), in_scope=['authorized.test'],
                    out_of_scope=[], _allowed_domains=set(), cost_tracker=None,
                    session_budget=1, _last_results_text='',
                    _findings_list=lambda: [], methodology=None,
                )
                agent._build_progress_digest = MethodType(ToolAgent._build_progress_digest, agent)
                text = (await ToolAgent._assemble_turn_context(agent, 1) if rebuilt
                        else agent._build_progress_digest(1))
                self.assertIn('custom turn feedback', text)
                self.assertEqual(memory.get_score('worker').total, 3)


class ConditionalTests(unittest.TestCase):
    def test_submission_filters_do_not_consume_state_for_skipped_events(self):
        mechanism, = build_mechanisms([{
            'name': 'first_finder_bonus', 'bonus': 9,
            'when': {'hooks': ['on_submit'], 'accepted': True,
                     'targets': ['authorized.test'], 'severities': ['high'],
                     'agent_ids': ['worker-a']},
        }])
        finding = SimpleNamespace(target='authorized.test', severity='high', vuln_type='xss')
        self.assertFalse(mechanism.on_submit('worker-b', finding, True, None))
        self.assertFalse(mechanism.on_submit('worker-a', finding, False, 'rejected'))
        finding.severity = 'low'
        self.assertFalse(mechanism.on_submit('worker-a', finding, True, None))
        finding.severity = 'high'
        finding.target = 'other.test'
        self.assertFalse(mechanism.on_submit('worker-a', finding, True, None))
        finding.target = 'authorized.test'
        self.assertEqual(mechanism.on_submit('worker-a', finding, True, None).score_delta, 9)
        self.assertFalse(mechanism.on_submit('worker-a', finding, True, None))

    def test_gates_cover_all_hooks(self):
        from memory.mechanisms import ConditionalMechanism
        from unittest.mock import Mock
        inner = Mock(spec=Mechanism)
        inner.name = 'probe'
        mechanism = ConditionalMechanism(inner, {'hooks': []})
        self.assertFalse(mechanism.on_submit('a', object(), True, None))
        self.assertFalse(mechanism.on_claim('a', 'target', True))
        self.assertFalse(mechanism.on_turn('a'))
        self.assertIsNone(mechanism.on_director_review([]))
        self.assertFalse(mechanism.hides_rank_info())
        mechanism.on_run_end([])
        self.assertEqual(inner.mock_calls, [])

    def test_agent_filter_on_claim_and_turn(self):
        from memory.mechanisms import ConditionalMechanism
        from unittest.mock import Mock
        inner = Mock(spec=Mechanism)
        inner.name = 'probe'
        mechanism = ConditionalMechanism(inner, {'hooks': ['on_turn', 'on_claim'], 'agent_ids': ['a']})
        mechanism.on_turn('b')
        mechanism.on_claim('b', 'target', True)
        self.assertEqual(inner.mock_calls, [])
        mechanism.on_turn('a')
        mechanism.on_claim('a', 'target', True)
        inner.on_turn.assert_called_once_with('a')
        inner.on_claim.assert_called_once_with('a', 'target', True)

    def test_disabled_extension_is_not_imported(self):
        self.assertEqual(build_mechanisms([{'class': 'missing_package:Missing', 'enabled': False}]), [])

    def test_invalid_filters_fail_at_construction(self):
        for when in ({'hooks': ['typo']}, {'hooks': 'on_turn'}, {'typo': True},
                     {'agent_ids': ['a']}, {'targets': ['test']},
                     {'hooks': ['on_submit'], 'accepted': 'yes'}):
            with self.subTest(when=when), self.assertRaises(ValueError):
                build_mechanisms([{'name': 'first_finder_bonus', 'when': when}])

    def test_inspector_reports_active_hooks_and_excludes_disabled_entries(self):
        from profiles.__main__ import inspect_profile
        path = Path(__file__).resolve().parents[1] / 'examples/mechanisms/conditional.toml'
        result = inspect_profile(str(path))
        active, = result['active_mechanisms']
        self.assertEqual(active['active_hooks'], ['on_submit'])
        self.assertEqual(active['when']['accepted'], True)
        self.assertEqual(result['profile']['topology']['agents_per_team'], 2)

    def test_explicit_empty_list_overrides_environment_in_pipeline_and_group(self):
        from unittest.mock import patch, Mock
        from agents.pipeline import AgentPipeline
        from agents.group import AgentGroup
        settings = SimpleNamespace(mechanisms='idle_tax')
        with patch('agents.pipeline.get_settings', return_value=settings), \
             patch('agents.pipeline.PersistentMemory'), patch('agents.pipeline.LearningMemory'), \
             patch.object(AgentPipeline, '_create_workers'):
            self.assertEqual(AgentPipeline(dry_run=True, mechanisms=[]).mechanisms, [])
            self.assertEqual(AgentPipeline(dry_run=True).mechanisms, ['idle_tax'])
        kwargs = dict(num_teams=2, agents_per_team=2, director=Mock(), bus=Mock(),
                      cost_tracker=Mock(), persistent=Mock(), settings=settings)
        self.assertEqual(AgentGroup(**kwargs, mechanisms=[]).mechanisms, [])
        self.assertEqual(AgentGroup(**kwargs).mechanisms, ['idle_tax'])
