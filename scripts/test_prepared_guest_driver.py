"""Whole provider local lifecycle with a fake VM and a real guest-driver ABI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import signal
import unittest

ROOT=Path(__file__).resolve().parents[1]

class PreparedGuestDriverTests(unittest.TestCase):
    def execute(self,provider,fail=False,github=False,drain=False,lease_budget=False,delayed_delete=False,admission=None,pool_drain=False,dhcp_verifying=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bin=root/'bin';bin.mkdir();(root/'vms').mkdir()
            driver=bin/'driver'
            driver.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['FIXTURE'])
with (p/'driver-calls').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='preflight' and os.environ.get('DRAIN_DURING_PREFLIGHT')=='1':
 Path(os.environ['TARTCI_POOL_STATE_FILE']).write_text('draining\\n')
if sys.argv[1]=='run-jit':
 assert sys.stdin.buffer.read()==b'ZmFrZS1qaXQ='
 print('Running job: fixture',flush=True)
 if os.environ.get('DRAIN_JOB')=='1':
  import time
  (p/'assigned').touch();time.sleep(3);(p/'finished').touch()
 print('Job fixture completed with result: Succeeded',flush=True)
if sys.argv[1]=='run-local':
 print('fixture ordinary CI job')
 raise SystemExit(9 if os.environ.get('FAIL_JOB')=='1' else 0)
''');driver.chmod(0o700)
            tart=bin/'tart'
            tart.write_text('''#!/usr/bin/env python3
import json,os,sys,time,signal
from pathlib import Path
root=Path(os.environ['FIXTURE']);a=sys.argv[1:]
with (root/'tart-calls').open('a') as f:f.write(json.dumps(a)+'\\n')
if a[0]=='clone':
 (root/'present').touch();(root/'vm-name').write_text(a[2])
elif a[0]=='run':
 (root/'run-pid').write_text(str(os.getpid()));time.sleep(300)
elif a[0]=='stop' and (root/'run-pid').exists():
 try:os.kill(int((root/'run-pid').read_text()),signal.SIGTERM)
 except ProcessLookupError:pass
elif a[0]=='delete':
 if os.environ.get('DELAY_DELETE')=='1' and not (root/'delete-once').exists():
  (root/'delete-once').touch();raise SystemExit(7)
 (root/'present').unlink(missing_ok=True)
elif a[0]=='list':
 print(json.dumps([{'Name':(root/'vm-name').read_text(),'OS':'darwin','State':'running'}] if (root/'present').exists() else []))
''');tart.chmod(0o700)
            for name in ['qemu-img','qemu-system-aarch64','ssh','fake-gh']:
                script=bin/name
                if name=='qemu-system-aarch64':text='#!/bin/bash\nexec sleep 300\n'
                elif name=='fake-gh' and github:text="#!/usr/bin/env python3\nimport sys,json,os\nfrom pathlib import Path\nwith (Path(os.environ['FIXTURE'])/'gh-calls').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\nif any('generate-jitconfig' in a for a in sys.argv):\n assert '-f' in sys.argv and 'labels[]=self-hosted' in sys.argv\n print('ZmFrZS1qaXQ=')\nelif sys.argv[1:2]==['api'] and sys.argv[-1].startswith('repos/') and sys.argv[-1].count('/')==2:print(json.dumps({'private':True,'visibility':'private'}))\nelif '--jq' not in sys.argv:print(json.dumps({'runners':[]}))\n"
                elif name=='fake-gh':text='#!/bin/bash\necho unexpected-gh >&2; exit 99\n'
                elif name=='ssh':text='#!/usr/bin/env python3\nimport os,json,sys\nfrom pathlib import Path\nwith (Path(os.environ["FIXTURE"])/"ssh-calls").open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                else:text='#!/bin/bash\nexit 0\n'
                script.write_text(text);script.chmod(0o700)
            for file in ['golden.qcow2','firmware.fd','vars.fd']:(root/file).touch()
            env={'PATH':str(bin)+':'+os.environ['PATH'],'HOME':str(root),'FIXTURE':str(root),
                 'TARTCI_GUEST_DRIVER':str(driver),'TARTCI_VM_LEASES':'0',
                 'TART_HOME':str(root/'vms'),'TARTCI_VM_DISK_FREE_FLOOR_GB':'1','TARTCI_STATE_DIR':str(root/'state'),'TARTCI_MACOS_GOLDEN':'fixture-golden',
                 'TARTCI_GH_CLI':str(bin/'fake-gh'),'TARTCI_JIT_GH_CLI':str(bin/'fake-gh'),
                 'TARTCI_WIN_WORK':str(root/'jobs'),'TARTCI_WIN_LOGS':str(root/'logs'),
                 'TARTCI_WIN_GOLDEN':str(root/'golden.qcow2'),'TARTCI_WIN_FIRMWARE':str(root/'firmware.fd'),
                 'TARTCI_WIN_VARS_TEMPLATE':str(root/'vars.fd'),'TARTCI_RUNNER_NAME_PREFIX':'tc-fixture',
                 'TARTCI_MACOS_VM_CORES':'4','TARTCI_MACOS_VM_MEM_MB':'8192','TARTCI_WIN_CPUS':'2',
                 'TARTCI_WIN_PROXY_COMMAND':'/usr/bin/true','TARTCI_RUNNER_IDLE_TIMEOUT_SECS':'4','TARTCI_TEARDOWN_STEP_TIMEOUT_SECS':'1','TARTCI_JOB_TIMEOUT_SECS':'10'}
            if provider=='tart-macos':env['TARTCI_RUNNER_VERSION']='2.337.0'
            if dhcp_verifying:
                breaker=root/'dhcp';breaker.mkdir()
                breaker_before={'state':'verifying','streak':[],'boot_time':1,
                                'probe_lane':'native-lane','probe_started_at':time.time()}
                (breaker/'breaker.json').write_text(json.dumps(breaker_before))
                env.update(TARTCI_VM_DHCP_DIR=str(breaker),TARTCI_VM_DHCP_BOOT_TIME='1')
            if pool_drain:env.update(DRAIN_DURING_PREFLIGHT='1',TARTCI_POOL_STATE_FILE=str(root/'pool-state'))
            if admission:
                shipyard=bin/'fake-shipyard'
                shipyard.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
def arg(name):return args[args.index(name)+1]
verdict=os.environ['ADMISSION_VERDICT']
(Path(os.environ['FIXTURE'])/'admission-seen').touch()
print(json.dumps({'schema_version':1,'command':'runner:admission-clean',
 'verdict':verdict,'reason':{'admit':'clean','defer':'stale_compatible_runs','error':'mutation_failed'}[verdict],
 'repo':arg('--repo'),'base':arg('--base'),'labels':sorted(set(arg('--labels').lower().split(','))),
 'observed_at':'2026-10-03T08:00:00Z','blocker_run_ids':[] if verdict=='admit' else [42]}))
raise SystemExit({'admit':0,'defer':3,'error':1}[verdict])
''');shipyard.chmod(0o700)
                env.update(TARTCI_ADMISSION_CLEAN_MODE='required',TARTCI_SHIPYARD_CLI=str(shipyard),ADMISSION_VERDICT=admission)
            if lease_budget:
                env.update(TARTCI_VM_LEASES='1',TARTCI_LEASE_DIR=str(root/'leases'),TARTCI_LEASE_CAPACITY_CORES='10',TARTCI_LEASE_CAPACITY_MEM_MB='24576',TARTCI_GATE_RESERVED_CORES='6',TARTCI_GATE_RESERVED_MEM_MB='12288',TARTCI_NON_GATE_CAPACITY_CORES='4',TARTCI_MACOS_VM_MEM_MB='12288')
            if delayed_delete:env.update(DELAY_DELETE='1',TARTCI_PENDING_DELETE_RETRY_SECS='1',TARTCI_PENDING_DELETE_MAX_ATTEMPTS='3')
            if fail:env['FAIL_JOB']='1'
            if drain:env['DRAIN_JOB']='1'
            command=['/bin/bash',str(ROOT/'providers'/provider/'runner.sh'),'--once',* ([] if github else ['--local-job','fixture'])]
            if drain:
                process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline and not (root/'assigned').exists() and process.poll() is None:time.sleep(.05)
                self.assertTrue((root/'assigned').exists(),'listener never assigned fixture job')
                process.send_signal(signal.SIGTERM)
                stdout,stderr=process.communicate(timeout=15)
                result=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
                self.assertTrue((root/'finished').exists(),'signal interrupted assigned job')
            else:
                result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=45 if lease_budget else 25)
            calls=[json.loads(s) for s in (root/'driver-calls').read_text().splitlines()] if (root/'driver-calls').exists() else []
            admission_refused=github and admission in ('defer','error')
            jit_refused=admission_refused or (github and pool_drain)
            expected=({'defer':3,'error':1}[admission] if admission_refused else (75 if pool_drain else (9 if fail else 0)))
            self.assertEqual(result.returncode,expected,(result.stdout+result.stderr)[-6000:])
            if jit_refused:
                if admission_refused:self.assertTrue((root/'admission-seen').exists())
                if pool_drain:self.assertIn('preflight',[c[0] for c in calls])
                self.assertNotIn('run-jit',[c[0] for c in calls])
                gh_calls=(root/'gh-calls').read_text() if (root/'gh-calls').exists() else ''
                self.assertNotIn('generate-jitconfig',gh_calls)
            else:
                self.assertIn('preflight',[c[0] for c in calls])
                self.assertIn('run-jit' if github else 'run-local',[c[0] for c in calls])
                if admission and github:self.assertTrue((root/'admission-seen').exists())
                if admission and not github:self.assertFalse((root/'admission-seen').exists())
            self.assertNotIn('unexpected-gh',result.stderr)
            if delayed_delete:self.assertIn('pending-delete VM',result.stdout+result.stderr)
            if lease_budget:
                self.assertIn('cores=4 mem_mb=12288',result.stdout+result.stderr)
                self.assertEqual(json.loads((root/'leases/leases.json').read_text()),[])
            self.assertFalse((root/'present').exists())
            self.assertFalse(list((root/'jobs').glob('tc-fixture-*')))
            if provider=='tart-macos':
                tart_calls=[json.loads(s) for s in (root/'tart-calls').read_text().splitlines()]
                boot=next(c for c in tart_calls if c[0]=='run')
                self.assertIn('--net-softnet',boot)
                self.assertFalse(any(c.startswith('--dir') for c in boot))
            if dhcp_verifying:
                self.assertEqual(json.loads((breaker/'breaker.json').read_text()),breaker_before)
                self.assertFalse((root/'ssh-calls').exists())

    def test_macos_success_and_failure_cleanup(self):
        for fail in (False,True):
            with self.subTest(fail=fail):self.execute('tart-macos',fail)

    def test_macos_explicit_governor_budget_and_release(self):
        self.execute('tart-macos',lease_budget=True)

    def test_macos_local_pending_delete_reconciles_before_success(self):
        self.execute('tart-macos',delayed_delete=True)

    def test_windows_success_and_failure_cleanup(self):
        for fail in (False,True):
            with self.subTest(fail=fail):self.execute('qemu-windows',fail)

    def test_macos_github_jit_fixture_and_cleanup(self):
        self.execute('tart-macos',github=True)

    def test_prepared_local_guest_ignores_native_dhcp_probe_and_direct_ssh(self):
        self.execute('tart-macos',dhcp_verifying=True)

    def test_prepared_jit_guest_ignores_native_dhcp_probe_and_direct_ssh(self):
        self.execute('tart-macos',github=True,dhcp_verifying=True)

    def test_windows_github_jit_fixture_and_cleanup(self):
        self.execute('qemu-windows',github=True)

    def test_windows_required_admission_precedes_prepared_jit(self):
        self.execute('qemu-windows',github=True,admission='admit')

    def test_windows_admission_refusal_never_mints_jit_and_disposes_vm(self):
        for verdict in ('defer','error'):
            with self.subTest(verdict=verdict):self.execute('qemu-windows',github=True,admission=verdict)

    def test_windows_credential_free_local_job_skips_github_admission(self):
        self.execute('qemu-windows',admission='defer')

    def test_windows_drain_during_preflight_prevents_jit_and_disposes_vm(self):
        self.execute('qemu-windows',github=True,pool_drain=True)

    def test_windows_provider_drains_assigned_job_before_cleanup(self):
        self.execute('qemu-windows',github=True,drain=True)

    def test_missing_driver_cannot_enable_local_mode(self):
        for provider in ('tart-macos','qemu-windows'):
            with tempfile.TemporaryDirectory() as directory:
                env={'PATH':os.environ['PATH'],'HOME':directory}
                r=subprocess.run(['/bin/bash',str(ROOT/'providers'/provider/'runner.sh'),'--once','--local-job','fixture'],env=env,capture_output=True,text=True,timeout=5)
                self.assertNotEqual(r.returncode,0)
                self.assertIn('requires',r.stderr)

class GuestListenerTests(unittest.TestCase):
    def test_stdin_reaches_child_without_argv_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt=Path(directory)/'receipt.json'
            secret=b'fixture-credential'
            code="import sys;value=sys.stdin.buffer.read();assert value==b'fixture-credential';print('Running job: fixture');print('Job fixture completed with result: Succeeded')"
            r=subprocess.run([sys.executable,str(ROOT/'scripts/guest_driver_listener.py'),'--idle-timeout','2','--job-timeout','3','--receipt',str(receipt),'--',sys.executable,'-c',code],input=secret,capture_output=True,timeout=8)
            self.assertEqual(r.returncode,0,r.stderr)
            value=json.loads(receipt.read_text());self.assertTrue(value['assigned'] and value['terminal'])
            self.assertNotIn(secret,r.stdout)

    def test_assigned_job_drains_after_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt=Path(directory)/'receipt.json'
            code="import time;print('Running job: fixture',flush=True);time.sleep(2);print('Job fixture completed with result: Succeeded',flush=True)"
            process=subprocess.Popen([sys.executable,str(ROOT/'scripts/guest_driver_listener.py'),'--idle-timeout','5','--job-timeout','6','--receipt',str(receipt),'--',sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if receipt.exists() and json.loads(receipt.read_text())['assigned']:break
                time.sleep(.05)
            else:self.fail('assignment was not observed')
            process.send_signal(signal.SIGTERM)
            stdout,stderr=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,stderr)
            self.assertIn(b'Succeeded',stdout)
            self.assertTrue(json.loads(receipt.read_text())['drain_requested'])

    def test_assigned_deadline_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt=Path(directory)/'receipt.json'
            code="import time;print('Running job: fixture',flush=True);time.sleep(30)"
            r=subprocess.run([sys.executable,str(ROOT/'scripts/guest_driver_listener.py'),'--idle-timeout','5','--job-timeout','1','--receipt',str(receipt),'--',sys.executable,'-c',code],capture_output=True,timeout=15)
            self.assertEqual(r.returncode,124,r.stderr)
            self.assertTrue(json.loads(receipt.read_text())['assigned'])

    def test_idle_deadline_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt=Path(directory)/'receipt.json'
            r=subprocess.run([sys.executable,str(ROOT/'scripts/guest_driver_listener.py'),'--idle-timeout','1','--job-timeout','5','--receipt',str(receipt),'--',sys.executable,'-c','import time;time.sleep(30)'],capture_output=True,timeout=15)
            self.assertEqual(r.returncode,124,r.stderr)
            self.assertTrue(json.loads(receipt.read_text())['terminal'])

if __name__=='__main__':unittest.main()
