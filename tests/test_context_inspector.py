import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agents.interactive import InteractiveSession
from profiles.loader import load_profile


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        p=load_profile('interactive')
        return InteractiveSession(p,p.llm,SimpleNamespace(budget=2),[],lambda text: None)

    async def test_context_is_available_without_creating_agent(self):
        s=self.session()
        s.ensure_agent=AsyncMock()
        await s.handle('/context')
        self.assertEqual(s.activity.inspector_kind,'context')
        self.assertIn('no model request yet',s.context_inspector.render())
        s.ensure_agent.assert_not_called()
        self.assertIsNone(s.task)
        await s.handle('/context off')
        self.assertFalse(s.activity.expanded)

    async def test_captured_request_has_roles_tools_and_redacted_previews(self):
        s=self.session()
        messages=[{'role':'user','content':{'Authorization':'hidden','text':'investigate login'}}]
        s.context_inspector.capture(messages,[{'name':'http_fetch','input_schema':{'type':'object'}}])
        messages[0]['content']['text']='mutated after capture'
        view=s.context_inspector.render('1')
        self.assertIn('investigate login',view)
        self.assertNotIn('hidden',view)
        self.assertNotIn('mutated',view)
        self.assertIn('http_fetch',s.context_inspector.render('tools'))
        self.assertIn('1 messages',s.context_inspector.render())
        self.assertIn('No context message',s.context_inspector.render('20'))

    async def test_switch_between_context_and_tools_and_close(self):
        s=self.session()
        s.activity.live_inspector=True
        await s.handle('/context messages')
        self.assertFalse(s.activity.follow_tail)
        self.assertIn('CONTEXT',s.activity.toolbar())
        s.activity.toggle()
        self.assertEqual(s.activity.inspector_kind,'tools')
        self.assertTrue(s.activity.expanded)
        self.assertTrue(s.activity.follow_tail)
        s.activity.open_context(toggle=True)
        self.assertEqual(s.activity.inspector_kind,'context')
        s.activity.open_context(toggle=True)
        self.assertFalse(s.activity.expanded)

    async def test_real_agent_callback_captures_model_request(self):
        from agents.interactive import InteractivePentester
        from agents.messaging import MessageBus, TaskAssignment
        from memory.shared import SharedMemory
        s=self.session()
        model=SimpleNamespace(supports_tool_use=True, model='fake',
            chat_with_tools=AsyncMock(return_value={'content':[{'type':'text','text':'hello'}],'usage':{}}))
        agent=InteractivePentester(agent_id='pentester',bus=MessageBus(),llm=model,
                                  shared_memory=SharedMemory(),methodology=s.profile.methodology)
        agent.on_context_snapshot=s.context_inspector.capture
        result=await agent.execute(TaskAssignment(agent_name='pentester',task_type='interactive',context='hi'))
        self.assertTrue(result.success)
        self.assertTrue(s.context_inspector.messages)
        self.assertIn('User request:\nhi',s.context_inspector.render('1'))
