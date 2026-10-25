import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from agents.investigation_journal import InvestigationJournal
from agents.interactive import InteractivePentester
from agents.messaging import MessageBus, TaskAssignment
from memory.shared import SharedMemory
from profiles.loader import load_profile


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_same_question_retains_negative_evidence(self):
        journal=InvestigationJournal()
        data=dict(question='Does the fixture contain the record?',evidence='',conclusion='Unknown',next_step='Read the local fixture',status='active')
        await journal.record(data)
        data.update(evidence='Fixture has no matching record',conclusion='Not present in this fixture',next_step='',status='ruled_out')
        await journal.record(data)
        self.assertEqual(len(journal.entries),1)
        self.assertIn('No active questions remain',journal.briefing())
        restored=InvestigationJournal();restored.restore(journal.entries)
        self.assertEqual(restored.entries,journal.entries)
        self.assertIn('no matching record',restored.briefing())

    async def test_no_network_or_scope_needed_for_notebook(self):
        profile=load_profile('interactive')
        decision=dict(question='Is the supplied example complete?',evidence='Missing required field',conclusion='Blocked on missing input',next_step='Ask user for the field',status='blocked')
        replies=[{'content':[{'type':'tool_use','id':'note-1','name':'record_investigation','input':decision}],'usage':{}},
                 {'content':[{'type':'text','text':'The example is missing a required field.'}],'usage':{}}]
        model=SimpleNamespace(supports_tool_use=True,model='fake',chat_with_tools=AsyncMock(side_effect=replies))
        agent=InteractivePentester(agent_id='pentester',bus=MessageBus(),llm=model,
                                  shared_memory=SharedMemory(),methodology=profile.methodology)
        agent._allowed_domains=set()
        captured=[]
        agent.on_context_snapshot=lambda messages, tools: captured.append(str(messages))
        result=await agent.execute(TaskAssignment(agent_name='pentester',task_type='interactive',context='Review this local fixture'))
        self.assertTrue(result.success)
        self.assertEqual(model.chat_with_tools.await_count,2)
        self.assertEqual(agent.journal.entries[0]['status'],'blocked')
        self.assertIn('No active questions remain',captured[-1])
        self.assertNotIn('Claim ONE target',captured[-1])
        self.assertNotIn('Score:',captured[-1])

    async def test_invalid_entries_do_not_mutate_journal(self):
        journal=InvestigationJournal()
        self.assertIn('error',await journal.record({'question':'x'}))
        self.assertEqual(journal.entries,[])
