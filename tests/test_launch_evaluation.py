import json
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from benchmark.launch.fixture import receipt_fixture
from benchmark.launch.run import grade


class LaunchEvaluationTests(unittest.TestCase):
    def test_actual_fixture_boundary_and_grading(self):
        for vulnerable in (True, False):
            observations = []
            with receipt_fixture(vulnerable) as url:
                for receipt_id in (1, 2):
                    req = Request(f'{url}/receipts/{receipt_id}', headers={'Authorization': 'Bearer alice-demo-token'})
                    try: response = urlopen(req)
                    except HTTPError as error: response = error
                    with response:
                        observations.append({'observation_id': f'R{receipt_id}', 'receipt_id': receipt_id,
                                             'status': response.status, 'body': json.loads(response.read())})
            report = {'verdict': 'confirmed' if vulnerable else 'not_reproduced', 'observation_ids': ['R1', 'R2']}
            self.assertTrue(grade(vulnerable, observations, report)['passed'])
            self.assertFalse(grade(vulnerable, observations, dict(report, observation_ids=['R1']))['passed'])
            self.assertFalse(grade(vulnerable, observations, None)['passed'])
            if not vulnerable:
                wrong = grade(False, observations, dict(report, verdict='confirmed'))
                self.assertTrue(wrong['false_positive'])
                self.assertFalse(wrong['passed'])

    def test_artifact_audit_rejects_unreviewed_completion(self):
        import tempfile
        from pathlib import Path
        from benchmark.launch.audit_framework import audit
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'sessions' / 'case'
            path.mkdir(parents=True)
            snapshot = {'id': 'fixture', 'scope': ['127.0.0.1'], 'history': [{'role': 'user', 'content': 'fixture'}],
                        'goal': {'status': 'complete', 'criteria': ['Show evidence']}}
            (path / 'fixture.json').write_text(json.dumps(snapshot))
            case = {'case': 'case', 'goal_status': 'complete', 'observations': [{'observation_id': 'R1'}],
                    'assessment': {'observation_ids': ['R1']}, 'retain_thinking': False, 'error': None, 'passed': True}
            (root / 'results.json').write_text(json.dumps({'mode': 'offline_scripted', 'cases': [case]}))
            self.assertFalse(audit(root)['all_passed'])
            snapshot['goal']['review'] = {'approved': True, 'observations': [{'id': 'E1'}],
                                         'criteria': [{'index': 0, 'met': True, 'evidence_refs': ['E1']}]}
            (path / 'fixture.json').write_text(json.dumps(snapshot))
            self.assertTrue(audit(root)['all_passed'])
