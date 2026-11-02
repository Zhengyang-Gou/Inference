import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.backend.client import Ns3Backend
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.config import load_config


@unittest.skipUnless(backend_available(),'build the current native adapter first')
class RetainedStateTests(unittest.TestCase):
    def test_local_flow_survives_same_time_barrier_until_completion_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load_config(PACKAGE/'configs/baseline_smoke.json')
            topo=replace(config.topology,intra_node_bandwidth_gbps=1.0,intra_node_latency_us=0)
            with Ns3Backend(topo,Path(directory)) as backend:
                completed=[]
                backend.call_at(.000008,lambda:None)
                identifiers=backend.send_hosts(0,0,1000,lambda:completed.append(True))
                backend.advance(.000008)
                self.assertFalse(completed)
                before=backend.network_snapshot()
                self.assertIn(identifiers[0],[flow['id'] for flow in before['flows']])
                self.assertEqual(before['diagnostics']['retained_local_jobs'],1)
                backend.run_until(.000008)
                after=backend.network_snapshot()
                self.assertTrue(completed)
                self.assertNotIn(identifiers[0],[flow['id'] for flow in after['flows']])
                self.assertEqual(after['diagnostics']['retained_local_jobs'],0)

    def test_local_history_folds_completed_bytes_without_changing_fifo_times(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load_config(PACKAGE/'configs/baseline_smoke.json')
            topo=replace(config.topology,intra_node_bandwidth_gbps=1.0,intra_node_latency_us=0)
            with Ns3Backend(topo,Path(directory)) as backend:
                completions=[]
                for batch in range(100):
                    start=backend.now_seconds()
                    for index in range(8):
                        backend.send_hosts(0,0,1000,lambda:completions.append(backend.now_seconds()),
                                           'kv' if index%2==0 else 'collective')
                    backend.run_until(start+.0001)
                    state=backend.network_snapshot()
                    local=next(r for r in state['resources'] if r['resource_id']=='local:0')
                    self.assertEqual(local['tx_bytes'],(batch+1)*8000)
                    self.assertEqual(local['kv_bytes'],(batch+1)*4000)
                    self.assertEqual(local['collective_bytes'],(batch+1)*4000)
                    self.assertEqual(local['queue_bytes'],0)
                    self.assertEqual(state['diagnostics']['retained_local_jobs'],0)
                    self.assertEqual(state['diagnostics']['retired_local_jobs'],(batch+1)*8)
                    for index,value in enumerate(completions[-8:]):
                        self.assertAlmostEqual(value,start+(index+1)*.000008,places=12)
                self.assertEqual(len(completions),800)


if __name__=='__main__':unittest.main()
