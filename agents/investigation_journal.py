"""A bounded decision log for user-led work; no execution or target selection."""
import json
from agents.tools import Tool


class InvestigationJournal:
    STATUSES = ('active', 'ruled_out', 'blocked', 'complete')

    def __init__(self):
        self.entries = []

    def restore(self, entries):
        self.entries = [dict(e) for e in entries[-30:]]

    async def record(self, data):
        required = ('question', 'evidence', 'conclusion', 'next_step', 'status')
        for key in required:
            if not isinstance(data.get(key), str) or len(data[key]) > 2000:
                return {'error': f'{key} must be a string of at most 2000 characters'}
        if not data['question'].strip() or data['status'] not in self.STATUSES:
            return {'error': 'Provide a question and a valid status'}
        entry = next((e for e in self.entries if e['question'] == data['question']), None)
        if entry is None:
            if len(self.entries) >= 30:
                completed = next((e for e in self.entries if e['status'] != 'active'), None)
                if completed is None:
                    return {'error': 'Close an existing question before adding another'}
                self.entries.remove(completed)
            entry = {}
            self.entries.append(entry)
        else:
            self.entries.remove(entry)
            self.entries.append(entry)
        entry.update({key: data[key] for key in required})
        return {'recorded': True, 'status': entry['status'], 'questions': len(self.entries)}

    def briefing(self):
        if not self.entries:
            return 'No investigation decisions recorded. Direct questions need no journal. For multi-step work, record the question, evidence, and decision before changing direction.'
        active = [e for e in self.entries if e['status'] == 'active']
        recent_closed = [e for e in self.entries if e['status'] != 'active'][-5:]
        selected = active[-5:] + recent_closed
        compact = [{key: value[:600] for key, value in e.items()} for e in selected]
        text = f'{len(active)} active question(s); showing latest decisions.\n' + json.dumps(compact, ensure_ascii=False)
        if not active:
            text += '\nNo active questions remain. Summarize the evidence and limits or ask for missing prerequisites. Do not invent more work just to spend the remaining budget.'
        return text

    def tool(self):
        return Tool(
            name='record_investigation', category='memory',
            description=('Record or update a decision by question. Capture observed evidence, what it supports or rules out, '
                         'and the next step (empty if finished). This is a notebook, not an execution tool. '
                         'Use for multi-step work; not for greetings or simple answers.'),
            parameters={'type':'object','properties':{
                **{key:{'type':'string','maxLength':2000} for key in ('question','evidence','conclusion','next_step')},
                'status':{'type':'string','enum':list(self.STATUSES)}},
                'required':['question','evidence','conclusion','next_step','status']},
            handler=self.record)
