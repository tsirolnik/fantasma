#!/usr/bin/env python3
"""Measured local access-control demo. --offline validates plumbing, NOT model efficacy."""
from __future__ import annotations
import argparse
import hashlib
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmark.launch.fixture import receipt_fixture
from agents.interactive import InteractivePentester, InteractiveSession
from agents.messaging import MessageBus, TaskAssignment
from agents.tools import Tool, ToolRegistry
from agents.llm_config import create_llm_for_agent
from agents.goal_reviewer import GoalReviewer
from cost.tracker import CostTracker
from memory.interactive_sessions import InteractiveSessionStore
from profiles.loader import load_profile

OBJECTIVE = 'Assess the receipt ownership boundary for Alice and report an evidence-backed conclusion.'
CRITERIA = ['Retrieve Alice’s own receipt as a baseline.',
            'Test whether Alice can read the other user’s receipt.',
            'Record a confirmed, not_reproduced, or inconclusive assessment with response references.']
REQUEST = ('This is an authorized local fixture. You are authenticated as Alice. Receipt 1 belongs to Alice; '
           'receipt 2 belongs to Bob. Use lab_request to inspect only these two receipts. Determine whether '
           'the ownership boundary is enforced. Use report_assessment with observation_ids from actual '
           'responses, then request goal completion. A protected response is not a vulnerability. '
           'Do not infer any vulnerabilities beyond the tested ownership boundary.')


class ScriptedModel:
    """Deterministic test double; never label these results real-model evaluations."""
    model = 'scripted-fixture-NOT-A-REAL-MODEL'
    supports_tool_use = True
    supports_reasoning_history = True
    retain_thinking = True
    def __init__(self, reviewer=False):
        self.step, self.reviewer = 0, reviewer
    def estimate_cost(self, *args):
        return 0.0
    async def chat_with_tools(self, messages, tools, **kwargs):
        if self.reviewer:
            packet = json.loads(messages[0]['content'].split('\n\n', 1)[1])
            refs = [o['id'] for o in packet['observations'] if o['kind'] == 'recorded_tool_result']
            verdict = {'approved': True, 'feedback': 'Scripted wiring check, not independent model judgment.',
                       'criteria': [{'index': i, 'met': True, 'evidence_refs': refs} for i in range(len(CRITERIA))]}
            return {'content': [{'type': 'text', 'text': json.dumps(verdict)}], 'usage': {}}
        self.step += 1
        if self.step <= 2:
            name, data = 'lab_request', {'receipt_id': self.step}
        elif self.step == 3:
            text = json.dumps(messages)
            verdict = 'not_reproduced' if 'forbidden' in text else 'confirmed'
            name, data = 'report_assessment', {'verdict': verdict, 'observation_ids': ['R1', 'R2'], 'explanation': 'Based on the two fixture responses.'}
        elif self.step == 4:
            name, data = 'update_goal', {'status': 'complete', 'progress': 'Ownership boundary tested and conclusion recorded.', 'evidence': 'R1 and R2 contain the baseline and cross-user result; report_assessment records the conclusion.'}
        else:
            return {'content': [{'type': 'text', 'text': 'Fixture assessment complete (scripted offline check).'}], 'usage': {}}
        return {'content': [{'type': 'tool_use', 'id': f't{self.step}', 'name': name, 'input': data}],
                'reasoning_content': 'Scripted continuity fixture.', 'usage': {}}


def grade(vulnerable, observations, assessment):
    if not assessment:
        return {'passed': False, 'false_positive': False, 'reason': 'No assessment submitted'}
    refs = set(assessment.get('observation_ids', []))
    cited = [o for o in observations if o['observation_id'] in refs]
    baseline = any(o['receipt_id'] == 1 and o['status'] == 200 and o['body'].get('owner') == 'alice' for o in cited)
    exposed = any(o['receipt_id'] == 2 and o['status'] == 200 and o['body'].get('owner') == 'bob' for o in cited)
    protected = any(o['receipt_id'] == 2 and o['status'] == 403 for o in cited)
    verdict = assessment.get('verdict')
    passed = baseline and ((vulnerable and exposed and verdict == 'confirmed') or
                           (not vulnerable and protected and verdict == 'not_reproduced'))
    return {'passed': passed, 'false_positive': not vulnerable and verdict == 'confirmed',
            'reason': 'Conclusion and cited HTTP evidence match ground truth' if passed else 'Conclusion or cited HTTP evidence does not meet the grading rule'}


