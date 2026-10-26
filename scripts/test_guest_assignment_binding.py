"""After discovery, revalidate the exact public job/repository instead of rescanning."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import guest_driver_listener as listener

class BindingTests(unittest.TestCase):
    def test_confirmed_foreign_binding_is_used_for_revalidation(self):
        response={'kind':'active','run_id':12,'job_id':34,'repository':'owner/other'}
        with patch.object(listener,'run_bounded',return_value=subprocess.CompletedProcess([],0,json.dumps(response),'')) as api:
            result=listener.assignment_observation('owner/repo','fixture',Path('/PUBLIC_POLICY'),response)
        self.assertEqual(response,result)
        command=api.call_args.args[0]
        for flag,value in [('--mode','revalidate'),('--run-id','12'),('--job-id','34'),('--assigned-repo','owner/other')]:
            self.assertEqual(value,command[command.index(flag)+1])
    def test_discovery_remains_bounded_and_malformed_output_is_not_authorization(self):
        with patch.object(listener,'run_bounded',return_value=subprocess.CompletedProcess([],1,'not-json','')) as api:
            self.assertEqual({'kind':'observation_error'},listener.assignment_observation('owner/repo','fixture',Path('/PUBLIC_POLICY')))
        self.assertNotIn('--mode',api.call_args.args[0])
        self.assertEqual(130,api.call_args.kwargs['timeout'])

if __name__=='__main__':unittest.main()
