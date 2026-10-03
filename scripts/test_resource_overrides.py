"""Admission receives explicit resource plans without changing host defaults."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
class ResourceOverridesTests(unittest.TestCase):
    def status(self,overrides=None,flags=()):
        with tempfile.TemporaryDirectory() as directory:
            env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1',
                 'TARTCI_LEASE_DIR':directory,'TARTCI_ROLE':'light',
                 'TARTCI_GOVERNOR_FILE':str(Path(directory)/'absent.toml'),
                 'TARTCI_HOST_CORES':'10','TARTCI_HOST_MEM_MB':'32768'}
            if overrides:env.update(overrides)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/leases.py'),'status','--json',*flags],env=env,capture_output=True,text=True,check=True)
            return json.loads(result.stdout)['capacity']
    def override(self):
        return {'TARTCI_LEASE_CAPACITY_CORES':'10','TARTCI_LEASE_CAPACITY_MEM_MB':'24576',
                'TARTCI_GATE_RESERVED_CORES':'6','TARTCI_GATE_RESERVED_MEM_MB':'12288'}
    def test_explicit_budget_and_production_reserve(self):
        cap=self.status(self.override())
        self.assertEqual((cap['total_cores'],cap['total_mem_mb'],cap['reserved_gate_cores'],cap['reserved_gate_mem_mb']),(10,24576,6,12288))
        self.assertEqual((cap['non_gate_limit_cores'],cap['non_gate_limit_mem_mb']),(4,12288))
    def test_cli_flags_win_over_environment(self):
        cap=self.status(self.override(),['--capacity','8','--capacity-mem-mb','16384','--reserved-gate-cores','4','--reserved-gate-mem-mb','8192'])
        self.assertEqual((cap['total_cores'],cap['total_mem_mb'],cap['reserved_gate_cores'],cap['reserved_gate_mem_mb']),(8,16384,4,8192))
    def test_absent_overrides_preserve_host_profile(self):
        cap=self.status()
        self.assertEqual((cap['total_cores'],cap['total_mem_mb'],cap['reserved_gate_cores'],cap['reserved_gate_mem_mb']),(6,22528,3,11264))
if __name__=='__main__':unittest.main()
