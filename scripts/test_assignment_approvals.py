"""Public fixtures for merged-PR authorization and shared-group assignments."""
import json
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from current_job_scan import CurrentJobScanner
from queue_policy import QueuePolicy
from queue_scan import QueueScanner


def policy(repo='owner/repo'):
    return dict(repository=repo, events=['pull_request', 'push'],
                workflow_paths=['.github/workflows/ci.yml'],
                same_repository_pull_requests=True, push_branches=['main'])


def run(repo='owner/repo', number=1, repo_id=101):
    return dict(id=number, run_attempt=1, workflow_id=99, name='Build',
                path='.github/workflows/ci.yml', event='pull_request',
                head_sha='a'*40, head_branch='feature', status='in_progress',
                repository=dict(id=repo_id, full_name=repo),
                head_repository=dict(id=repo_id, full_name=repo),
                pull_requests=[{side:dict(repo=dict(id=repo_id)) for side in ('head','base')}])


class ApprovalTests(unittest.TestCase):
    def test_cached_proof_survives_merge_but_cannot_authorize_changed_identity(self):
        p=QueuePolicy(policy(),'owner/repo'); live=run(); merged={**live,'pull_requests':[]}; now=int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(p.allows(merged)); self.assertFalse(p.approved(merged,directory,now))
            p.remember(live,directory,now)
            self.assertTrue(p.approved(merged,directory,now+60))
            changes=[{'id':2},{'run_attempt':2},{'head_sha':'b'*40},{'path':'.github/workflows/other.yml'},
                     {'workflow_id':100},{'head_branch':'other'},{'event':'pull_request_target'},
                     {'head_repository':dict(id=202,full_name='fork/repo')},
                     {'repository':dict(id=202,full_name='owner/repo')},
                     {'pull_requests':None}, {'pull_requests':[{'head':dict(repo=dict(id=202))}]}]
            for change in changes:
                with self.subTest(change=change):self.assertFalse(p.approved({**merged,**change},directory,now+60))
            self.assertFalse(p.approved(merged,directory,now+p.APPROVAL_TTL+1))
            self.assertFalse(p.approved(merged,directory,now-1))
            changed=QueuePolicy({**policy(),'events':['pull_request']},'owner/repo')
            self.assertFalse(changed.approved(merged,directory,now+60))

    def test_forks_and_incomplete_metadata_never_create_approval(self):
        p=QueuePolicy(policy(),'owner/repo'); now=int(time.time())
        variants=[{**run(),'pull_requests':[]},{**run(),'head_repository':dict(id=202,full_name='fork/repo')},
                  {**run(),'head_sha':'invalid'}, {**run(),'run_attempt':None}]
        with tempfile.TemporaryDirectory() as directory:
            for value in variants:self.assertFalse(p.remember(value,directory,now))
            self.assertEqual([],list(Path(directory).iterdir()))

    def test_unprotected_and_symlinked_proof_is_rejected(self):
        p=QueuePolicy(policy(),'owner/repo'); live=run(); merged={**live,'pull_requests':[]};now=int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            p.remember(live,directory,now); f=Path(directory)/p.fingerprint/'1.json'
            f.chmod(0o644);self.assertFalse(p.approved(merged,directory,now))
            original=f.read_bytes();f.unlink();outside=Path(directory)/'public-fixture';outside.write_bytes(original)
            outside.chmod(0o600);f.symlink_to(outside);self.assertFalse(p.approved(merged,directory,now))

    def test_queue_discovery_records_fresh_api_proof_before_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); p=QueuePolicy(policy(),'owner/repo'); value=run()
            q=QueueScanner.__new__(QueueScanner);q.policy=p
            q.args=SimpleNamespace(repo='owner/repo',force_refresh=True,discovery_ttl=100,max_job_fetches=5,
                                   assignment_approval_dir=str(root/'approvals'))
            q.discovery_path=root/'discovery.json';q.now=int(time.time())
            q._discovery_lock=lambda:nullcontext();q._observation_lock=lambda:nullcontext()
            q._preflight_identity=lambda:None;q._runs=lambda state:[value]
            q._candidate_runs=lambda runs,state:(runs,[]);q._gh=lambda path:{'jobs':[]}
            self.assertEqual(1,len(q._discover()))
            self.assertTrue(p.approved({**value,'pull_requests':[]},str(root/'approvals'),int(time.time())))


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for repo,name in [('owner/repo','primary'),('owner/other','other')]:
            (self.root/(name+'.json')).write_text(json.dumps(policy(repo)))
        self.args=SimpleNamespace(repo='owner/repo',runner='fixture',workflow=[],
            policy_file=str(self.root/'primary.json'),assignment_repo=['owner/other'],
            assignment_policy_map={'owner/other':[str(self.root/'other.json')]},
            assignment_approval_dir=str(self.root/'approvals'),scan_timeout=60,max_api_calls=310,
            observation_lock_file=str(self.root/'lock'),gh_timeout=5,max_pages=3,result_cap=300,parallelism=1)
        self.s=CurrentJobScanner(self.args);self.actual=run('owner/other',2,202);self.actual['workflow_id']=77
        self.s._gh=self.api
    def tearDown(self):self.tmp.cleanup()
    def api(self,path):
        if '/actions/jobs/'in path:return dict(id=20,status='in_progress',runner_name='fixture')
        if '/actions/runs/'in path:return self.actual
        raise AssertionError(path)
    def pages(self,prefix,key):
        if key=='workflows':return [dict(id=77 if '/other/'in prefix else 99,name='Build',path='.github/workflows/ci.yml')]
        if key=='workflow_runs':return [self.actual] if 'status=queued'in prefix and '/other/'in prefix else []
        if key=='jobs':return [dict(id=20,status='in_progress',runner_name='fixture')]
        raise AssertionError(prefix)
    def test_shared_assignment_uses_its_own_policy_and_workflow_contract(self):
        self.s._pages=self.pages
        receipt=self.s.discover()
        self.assertEqual('active',receipt['kind']);self.assertEqual('owner/other',receipt['repository'])
        self.args.assigned_repo='owner/other';self.args.run_id=2;self.args.job_id=20
        self.assertEqual('active',self.s.revalidate()['kind'])
        # The already-running job remains visible even when its parent is queued.
        self.assertEqual(77,receipt['workflow_id'])
    def test_observed_repository_without_an_explicit_policy_is_still_rejected(self):
        self.s.assignment_policies.pop('owner/other')
        self.assertEqual('unexpected_assignment',self.s._confirm(2,20,{99:'Build'},'owner/other')['kind'])
    def test_fork_and_foreign_workflow_still_fail_closed(self):
        self.s._pages=self.pages
        for change in [{'head_repository':dict(id=303,full_name='fork/repo')}, {'path':'.github/workflows/unreviewed.yml'}]:
            with self.subTest(change=change):
                self.actual={**run('owner/other',2,202),'workflow_id':77,**change}
                self.assertEqual('policy_violation',self.s._confirm(2,20,{99:'Build'},'owner/other')['kind'])
    def test_live_policy_approval_is_retained_across_later_merge(self):
        self.s._pages=self.pages
        self.assertEqual('active',self.s._confirm(2,20,{99:'Build'},'owner/other')['kind'])
        self.actual['pull_requests']=[]
        self.assertEqual('active',self.s._confirm(2,20,{99:'Build'},'owner/other')['kind'])
        self.actual['run_attempt']=2
        self.assertEqual('policy_violation',self.s._confirm(2,20,{99:'Build'},'owner/other')['kind'])
    def test_map_cannot_read_a_policy_for_an_unobserved_repository(self):
        self.args.assignment_policy_map={'foreign/repo':['/PUBLIC_UNREAD_FIXTURE']}
        with self.assertRaisesRegex(Exception,'outside the observed scope'):CurrentJobScanner(self.args)


if __name__=='__main__':unittest.main()
