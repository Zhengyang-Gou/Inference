import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from joint_scheduling.observation.network import NetworkObserver


class ClockBackend:
    def __init__(self):self.now_ns=0
    def now_seconds(self):return self.now_ns/1e9
    def network_snapshot(self):
        return dict(time_ns=self.now_ns,flows=[],resources=[dict(resource_id='0:1',node_id=0,peer_id=1,
            capacity_bps=1000000000,tx_bytes=self.now_ns,kv_bytes=self.now_ns,collective_bytes=0,
            other_bytes=0,queue_bytes=0,pfc_pause_count=0)])


class NetworkRetentionTests(unittest.TestCase):
    def test_idle_sampling_retains_only_the_visibility_delay_window(self):
        for delay in (0,20,350):
            with self.subTest(delay_us=delay),tempfile.TemporaryDirectory() as directory:
                backend=ClockBackend()
                observer=NetworkObserver(backend,SimpleNamespace(num_nodes=2,intra_node_bandwidth_gbps=100),
                    SimpleNamespace(network_ewma_alpha=.5,observation_delay_us=delay),Path(directory),False)
                try:
                    for index in range(1,5001):
                        backend.now_ns=index*100000
                        observer.sample()
                        self.assertLessEqual(len(observer.pending),math.ceil(delay/100))
                    visible=observer.latest_observed()
                    self.assertLessEqual(visible['visible_at_seconds'],backend.now_seconds()+1e-12)
                    expected=max(0,5000-math.ceil(delay/100))
                    self.assertEqual(visible['sequence'],expected)
                finally:observer.close()

    def test_reads_between_samples_observe_exact_delayed_snapshot_and_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=ClockBackend()
            observer=NetworkObserver(backend,SimpleNamespace(num_nodes=0,intra_node_bandwidth_gbps=100),
                SimpleNamespace(network_ewma_alpha=.5,observation_delay_us=20),Path(directory),False)
            try:
                self.assertIsNone(observer.latest_observed())
                backend.now_ns=20000
                self.assertEqual(observer.latest_observed()['sequence'],0)
                backend.now_ns=100000
                sampled=observer.sample()
                self.assertEqual(observer.latest_observed()['sequence'],0)
                backend.now_ns=119999
                self.assertEqual(observer.latest_observed()['sequence'],0)
                backend.now_ns=120000
                result=observer.latest_observed()
                self.assertEqual(result,sampled)
                result['resources']['0:1']['kv_bytes']=0
                self.assertEqual(observer.latest_observed()['resources']['0:1']['kv_bytes'],100000)
            finally:observer.close()


if __name__=='__main__':unittest.main()
