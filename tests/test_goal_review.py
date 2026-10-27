import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agents.goal import InvestigationGoal
from agents.goal_reviewer import GoalReviewer
from cost.tracker import CostTracker


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def model(self, verdict):
        return SimpleNamespace(model='review-fixture', estimate_cost=lambda *args: .02,
            chat_with_tools=AsyncMock(return_value={'content': [{'type': 'text', 'text': json.dumps(verdict)}],
                                                   'usage': {'input': 50, 'output': 30}}))

    async def test_separate_review_requires_each_criterion_and_real_references(self):
        verdict = {'approved': True, 'feedback': 'Both criteria supported.', 'criteria': [
            {'index': 0, 'met': True, 'evidence_refs': ['E1']}, {'index': 1, 'met': True, 'evidence_refs': ['E2']}]}
        model = self.model(verdict)
        cost = CostTracker()
        reviewer = GoalReviewer(model, cost, 1)
        goal = {'objective': 'Review fixture', 'criteria': ['Observe record', 'Report result']}
        evidence = [{'id': 'E1', 'content': 'Tool record'}, {'id': 'E2', 'content': 'Report'}]
        result = await reviewer.review(goal, {'evidence': 'Investigator claim'}, evidence)
        self.assertTrue(result['approved'])
        self.assertEqual(cost.get_total(), .02)
        self.assertEqual(model.chat_with_tools.call_args.kwargs['tools'], [])
        self.assertEqual(len(model.chat_with_tools.call_args.kwargs['messages']), 1)
        for rows in ([verdict['criteria'][0]],
                     [{'index': 0, 'met': True, 'evidence_refs': ['invented']}, verdict['criteria'][1]],
                     [{'index': 0, 'met': False, 'evidence_refs': []}, verdict['criteria'][1]]):
            bad = self.model(dict(verdict, criteria=rows))
            with self.assertRaises(ValueError):
                await GoalReviewer(bad, cost, 1).review(goal, {}, evidence)

    async def test_rejection_then_approval_and_review_limit(self):
        reject = {'approved': False, 'feedback': 'Missing response evidence.', 'criteria': []}
        approve = {'approved': True, 'feedback': 'Now supported.', 'criteria': []}
        goal = InvestigationGoal(reviewer=AsyncMock(side_effect=[reject, approve]))
        goal.create('Assess fixture', ['Show result'])
        proposal = {'status': 'complete', 'progress': 'Done', 'evidence': 'Claim'}
        first = await goal.update_tool(proposal)
        self.assertEqual(first['status'], 'active')
        self.assertIn('Missing response', goal.briefing())
        second = await goal.update_tool(proposal)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(second['review_attempts'], 2)
        goal = InvestigationGoal(reviewer=AsyncMock(return_value=reject))
        goal.max_reviews = 1
        goal.create('Assess fixture', ['Show result'])
        await goal.update_tool(proposal)
        self.assertEqual(goal.data['status'], 'paused')
        self.assertTrue(goal.resume())
        self.assertEqual(goal.data['review_attempts'], 0)

    async def test_unavailable_malformed_and_budget_cannot_approve(self):
        proposal = {'status': 'complete', 'progress': 'Done', 'evidence': 'Claim'}
        for reviewer in (None, AsyncMock(side_effect=ValueError('malformed'))):
            goal = InvestigationGoal(reviewer=reviewer)
            goal.create('Assess fixture', ['Show result'])
            self.assertIn('error', await goal.update_tool(proposal))
            self.assertEqual(goal.data['status'], 'paused')
        model = self.model({})
        cost = CostTracker(); cost.add_cost('test', 1)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            await GoalReviewer(model, cost, 1).review({}, {}, [])
        model.chat_with_tools.assert_not_called()

    async def test_pause_during_review_cannot_be_overridden_by_late_approval(self):
        event = asyncio.Event()
        async def review(*args):
            await event.wait()
            return {'approved': True, 'feedback': 'Supported', 'criteria': []}
        goal = InvestigationGoal(reviewer=review)
        goal.create('Assess fixture', ['Show result'])
        task = asyncio.create_task(goal.update_tool({'status': 'complete', 'progress': 'Done', 'evidence': 'Claim'}))
        await asyncio.sleep(0)
        self.assertIn('error', await goal.update_tool({'status': 'active', 'progress': 'Conflicting update'}))
        goal.pause('User stopped')
        event.set()
        self.assertIn('error', await task)
        self.assertEqual(goal.data['status'], 'paused')

    async def test_session_wires_validator_and_records_evidence_packet(self):
        from unittest.mock import patch
        from agents.interactive import InteractiveSession
        from agents.tool_agent import AgentTrace, ToolCall
        from agents.messaging import TaskAssignment
        from profiles.loader import load_profile
        profile = load_profile('interactive')
        session = InteractiveSession(profile, profile.llm, SimpleNamespace(budget=1), [], lambda text: None)
        trace = AgentTrace(agent_name='pentester', assignment=TaskAssignment(
            agent_name='pentester', task_type='interactive', context='Review supplied fixture'))
        trace.tool_calls.append(ToolCall(tool_name='read_fixture', input={}, output={'record': 'observed'}))
        session.agent = SimpleNamespace(last_trace=trace, history=[])
        session.goal.create('Review fixture', ['Observe the record'])
        model = self.model({'approved': True, 'feedback': 'Recorded tool output supports the criterion.',
                            'criteria': [{'index': 0, 'met': True, 'evidence_refs': ['E2']}]})
        with patch('agents.interactive.create_llm_for_agent', return_value=model) as factory:
            result = await session.goal.update_tool({'status': 'complete', 'progress': 'Reviewed', 'evidence': 'Record present'})
        factory.assert_called_once_with('validator', session.llm_config)
        self.assertEqual(result['status'], 'complete')
        self.assertNotIn('observations', result['review'])
        self.assertEqual(session.goal.data['review']['observations'][1]['kind'], 'recorded_tool_result')
        self.assertIn('read_fixture', session.goal.data['review']['observations'][1]['content'])
        self.assertEqual(session.cost.get_total(), .02)