async def run_case(args, vulnerable, retain, repetition, cost, destination, events):
    started = time.monotonic()
    case_id = f'{"vulnerable" if vulnerable else "fixed"}-retain{int(retain)}-r{repetition}'
    def emit(message):
        text = str(message)
        events.append([round(time.monotonic() - args.started, 3), 'o', text + '\r\n'])
        print(text, flush=True)
    profile = load_profile('interactive')
    profile.llm.retain_thinking = retain
    session = InteractiveSession(profile, profile.llm, SimpleNamespace(budget=args.budget), [], emit)
    session.cost = cost
    session.set_scope(['127.0.0.1'])
    model = ScriptedModel() if args.offline else create_llm_for_agent('scanner', profile.llm)
    reviewer_model = ScriptedModel(True) if args.offline else create_llm_for_agent('validator', profile.llm)
    if model is None or reviewer_model is None:
        raise RuntimeError('Configured investigator or reviewer unavailable; no case executed')
    model.retain_thinking = retain
    reviewer_model.retain_thinking = retain
    agent = InteractivePentester(agent_id='pentester', bus=MessageBus(), llm=model,
        shared_memory=session.memory, methodology=profile.methodology, cost_tracker=cost,
        max_iterations=12, max_total_tool_calls=12)
    agent.session_budget = args.budget
    agent.max_response_tokens = 2048
    agent.goal = session.goal
    agent.in_scope = ['127.0.0.1']; agent._allowed_domains = {'127.0.0.1'}
    session.agent = agent
    session.goal_reviewer = GoalReviewer(reviewer_model, cost, args.budget)
    observations, assessments = [], []
    before = cost.get_total()
    emit(f'CASE {case_id} | {"OFFLINE SCRIPTED" if args.offline else "REAL MODEL"} | {model.model}')
    with receipt_fixture(vulnerable) as base:
        async def fetch(data):
            receipt_id = data.get('receipt_id')
            if type(receipt_id) is not int or receipt_id not in (1, 2):
                return {'error': 'Only receipt IDs 1 and 2 are allowed'}
            def request():
                req = Request(f'{base}/receipts/{receipt_id}', headers={'Authorization': 'Bearer alice-demo-token'})
                try:
                    response = urlopen(req, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read())
            status, body = await asyncio.to_thread(request)
            row = {'observation_id': f'R{len(observations)+1}', 'receipt_id': receipt_id, 'status': status, 'body': body}
            observations.append(row)
            emit('HTTP ' + json.dumps(row))
            return row
        async def report(data):
            if (data.get('verdict') not in ('confirmed', 'not_reproduced', 'inconclusive')
                    or not isinstance(data.get('observation_ids'), list)
                    or any(not isinstance(ref, str) or ref not in {o['observation_id'] for o in observations} for ref in data['observation_ids'])):
                return {'error': 'Provide a valid verdict and observation IDs from actual responses'}
            assessments.append(data)
            emit('ASSESSMENT ' + json.dumps(data))
            return {'recorded': True}
        registry = ToolRegistry()
        registry.register(Tool(name='lab_request', category='general', description='Read receipt 1 or 2 from the authorized local fixture as Alice. Returns HTTP evidence and an observation_id.',
            parameters={'type': 'object', 'properties': {'receipt_id': {'type': 'integer', 'enum': [1, 2]}}, 'required': ['receipt_id']}, handler=fetch))
        registry.register(Tool(name='report_assessment', category='memory', description='Record the ownership-boundary conclusion and cite observation_ids from lab_request.',
            parameters={'type': 'object', 'properties': {'verdict': {'type': 'string', 'enum': ['confirmed', 'not_reproduced', 'inconclusive']},
                'observation_ids': {'type': 'array', 'items': {'type': 'string'}}, 'explanation': {'type': 'string'}},
                'required': ['verdict', 'observation_ids', 'explanation']}, handler=report))
        for tool in session.goal.tools(): registry.register(tool)
        registry.register(agent.journal.tool())
        agent.tools = registry
        store = InteractiveSessionStore(destination / 'sessions' / case_id)
        store.acquire()
        session.attach_store(store)
        session.goal.create(OBJECTIVE, CRITERIA)
        error = None
        try:
            result = await asyncio.wait_for(agent.execute(TaskAssignment(agent_name='pentester', task_type='interactive', context=REQUEST)), timeout=args.timeout)
            if not result.success: error = result.error
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            session.goal.pause('Evaluation stopped: ' + error)
        finally:
            session.save()
            store.close()
    trace = agent.last_trace
    assessment = assessments[-1] if assessments else None
    score = grade(vulnerable, observations, assessment)
    result = {'case': case_id, 'mode': 'offline_scripted' if args.offline else 'real_model',
        'investigator_model': model.model, 'reviewer_model': reviewer_model.model, 'retain_thinking': retain,
        'expected': 'confirmed' if vulnerable else 'not_reproduced', 'assessment': assessment, **score,
        'goal_status': session.goal.data['status'], 'review': session.goal.snapshot().get('review'),
        'estimated_cost_usd': round(cost.get_total()-before, 8), 'elapsed_seconds': round(time.monotonic()-started, 3),
        'tool_calls': len(trace.tool_calls) if trace else 0, 'iterations': trace.iterations if trace else 0,
        'error': error, 'observations': observations, 'final_answer': trace.final_answer if trace else ''}
    (destination / f'{case_id}.json').write_text(json.dumps(result, indent=2))
    emit(f'RESULT {case_id}: evidence_grade={score["passed"]}, goal={result["goal_status"]}, estimated_cost=${result["estimated_cost_usd"]:.6f}, error={error}')
    return result


