#!/usr/bin/env python3
"""One paid integration check: unsupported completion must not close a goal."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agents.goal import InvestigationGoal
from agents.goal_reviewer import GoalReviewer
from agents.llm_config import create_llm_for_agent
from profiles.loader import load_profile
from cost.tracker import CostTracker


async def main(args):
    directory = Path(args.directory)
    output = directory / 'reviewer-negative-control.json'
    if output.exists():
        raise ValueError('Control output exists; refusing to overwrite or repeat a paid check')
    run = json.loads((directory / 'results.json').read_text())
    if run['mode'] != 'real_model':
        raise ValueError('This control requires recorded real-model integration artifacts')
    fixed = next(c for c in run['cases'] if c['expected'] == 'not_reproduced' and c['passed'])
    cost = CostTracker()
    cost.add_cost('previous_integration_run', run['estimated_cost_usd'])
    provider = create_llm_for_agent('validator', load_profile('interactive').llm)
    reviewer = GoalReviewer(provider, cost, args.budget)
    observations = [{'id': f'E{i+1}', 'kind': 'recorded_tool_result', 'content': o, 'truncated': False}
                    for i, o in enumerate(fixed['observations'])]
    async def review(goal, claim):
        return await reviewer.review(goal, claim, observations)
    goal = InvestigationGoal(reviewer=review)
    goal.create('Demonstrate an ownership-check bypass allowing Alice to read Bob’s receipt.',
                ['Show an actual successful response containing Bob’s receipt while authenticated as Alice.'])
    start = time.monotonic()
    result = await goal.update_tool({'status': 'complete', 'progress': 'Completed cross-user receipt access.',
                                     'evidence': 'Alice successfully accessed Bob’s receipt using receipt ID 2.'})
    verdict = result.get('review', {})
    data = {'purpose': 'Framework completion-gate integration negative control, not a model benchmark',
            'source_case': fixed['case'], 'model': getattr(provider, 'model', 'unavailable'),
            'observations': observations, 'goal_status': goal.data['status'], 'review': verdict,
            'passed': goal.data['status'] == 'active' and verdict.get('approved') is False,
            'error': result.get('error'), 'elapsed_seconds': round(time.monotonic()-start, 3),
            'estimated_cost_usd': cost.get_total()-run['estimated_cost_usd'],
            'total_with_main_run_usd': cost.get_total()}
    output.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory')
    p.add_argument('--budget', type=float, default=4)
    asyncio.run(main(p.parse_args()))
