import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from agents.goal import InvestigationGoal
from agents.interactive import InteractivePentester, InteractiveSession
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory
from memory.interactive_sessions import InteractiveSessionStore
from profiles.loader import load_profile
from cost.tracker import CostTracker


class GoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_requires_evidence_and_user_resume(self):
        goal = InvestigationGoal(reviewer=AsyncMock(return_value={"approved": True, "feedback": "Supported", "criteria": []}))
        self.assertIn('error', goal.create('', []))
        goal.create('Review fixture', ['Read fixture and report result'])
        self.assertIn('error', goal.create('Replace', ['New goal']))
        self.assertIn('error', await goal.update_tool({'status': 'complete', 'progress': 'Done'}))
        await goal.update_tool({'status': 'blocked', 'progress': 'Missing fixture'})
        self.assertIn('error', await goal.update_tool({'status': 'active', 'progress': 'Keep going'}))
        self.assertTrue(goal.resume())
        done = await goal.update_tool({'status': 'complete', 'progress': 'Read fixture', 'evidence': 'Fixture contains the expected record'})
        self.assertEqual(done['status'], 'complete')
        self.assertFalse(goal.resume())

    def agent(self, replies, budget=100):
        model = SimpleNamespace(model='fixture', supports_tool_use=True,
                                chat_with_tools=AsyncMock(side_effect=replies))
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=model,
            shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology,
            cost_tracker=CostTracker(), max_iterations=10)
        agent.session_budget = budget
        return agent, model

    async def test_autonomous_continuation_until_evidenced_completion(self):
        agent, model = self.agent([
            {'content': [{'type': 'text', 'text': 'I will review the supplied fixture.'}]},
            {'content': [{'type': 'tool_use', 'name': 'update_goal', 'id': 'g', 'input': {
                'status': 'complete', 'progress': 'Reviewed supplied fixture', 'evidence': 'The supplied record is present'}}]},
            {'content': [{'type': 'text', 'text': 'Fixture reviewed.'}]}])
        agent.goal.reviewer = AsyncMock(return_value={'approved': True, 'feedback': 'Fixture supports completion', 'criteria': []})
        agent.goal.create('Review supplied fixture', ['Report whether record is present'])
        result = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Review fixture: record present'))
        self.assertTrue(result.success)
        self.assertEqual(model.chat_with_tools.await_count, 3)
        self.assertEqual(agent.goal.data['status'], 'complete')
        self.assertIn('continued pursuit', str(model.chat_with_tools.call_args_list[1]))
        self.assertTrue(any('INVESTIGATION GOAL' in str(m) for m in model.chat_with_tools.call_args.kwargs['messages']))

    async def test_idle_answers_pause_and_budget_stops_without_model_call(self):
        agent, model = self.agent([{'content': [{'type': 'text', 'text': 'Still working.'}]}] * 3)
        agent.goal.create('Review fixture', ['Report result'])
        await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Review'))
        self.assertEqual(model.chat_with_tools.await_count, 3)
        self.assertEqual(agent.goal.data['status'], 'paused')
        agent.goal.resume()
        agent.cost_tracker.add_cost('test', 101)
        await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Continue'))
        self.assertEqual(model.chat_with_tools.await_count, 3)
        self.assertEqual(agent.goal.data['status'], 'paused')

    def session(self):
        profile = load_profile('interactive')
        return InteractiveSession(profile, profile.llm, SimpleNamespace(budget=None), [], lambda text: None)

    async def test_session_restore_is_paused_and_controls_resume_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InteractiveSessionStore(directory)
            store.acquire()
            session = self.session()
            session.attach_store(store)
            session.goal.create('Review fixture', ['Report result'])
            snapshot = json.loads(store.path(store.session_id).read_text())
            restored = self.session()
            restored.attach_store(store, snapshot)
            self.assertEqual(restored.goal.data['objective'], 'Review fixture')
            self.assertEqual(restored.goal.data['status'], 'paused')
            self.assertIsNone(restored.task)
            restored.start_request = Mock()
            await restored.handle('/goal resume')
            restored.start_request.assert_called_once()
            self.assertEqual(restored.goal.data['status'], 'active')
            await restored.handle('/stop')
            self.assertEqual(restored.goal.data['status'], 'paused')
            await restored.handle('/goal clear')
            self.assertIsNone(restored.goal.data)
            store.close()
