"""Shared organization groups require complete bounded assignment observation."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from current_job_scan import CurrentJobScanner, ScanError
from runner_group_repository_access import verify, RepositoryInaccessible

PRIMARY='SuvioMedia/Suvio'
FOREIGN='SuvioMedia/sdk'

class SharedAssignmentScopeTests(unittest.TestCase):
    def group(self,declared,selected,group=7,visibility='selected'):
        def api(cli,path,repo,timeout):
            if '/repositories?' in path:return {'total_count':len(selected),'repositories':[{'full_name':r} for r in selected]}
            if path.startswith('repos/'):return {'private':True,'visibility':'private'}
            return {'visibility':visibility}
        with patch.dict(os.environ,{'TARTCI_ASSIGNMENT_REPOSITORIES':'\n'.join(declared),'TARTCI_RUNNER_SCOPE':'org'}),patch('runner_group_repository_access.api',side_effect=api) as calls:
            receipt=verify(PRIMARY,group,'fixture',5)
        return receipt,calls.call_count

    def test_group_admits_exact_complete_declared_scope(self):
        receipt,calls=self.group([PRIMARY,FOREIGN],[PRIMARY,FOREIGN])
        self.assertEqual(receipt['registration_scope'],'organization-observed-repositories')
        # Group, its repository page, then each reachable repository's visibility.
        self.assertEqual(calls,4)

    def test_group_rejects_unobserved_repository(self):
        with self.assertRaises(RepositoryInaccessible):self.group([PRIMARY],[PRIMARY,FOREIGN])

    def test_group_rejects_changed_scope(self):
        with self.assertRaises(RepositoryInaccessible):self.group([PRIMARY,FOREIGN],[PRIMARY])

    def test_org_default_group_cannot_bypass_visibility_check(self):
        with self.assertRaises(RepositoryInaccessible):self.group([PRIMARY],[PRIMARY],group=1,visibility='all')

    def scanner(self,foreign=True,primary=False):
        args=SimpleNamespace(repo=PRIMARY,runner='fixture-runner',workflow=['Build and Test'],assignment_repo=[FOREIGN],policy_file=None,scan_timeout=5,observation_lock_file='/fixture/unused',observation_lock_timeout=5,parallelism=1)
        scanner=CurrentJobScanner(args)
        def pages(path,key):
            if path.endswith('/workflows'):return [{'id':99,'name':'Build and Test'}]
            if '/runs?status=' in path:return [{'id':100 if path.startswith('repos/'+PRIMARY+'/') else 200}]
            if '/100/jobs?' in path:return [{'id':101,'status':'in_progress','runner_name':'fixture-runner'}] if primary else []
            if '/200/jobs?' in path:return [{'id':201,'status':'in_progress','runner_name':'fixture-runner'}] if foreign else []
            raise AssertionError(path)
        def api(path):
            if '/jobs/' in path:return {'id':201 if '/201' in path else 101,'status':'in_progress','runner_name':'fixture-runner'}
            return {'id':200 if '/200' in path else 100,'status':'in_progress','workflow_id':99,'name':'Build and Test'}
        scanner._pages=pages;scanner._gh=api
        return scanner

    def test_foreign_assignment_is_observed_and_quarantined(self):
        receipt=self.scanner().discover()
        self.assertEqual(receipt['kind'],'unexpected_assignment')
        self.assertEqual(receipt['repository'],FOREIGN)
        self.assertEqual(receipt['job_id'],201)

    def test_two_assignments_across_repositories_are_ambiguous(self):
        self.assertEqual(self.scanner(primary=True).discover(),{'kind':'ambiguous_assignment','matches':2})

    def test_primary_assignment_keeps_exact_repository_identity(self):
        receipt=self.scanner(foreign=False,primary=True).discover()
        self.assertEqual(receipt['kind'],'active')
        self.assertEqual(receipt['job_id'],101)

    def test_foreign_api_failure_cannot_report_idle(self):
        scanner=self.scanner(foreign=False)
        original=scanner._pages
        def pages(path,key):
            if path.startswith('repos/'+FOREIGN+'/'):raise ScanError('foreign repository unavailable')
            return original(path,key)
        scanner._pages=pages
        with self.assertRaises(ScanError):scanner.discover()

if __name__=='__main__':unittest.main()
