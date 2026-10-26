"""Offline reasoning continuity through the real loop and wire normalization."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agents.interactive import InteractivePentester
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory
from profiles.loader import load_profile
from llm.openai_provider import OpenAICompatibleProvider


class ReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_tool_final_resume_and_provider_switch(self):
        decision = dict(question='Is the local fixture complete?', evidence='Missing a field',
                        conclusion='Need input', next_step='Ask user', status='blocked')
        replies = [
            {'content': [{'type': 'tool_use', 'id': 'note', 'name': 'record_investigation', 'input': decision}],
             'reasoning_content': 'Record the missing prerequisite, then ask for it.', 'usage': {}},
            {'content': [{'type': 'text', 'text': 'Please supply the missing field.'}],
             'reasoning_content': 'The fixture is incomplete; stop here.', 'usage': {}},
        ]
        captured = []
        async def respond(**kwargs):
            captured.append(copy.deepcopy(kwargs['messages']))
            return replies.pop(0)
        model = SimpleNamespace(supports_tool_use=True, supports_reasoning_history=True, model='fake',
                                chat_with_tools=AsyncMock(side_effect=respond))
        def agent():
            return InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=model,
                shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        first = agent()
        result = await first.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Review fixture'))
        self.assertTrue(result.success)
        wire = OpenAICompatibleProvider._normalize_messages(captured[1])
        turn = next(m for m in wire if m.get('tool_calls'))
        self.assertEqual(turn['reasoning_content'], 'Record the missing prerequisite, then ask for it.')
        self.assertTrue(any(m.get('tool_call_id') == 'note' for m in wire))
        restored = agent()
        restored.history = json.loads(json.dumps(first.history))
        self.assertEqual(restored.history[-1]['reasoning_content'], 'The fixture is incomplete; stop here.')
        replies.append({'content': [{'type': 'text', 'text': 'Still waiting.'}], 'usage': {}})
        await restored.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='status?'))
        self.assertTrue(any(m.get('reasoning_content') == 'The fixture is incomplete; stop here.' for m in captured[-1]))
        model.supports_reasoning_history = False
        replies.append({'content': [{'type': 'text', 'text': 'Still waiting.'}], 'usage': {}})
        await restored.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='status?'))
        self.assertFalse(any('reasoning_content' in m for m in captured[-1]))

    async def test_native_response_field_survives_provider(self):
        provider = object.__new__(OpenAICompatibleProvider)
        provider.model = 'fake'; provider.enable_thinking = False
        msg = SimpleNamespace(content='Answer', reasoning_content='Native rationale', tool_calls=[])
        create = AsyncMock(return_value=SimpleNamespace(usage=None, choices=[SimpleNamespace(message=msg)]))
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = await provider.chat_with_tools([{'role': 'user', 'content': 'hello'}], [])
        self.assertEqual(result['reasoning_content'], 'Native rationale')
        del msg.reasoning_content
        msg.content = '<think>Inline rationale</think>Answer'
        result = await provider.chat_with_tools([], [])
        self.assertNotIn('reasoning_content', result)

    def test_normalization_preserves_empty_reasoning_and_ignores_user_metadata(self):
        messages = [{'role': 'assistant', 'content': 'hello', 'reasoning_content': ''},
                    {'role': 'user', 'content': 'hi', 'reasoning_content': 'not assistant reasoning'}]
        wire = OpenAICompatibleProvider._normalize_messages(messages)
        self.assertEqual(wire[0]['reasoning_content'], '')
        self.assertNotIn('reasoning_content', wire[1])

    async def test_compression_preserves_recent_and_summarizes_old_reasoning(self):
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=None,
            shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        messages = [{'role': 'user', 'content': 'fixture'}]
        for i in range(3):
            messages.extend([{'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'read', 'id': str(i), 'input': {}}],
                              'reasoning_content': f'Rationale {i}'},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(i), 'content': 'no record'}]}])
        compressed = agent._compress_messages(messages, keep_recent=1)
        self.assertEqual(compressed[-2:], messages[-2:])
        self.assertIn('Rationale 0', str(compressed))
        agent.compress_llm = SimpleNamespace(analyze=AsyncMock(return_value='Summary: no record; stop and ask for the missing input.'))
        compressed = await agent._compress_messages_llm(messages, keep_recent=1)
        self.assertEqual(compressed[-2:], messages[-2:])
        self.assertIn('Rationale 0', agent.compress_llm.analyze.call_args.args[0])
