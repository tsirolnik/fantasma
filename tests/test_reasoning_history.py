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
        self.assertEqual(compressed, messages)
        agent.compress_llm.analyze.assert_not_called()

class NativeHistoryTests(unittest.TestCase):
    def test_toml_default_and_validation(self):
        from profiles.loader import _resolve_llm
        self.assertTrue(_resolve_llm({}).retain_thinking)
        self.assertFalse(_resolve_llm({'retain_thinking': False}).retain_thinking)
        with self.assertRaisesRegex(ValueError, 'boolean'):
            _resolve_llm({'retain_thinking': 'false'})

    def test_native_ollama_and_openai_json_replay(self):
        from llm.ollama_provider import OllamaProvider
        from llm.reasoning import prepare_messages
        for cls, data, key in [
            (OllamaProvider, {'thinking': 'Full rationale', 'content': 'answer'}, 'thinking'),
            (OpenAICompatibleProvider, {'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'opaque'}],
                                       'content': '<think>Inline rationale</think>answer'}, 'reasoning_details'),
        ]:
            provider = object.__new__(cls)
            history = json.loads(json.dumps([{'role': 'assistant', 'content': 'answer',
                '_provider_history': {'provider': cls.__name__, 'data': data}}]))
            normalize = getattr(cls, '_normalize_messages', None) or cls._normalize_messages_for_ollama
            self.assertEqual(normalize(prepare_messages(history, provider))[0][key], data[key])
            provider.retain_thinking = False
            self.assertNotIn(key, normalize(prepare_messages(history, provider))[0])

    def test_gemini_signature_and_thought_resume_with_new_adapter(self):
        from google.genai import types
        from llm.gemini_provider import GeminiProvider
        provider = object.__new__(GeminiProvider)
        provider._pending_tool_names = {}; provider._pending_thought_sigs = {}
        content = types.Content(role='model', parts=[
            types.Part(text='Check the prerequisite', thought=True),
            types.Part(function_call=types.FunctionCall(name='read', args={}), thought_signature=b'\x00\xffsignature')])
        response = SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=None)
        parsed = provider._parse_response(response)
        history = json.loads(json.dumps([{'role': 'assistant', 'content': parsed['content'],
            '_provider_history': {'provider': 'GeminiProvider', 'data': parsed['history']}},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': parsed['content'][0]['id'], 'content': 'done'}]}]))
        provider._pending_tool_names = {}; provider._pending_thought_sigs = {}
        replay = provider._to_gemini_contents(history)
        self.assertEqual(replay[0].parts[0].text, 'Check the prerequisite')
        self.assertTrue(replay[0].parts[0].thought)
        self.assertEqual(replay[0].parts[1].thought_signature, b'\x00\xffsignature')
        self.assertEqual(replay[1].parts[0].function_response.name, 'read')

    def test_anthropic_signed_content_stays_exact(self):
        from llm.claude_provider import ClaudeProvider
        from llm.vertex_provider import VertexProvider
        from llm.reasoning import prepare_messages, has_reasoning
        history = [{'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'Reason', 'signature': 'signed'},
            {'type': 'redacted_thinking', 'data': 'opaque'},
            {'type': 'text', 'text': 'answer'}]}]
        for cls in (ClaudeProvider, VertexProvider):
            self.assertEqual(prepare_messages(json.loads(json.dumps(history)), object.__new__(cls)), history)
        self.assertTrue(has_reasoning(history))

class SwarmRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuilt_context_keeps_signed_tool_turn_and_final_response(self):
        from llm.claude_provider import ClaudeProvider
        model = object.__new__(ClaudeProvider)
        model.model = 'fixture'
        thought = {'type': 'thinking', 'thinking': 'Need a prerequisite', 'signature': 'signature'}
        decision = dict(question='Ready?', evidence='Missing input', conclusion='Blocked', next_step='Ask', status='blocked')
        model.chat_with_tools = AsyncMock(side_effect=[
            {'content': [thought, {'type': 'tool_use', 'id': 'note', 'name': 'record_investigation', 'input': decision}]},
            {'content': [thought, {'type': 'text', 'text': 'Please supply input.'}]}])
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=model,
            shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        agent.context_mode = True
        agent._assemble_turn_context = AsyncMock(side_effect=['Fresh briefing 1', 'Fresh briefing 2'])
        result = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='Review'))
        self.assertTrue(result.success)
        messages = model.chat_with_tools.call_args.kwargs['messages']
        self.assertEqual(messages[0]['content'], 'Fresh briefing 2')
        self.assertEqual(messages[1]['content'][0], thought)
        self.assertEqual(messages[2]['content'][0]['tool_use_id'], 'note')
        self.assertEqual(json.loads(json.dumps(agent.history))[-1]['content'][0], thought)
