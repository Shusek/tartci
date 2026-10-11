"""Exercise prepared guest assignment checks through the real scanner CLI."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from guest_driver_listener import assignment_observation


class PreparedAssignmentObservationTests(unittest.TestCase):
    def test_actual_scanner_accepts_only_the_admitted_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'policy.json'
            policy.write_text(json.dumps({'repository': 'owner/repo', 'events': ['workflow_dispatch'],
                                          'workflow_paths': ['.github/workflows/smoke.yml']}))
            gh = root / 'gh'
            gh.write_text('''#!/usr/bin/env python3
import json, os, sys
path = sys.argv[-1]
repo = path.split('/')[2]
expected = 'other' if os.environ['VERDICT'] == 'foreign' else 'repo'
run = {'id': 1, 'workflow_id': 3, 'name': 'Smoke', 'status': 'in_progress',
       'event': 'pull_request' if os.environ['VERDICT'] == 'fork' else 'workflow_dispatch',
       'path': '.github/workflows/smoke.yml', 'repository': {'full_name': 'owner/' + repo}}
job = {'id': 2, 'status': 'in_progress', 'runner_name': 'fixture'}
if '/actions/workflows?' in path:
    result = {'total_count': 1, 'workflows': [{'id': 3, 'name': 'Smoke', 'path': '.github/workflows/smoke.yml'}]}
elif '/actions/runs?status=in_progress' in path:
    result = {'total_count': int(repo == expected), 'workflow_runs': [run] if repo == expected else []}
elif '/actions/runs/1/jobs?' in path:
    result = {'total_count': 1, 'jobs': [job]}
elif path.endswith('/actions/jobs/2'):
    result = job
elif path.endswith('/actions/runs/1'):
    result = run
else:
    raise SystemExit('unexpected API path: ' + path)
print(json.dumps(result))
''')
            gh.chmod(0o700)
            env = {'TARTCI_GH_CLI': str(gh), 'TARTCI_ASSIGNMENT_REPOSITORIES': 'owner/other',
                   'TARTCI_QUEUE_OBSERVATION_LOCK_FILE': str(root / 'lock'),
                   'TARTCI_QUEUE_OBSERVATION_BACKOFF_FILE': str(root / 'backoff.json')}
            for mode, verdict in [('valid', 'active'), ('foreign', 'unexpected_assignment'), ('fork', 'policy_violation')]:
                with self.subTest(mode=mode), patch.dict(os.environ, {**env, 'VERDICT': mode}):
                    self.assertEqual(assignment_observation('owner/repo', 'fixture', policy), verdict)


if __name__ == '__main__':
    unittest.main()
