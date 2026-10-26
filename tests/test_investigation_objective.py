import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agents.methodology import PentestMethodology, RedTeamMethodology, InteractiveMethodology
from agents.pentester import PenTesterAgent
from agents.interactive import InteractivePentester
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory


class ObjectiveTests(unittest.IsolatedAsyncioTestCase):
    def test_all_builtin_pentester_prompts_share_objective(self):
        for methodology in (PentestMethodology(), RedTeamMethodology(), InteractiveMethodology()):
            for compact in (False, True):
                prompt = methodology.system_prompt(compact=compact)
                self.assertIn('highest-impact plausible vulnerability', prompt)
                self.assertIn('fewest necessary tool calls', prompt)
                self.assertIn('Minimize wasted calls, never verification', prompt)
                self.assertNotIn('Every response MUST', prompt)
                self.assertNotIn('test these patterns IN ORDER', prompt)

    async def test_real_requests_include_objective_and_can_finish_without_tools(self):
        for agent_type, methodology in ((PenTesterAgent, PentestMethodology()),
                                        (InteractivePentester, InteractiveMethodology())):
            requests = []
            async def reply(**kwargs):
                requests.append(str(kwargs['messages']))
                return {'content':[{'type':'text','text':'No useful supported lead remains; missing input prevents further conclusions.'}], 'usage':{}}
            model = SimpleNamespace(supports_tool_use=True, model='fixture', chat_with_tools=AsyncMock(side_effect=reply))
            agent = agent_type(agent_id='pentester', bus=MessageBus(), llm=model,
                               shared_memory=SharedMemory(), methodology=methodology)
            result = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Review the supplied local fixture only.'))
            self.assertTrue(result.success)
            self.assertEqual(model.chat_with_tools.await_count, 1)
            self.assertIn('highest-impact plausible vulnerability', requests[0])
            self.assertIn('ceilings, not targets to spend', requests[0])
            self.assertEqual(agent.last_trace.exit_reason, 'llm_concluded')
            self.assertEqual(agent.last_trace.tool_calls, [])

    def test_interactive_profile_does_not_require_swarm_claim(self):
        from profiles.loader import load_profile
        profile = load_profile('interactive')
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=None,
            shared_memory=SharedMemory(coordination=profile.coordination), methodology=profile.methodology)
        agent._allowed_domains = {'127.0.0.1'}
        self.assertIsNone(agent._check_scope_claim('http_fetch', {'url':'http://127.0.0.1/fixture'}))
        self.assertIn('OUT OF SCOPE', agent._check_scope_claim('http_fetch', {'url':'https://outside.test'}))
