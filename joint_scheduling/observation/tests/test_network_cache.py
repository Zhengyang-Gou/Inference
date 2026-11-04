"""Idle telemetry reuse preserves rates, peaks and independent snapshots."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from joint_scheduling.observation.network import NetworkObserver


class MutableBackend:
    def __init__(self):
        self.now_ns = 0
        self.port = dict(resource_id='0:1', node_id=0, peer_id=1, capacity_bps=1e9,
                         tx_bytes=0, kv_bytes=0, collective_bytes=0, other_bytes=0,
                         queue_bytes=0, pfc_pause_count=0)

    def now_seconds(self):
        return self.now_ns / 1e9

    def network_snapshot(self):
        # Reuse the backend's own dictionary to catch accidental aliasing.
        return dict(time_ns=self.now_ns, resources=[self.port], flows=[])


class NetworkCacheTests(unittest.TestCase):
    def observer(self, root, alpha=.5, sink=None):
        self.backend = MutableBackend()
        return NetworkObserver(self.backend, SimpleNamespace(num_nodes=0, intra_node_bandwidth_gbps=100),
                               SimpleNamespace(network_ewma_alpha=alpha, observation_delay_us=0),
                               root, False, sample_sink=sink)

    def test_unchanged_counters_still_decay_nonzero_ewma(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = self.observer(Path(directory))
            try:
                self.backend.now_ns = 1_000_000
                self.backend.port.update(tx_bytes=100, kv_bytes=60, collective_bytes=40,
                                         queue_bytes=16, pfc_pause_count=2)
                first = observer.sample()['resources']['0:1']
                self.assertEqual(first['tx_bytes_per_second'], 50000)
                self.assertEqual(first['kv_bytes_per_second'], 30000)
                self.assertEqual(first['collective_bytes_per_second'], 20000)
                self.assertEqual(first['pfc_pause_delta'], 2)
                self.backend.now_ns = 2_000_000
                second = observer.sample()['resources']['0:1']
                self.assertEqual(second['tx_bytes_per_second'], 25000)
                self.assertEqual(second['pfc_pause_delta'], 0)
                self.assertEqual(observer.peaks['0:1']['queue_bytes'], 16)
                self.assertEqual(observer.peaks['0:1']['utilization'], first['utilization'])
            finally:
                observer.close()

    def test_idle_snapshots_and_observation_hooks_remain_independent(self):
        def sink(state):
            state['resources'].clear()
        with tempfile.TemporaryDirectory() as directory:
            observer = self.observer(Path(directory), alpha=1, sink=sink)
            try:
                old = observer.initial
                self.backend.now_ns = 1_000_000
                current = observer.sample()
                old['resources']['0:1']['tx_bytes'] = 99
                self.assertEqual(current['resources']['0:1']['tx_bytes'], 0)
                visible = observer.latest_observed()
                visible['resources'].clear()
                self.assertTrue(observer.latest_observed()['resources'])
                self.backend.port.update(tx_bytes=100, capacity_bps=2e9, queue_bytes=8)
                self.backend.now_ns = 2_000_000
                changed = observer.sample()['resources']['0:1']
                self.assertEqual(changed['tx_bytes_per_second'], 100000)
                self.assertEqual(changed['capacity_bps'], 2e9)
                self.assertEqual(changed['queue_bytes'], 8)
            finally:
                observer.close()

    def test_counter_rollback_after_idle_samples_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = self.observer(Path(directory), alpha=1)
            try:
                self.backend.port['tx_bytes'] = 100
                self.backend.now_ns = 1_000_000
                observer.sample()
                self.backend.now_ns = 2_000_000
                observer.sample()
                self.backend.now_ns = 3_000_000
                observer.sample()
                self.backend.port['tx_bytes'] = 99
                self.backend.now_ns = 4_000_000
                with self.assertRaisesRegex(ValueError, 'moved backwards'):
                    observer.sample()
            finally:
                observer.close()


if __name__ == '__main__':
    unittest.main()
