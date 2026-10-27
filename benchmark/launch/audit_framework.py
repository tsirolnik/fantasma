#!/usr/bin/env python3
"""Audit saved integration-run artifacts; no model calls or network access."""
import argparse
import json
from pathlib import Path


def audit(directory):
    directory = Path(directory)
    run = json.loads((directory / 'results.json').read_text())
    cases = []
    for case in run['cases']:
        snapshots = list((directory / 'sessions' / case['case']).glob('*.json'))
        snapshot = json.loads(snapshots[0].read_text()) if len(snapshots) == 1 else {}
        goal = snapshot.get('goal', {})
        review = goal.get('review', {})
        refs = {o['id'] for o in review.get('observations', [])}
        criteria = review.get('criteria', [])
        expected_indices = set(range(len(goal.get('criteria', []))))
        complete_review = (review.get('approved') is True and bool(expected_indices)
                           and len(criteria) == len(expected_indices)
                           and {c.get('index') for c in criteria} == expected_indices
                           and all(c.get('met') is True and c.get('evidence_refs')
                                   and set(c['evidence_refs']) <= refs for c in criteria))
        recorded = {o['observation_id'] for o in case['observations']}
        assessment = case.get('assessment') or {}
        assessment_linked = bool(assessment.get('observation_ids')) and set(assessment['observation_ids']) <= recorded
        history = snapshot.get('history', [])
        reasoning = any('reasoning_content' in m or m.get('_provider_history') for m in history)
        checks = {
            'session_saved_with_history': bool(snapshot.get('id') and history),
            'scope_persisted': snapshot.get('scope') == ['127.0.0.1'],
            'goal_status_persisted': goal.get('status') == case['goal_status'],
            'assessment_cites_recorded_responses': assessment_linked,
            'completed_goal_has_supported_review': goal.get('status') != 'complete' or complete_review,
            'retention_disabled_omits_optional_metadata': case['retain_thinking'] or not reasoning,
            'run_has_no_error': case.get('error') is None,
        }
        cases.append({'case': case['case'], 'checks': checks, 'all_passed': all(checks.values()),
                      'reasoning_metadata_observed_in_saved_history': reasoning,
                      'goal_completed': goal.get('status') == 'complete',
                      'fixture_conclusion_correct': case['passed']})
    report = {'mode': run['mode'], 'cases': cases,
              'all_passed': bool(cases) and all(c['all_passed'] for c in cases) and not run.get('error'),
              'limitations': 'Post-run artifact audit, not independent network capture. It does not prove reasoning was replayed on the wire. Resume/no-autostart and rejection/cancellation/budget gates have separate offline regression tests.'}
    (directory / 'framework-audit.json').write_text(json.dumps(report, indent=2))
    lines = ['# Framework integration checks', '', f'Mode: `{run["mode"]}`. Overall artifact checks passed: **{report["all_passed"]}**.', '',
             '| Case | Saved history | Scope persisted | Evidence linked | Completion gate | Retention off honored | No error |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for case in cases:
        c = case['checks']
        lines.append('| ' + case['case'] + ' | ' + ' | '.join(str(c[k]) for k in (
            'session_saved_with_history', 'scope_persisted', 'assessment_cites_recorded_responses',
            'completed_goal_has_supported_review', 'retention_disabled_omits_optional_metadata', 'run_has_no_error')) + ' |')
    lines += ['', report['limitations'], '', 'Fixture conclusions are functional test outputs, not a ranking of models or a broad vulnerability-discovery score.']
    (directory / 'framework-audit.md').write_text('\n'.join(lines)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    args = parser.parse_args()
    print(json.dumps(audit(args.directory), indent=2))
