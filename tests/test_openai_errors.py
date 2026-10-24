"""Exercise provider failures through the real agent loop without network calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, mock_open
import httpx
import openai
from agents.interactive import InteractivePentester
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory
from profiles.loader import load_profile
from llm.openai_provider import OpenAICompatibleProvider


class ProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, status, body):
        response = httpx.Response(status, request=httpx.Request('POST', 'https://example.test/v1'))
        error = openai.APIStatusError('test failure', response=response, body=body)
        provider = object.__new__(OpenAICompatibleProvider)
        provider.model = 'test-model'
        provider.enable_thinking = False
        create = AsyncMock(side_effect=error)
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=provider,
                                     shared_memory=SharedMemory(), methodology=load_profile('interactive').methodology)
        with patch('builtins.open', mock_open()), patch('os.makedirs'):
            result = await agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context='hi'))
        self.assertFalse(result.success)
        return create.await_count

    async def test_permanent_errors_stop_after_one_call(self):
        for status, body in [(404, {'reason': 'MODEL_NOT_FOUND'}),
                             (401, {'code': 'invalid_api_key'}),
                             (400, {'message': 'insufficient balance'}),
                             (400, {'message': 'invalid tool schema'})]:
            with self.subTest(status=status, body=body):
                self.assertEqual(await self.run_failure(status, body), 1)

    async def test_context_recovery_is_bounded(self):
        self.assertEqual(await self.run_failure(400, {'code': 'context_length_exceeded'}), 3)