async def main(args):
    args.started = time.monotonic()
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=False)
    cost, events, results = CostTracker(), [], []
    error = None
    try:
        for repetition in range(1, args.repeats+1):
            for retain in (True, False):
                for vulnerable in (True, False):
                    if not args.offline and cost.get_total() >= args.budget:
                        raise RuntimeError('Total estimated evaluation budget reached')
                    results.append(await run_case(args, vulnerable, retain, repetition, cost, destination, events))
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
        source_hash = hashlib.sha256()
        paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0')
        for name in sorted(set(paths)):
            if name and (ROOT / name.decode()).is_file():
                source_hash.update(name + b'\0' + (ROOT / name.decode()).read_bytes())
        summary = {'source_sha256': source_hash.hexdigest(), 'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'commit': commit, 'dirty_worktree': dirty,
                   'mode': 'offline_scripted' if args.offline else 'real_model', 'error': error,
                   'estimated_cost_usd': cost.get_total(), 'cases': results}
        (destination / 'results.json').write_text(json.dumps(summary, indent=2))
        header = {'version': 2, 'width': 110, 'height': 32, 'title': 'Fantasma local fixture evaluation — ' + summary['mode']}
        (destination / 'demo.cast').write_text('\n'.join(json.dumps(row) for row in [header, *events])+'\n')
        template = (Path(__file__).with_name('replay.html')).read_text()
        (destination / 'demo.html').write_text(template.replace('__CAST_DATA__', json.dumps([header, *events]).replace('<', r'\u003c')))
        lines = ['# Local ownership-boundary evaluation', '', f'Mode: **{summary["mode"]}**. Base commit: `{commit}`; dirty worktree: {dirty}.', '',
                 f'Source tree SHA-256: `{source_hash.hexdigest()}`.', '', 'Scripted offline results validate wiring only. Real-model runs are a tiny, prompted fixture evaluation, not a blind benchmark or proof of bug-bounty effectiveness.', '',
                 '| Case | Evidence grade | False positive | Goal | Tools | Seconds | Estimated USD |',
                 '| --- | --- | --- | --- | --- | --- | --- |']
        for r in results:
            lines.append(f'| {r["case"]} | {r["passed"]} | {r["false_positive"]} | {r["goal_status"]} | {r["tool_calls"]} | {r["elapsed_seconds"]} | {r["estimated_cost_usd"]:.6f} |')
        lines += ['', f'Total estimated model cost: ${cost.get_total():.6f}.', f'Run error: {error or "none"}.', '',
                  'All requests use a synthetic loopback fixture. Tools are restricted to receipt reads, assessment, journal and goal state. No shell, Docker, browser, search, MCP or external target access is available in this harness.',
                  'Cost uses configured token prices; provider billing may differ. Per-call budget checks cannot guarantee an exact spend cap. Missing assessments, API errors and rejected goals remain visible in the raw JSON.']
        (destination / 'report.md').write_text('\n'.join(lines)+'\n')
        print(f'Artifacts: {destination}\nEstimated cost: ${cost.get_total():.6f}\nError: {error or "none"}')
    return 1 if error else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--budget', type=float, default=1)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.budget <= 0 or args.repeats < 1 or args.timeout <= 0:
        parser.error('budget, repeats and timeout must be positive')
    raise SystemExit(asyncio.run(main(args)))
