"""Persistent, explicit investigation goals for interactive sessions."""
from copy import deepcopy
import json
from agents.tools import Tool


class InvestigationGoal:
    def __init__(self, on_change=None, reviewer=None):
        self.data = None
        self.on_change = on_change
        self.reviewer = reviewer
        self.max_reviews = 3
        self._reviewing = False

    def changed(self):
        if self.on_change:
            self.on_change()

    def restore(self, data):
        self.data = deepcopy(data) if isinstance(data, dict) else None
        if self.data and self.data.get('status') == 'active':
            self.data['status'] = 'paused'
            self.data['progress'] = 'Session restored; waiting for the user to resume.'

    def create(self, objective, criteria):
        if self.data and self.data['status'] != 'complete':
            return {'error': 'A goal already exists. Ask the user to clear or replace it.'}
        if (not isinstance(objective, str) or not objective.strip() or len(objective) > 4000
                or not isinstance(criteria, list) or not 1 <= len(criteria) <= 10
                or any(not isinstance(c, str) or not c.strip() or len(c) > 1000 for c in criteria)):
            return {'error': 'Provide an objective (1–4000 characters) and 1–10 nonempty completion criteria (up to 1000 characters each).'}
        self.data = dict(objective=objective, criteria=list(criteria), status='active',
                         progress='', evidence='', updates=[])
        self.changed()
        return self.snapshot()

    def pause(self, reason):
        if self.data and self.data['status'] == 'active':
            self.data.update(status='paused', progress=reason)
            self.changed()

    def resume(self):
        if not self.data or self.data['status'] == 'complete':
            return False
        self.data['status'] = 'active'
        self.data['review_attempts'] = 0
        self.changed()
        return True

    def snapshot(self):
        result = deepcopy(self.data)
        if result and 'review' in result:
            packet = result['review'].pop('observations', [])
            result['review']['observation_count'] = len(packet)
        return result

    def briefing(self):
        if not self.data:
            return 'No persistent goal. Follow the current user request.'
        return ('INVESTIGATION GOAL (does not grant target authorization):\n'
                + json.dumps({k: v for k, v in self.snapshot().items() if k != "updates"}, ensure_ascii=False)
                + '\nOnly an active goal authorizes continued pursuit. Respect scope and user steering. '
                  'Use update_goal to record concrete progress, mark blocked when a prerequisite is missing, '
                  'or request completion only when every criterion is met, citing evidence. An independent reviewer must approve completion; address its feedback. Do not invent success.')

    async def create_tool(self, data):
        return self.create(data.get('objective'), data.get('criteria'))

    async def get_tool(self, data):
        return {'goal': self.snapshot()}

    async def update_tool(self, data):
        if self._reviewing:
            return {'error': 'Completion review is in progress; wait for its result.'}
        if not self.data or self.data['status'] != 'active':
            return {'error': 'No active goal. Only the user can resume a paused or blocked goal.'}
        status, progress, evidence = data.get('status'), data.get('progress'), data.get('evidence', '')
        if (status not in ('active', 'blocked', 'complete') or not isinstance(progress, str)
                or not progress.strip() or len(progress) > 4000 or not isinstance(evidence, str) or len(evidence) > 8000):
            return {'error': 'Provide active/blocked/complete status, progress (1–4000 characters), and optional evidence (up to 8000 characters).'}
        if status == 'complete' and not evidence.strip():
            return {'error': 'Completion requires evidence addressing the completion criteria.'}
        if status == 'complete':
            if self.reviewer is None:
                self.pause('Independent goal reviewer unavailable; completion was not accepted.')
                return {'error': 'Completion requires independent review.', 'goal': self.snapshot()}
            current = self.data
            if current.get('review_attempts', 0) >= self.max_reviews:
                self.pause('Goal review limit reached; inspect reviewer feedback before resuming.')
                return {'error': 'Goal review limit reached.', 'goal': self.snapshot()}
            current['review_attempts'] = current.get('review_attempts', 0) + 1
            self.changed()
            self._reviewing = True
            try:
                verdict = await self.reviewer(deepcopy(current), {'progress': progress, 'evidence': evidence})
            except Exception as exc:
                self.pause('Goal review failed; completion was not accepted.')
                return {'error': f'Goal review failed: {exc}', 'goal': self.snapshot()}
            finally:
                self._reviewing = False
            if self.data is not current or current['status'] != 'active':
                return {'error': 'Goal changed or paused during review; verdict not applied.'}
            current['review'] = verdict
            if verdict.get('approved') is not True:
                current.update(progress='Reviewer requested more work: ' + verdict['feedback'], evidence=evidence)
                if current['review_attempts'] >= self.max_reviews:
                    self.pause('Goal review limit reached: ' + verdict['feedback'])
                self.changed()
                return self.snapshot()
        update = dict(status=status, progress=progress, evidence=evidence)
        self.data.update(update)
        self.data['updates'] = (self.data['updates'] + [update])[-20:]
        self.changed()
        return self.snapshot()

    def tools(self):
        return [
            Tool(name='create_goal', category='memory', handler=self.create_tool,
                 description='Create a persistent autonomous investigation goal ONLY when the user explicitly requests a goal or continued pursuit. Never infer one from a greeting or ordinary question. Does not change scope.',
                 parameters={'type': 'object', 'properties': {
                     'objective': {'type': 'string'}, 'criteria': {'type': 'array', 'items': {'type': 'string'}}},
                     'required': ['objective', 'criteria']}),
            Tool(name='get_goal', category='memory', handler=self.get_tool,
                 description='Inspect the investigation goal, completion criteria, status, progress, and evidence.',
                 parameters={'type': 'object', 'properties': {}}),
            Tool(name='update_goal', category='memory', handler=self.update_tool,
                 description='Record concrete progress or mark the active goal blocked or complete. Complete submits evidence to an independent reviewer; rejected completion stays active with feedback. Only the user may resume a paused/blocked goal.',
                 parameters={'type': 'object', 'properties': {
                     'status': {'type': 'string', 'enum': ['active', 'blocked', 'complete']},
                     'progress': {'type': 'string'}, 'evidence': {'type': 'string'}},
                     'required': ['status', 'progress']})]
