"""Execute actual cleanup with a fake API and real jq; never touch a runner."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('jq'),'requires jq used by the production adapter')
class CleanupTests(unittest.TestCase):
    def test_exact_absence_is_confirmed_and_busy_or_incomplete_responses_are_not(self):
        source=(ROOT/'providers/tart-linux/runner.sh').read_text()
        function=re.search(r'(?ms)^delete_linux_runner_registration\(\)\{.*?^\}',source).group(0)
        for mode,success,deletes in [('idle',True,1),('absent',True,0),('busy',False,0),
                                     ('unfiltered',False,0),('delete_error',False,3),('unconfirmed_delete',False,3)]:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);state=root/'state.json';calls=root/'calls.jsonl';fake=root/'fake-gh'
                state.write_text(json.dumps({'mode':mode,'present':mode!='absent'}))
                fake.write_text('#!'+sys.executable+'\n'+f'''
import sys,json,subprocess
from pathlib import Path
state=Path({str(state)!r});calls=Path({str(calls)!r});args=sys.argv[1:];s=json.loads(state.read_text())
is_delete='DELETE' in args
with calls.open('a')as f:f.write(json.dumps(args)+'\\n')
if is_delete:
 if s['mode']=='delete_error':raise SystemExit(1)
 if s['mode']!='unconfirmed_delete':s['present']=False;state.write_text(json.dumps(s))
 raise SystemExit(0)
if s['mode']=='unfiltered':result={{'total_count':428,'runners':[{{'id':1,'name':'unrelated','busy':False}}]}}
else:
 runners=[{{'id':101,'name':'fixture','busy':s['mode']=='busy'}}] if s['present'] else []
 result={{'total_count':len(runners),'runners':runners}}
query=args[args.index('--jq')+1]
r=subprocess.run([{shutil.which('jq')!r},'-r',query],input=json.dumps(result),text=True)
raise SystemExit(r.returncode)
''');fake.chmod(0o700)
                script=function+'\nsleep(){ :; }\nGH_CLI='+shlex.quote(str(fake))+'\nRUNNER_API_ROOT=orgs/fixture/actions/runners\ndelete_linux_runner_registration fixture\n'
                result=subprocess.run(['bash','-c',script],capture_output=True,text=True)
                requests=[json.loads(l)for l in calls.read_text().splitlines()]
                self.assertEqual(0 if success else 1,result.returncode,result.stderr)
                self.assertEqual(deletes,sum('DELETE'in r for r in requests))
                self.assertEqual(success,'cleanup=confirmed'in result.stderr)
                if mode=='idle':self.assertEqual([False,True,False],['DELETE'in r for r in requests])

if __name__=='__main__':unittest.main()
