"""Physical NIC endpoint caps and concurrent local GPU-port transfers."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.client import Ns3Backend
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.binding.predictor import topology_resources
from joint_scheduling.config import load_config
from joint_scheduling.feedback import NetworkTransfer
from joint_scheduling.kv_scheduler.adaptive import NetworkResources
from joint_scheduling.topology import build_manifest, export_backend_topology


class GPUNetworkTopologyTests(unittest.TestCase):
    def test_research_bandwidths_ports_and_unchanged_parallel_layout(self):
        config = load_config(PACKAGE/'configs/research/base.json')
        topo = config.topology
        self.assertEqual((topo.num_nodes, topo.num_gpus), (20, 160))
        self.assertEqual((topo.nic_model, topo.endpoint_bandwidth_gbps), ('per_gpu', 400))
        self.assertEqual(topo.nic_bandwidth_gbps, 3200)
        self.assertEqual(topo.num_spines*topo.uplink_bandwidth_gbps, topo.nodes_per_leaf*topo.nic_bandwidth_gbps)
        self.assertEqual((topo.intra_node_model, topo.intra_node_bandwidth_gbps), ('gpu_ports', 1280))
        self.assertTrue(all(len(p.gpu_ids)==1 for p in topo.placements))
        self.assertEqual([len(g) for g in config.expert_parallel.groups], [32,128])
        graph = build_manifest(topo)
        self.assertEqual(len([l for l in graph['links'] if l['src'].startswith('gpu-')]), 160)
        with tempfile.TemporaryDirectory() as folder:
            path = export_backend_topology(topo, Path(folder)/'network.topo')
            self.assertEqual(path.read_text().splitlines()[0], '174 1 0 14 200 NONE')
        resources = NetworkResources(topo, topology_resources(topo))
        self.assertEqual(resources.for_gpus(0, 1), ('local:gpu-0:tx', 'local:gpu-1:rx'))
        a, b = set(resources.for_gpus(0, 8)), set(resources.for_gpus(1, 9))
        self.assertTrue(a and b)
        self.assertFalse(a & b)  # Separate GPU NICs, same-leaf traffic.
        self.assertTrue(set(resources.for_gpus(0, 24)) & set(resources.for_gpus(1, 25)))
        self.assertEqual(min(resources.capacities[k] for k in a), 50e9)


@unittest.skipUnless(backend_available(), 'build the current native adapter first')
class GPUNetworkNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = load_config(PACKAGE/'configs/ep_smoke.json').topology
        self.topology = replace(base, gpus_per_node=4, nic_model='per_gpu', nic_bandwidth_gbps=8,
            intra_node_model='gpu_ports', intra_node_bandwidth_gbps=1, intra_node_latency_us=0)

    def send(self, backend, name, source, destination, times, size=1000000):
        backend.submit(NetworkTransfer(name, 'collective', source, destination, size, 1),
                       lambda progress: times.update({name: backend.now_seconds()}))

    def test_local_disjoint_pairs_and_opposite_directions_run_concurrently(self):
        with Ns3Backend(self.topology, self.root/'network') as backend:
            times = {}
            for name, src, dst in [('a',0,1), ('b',2,3), ('c',1,0)]:
                self.send(backend, name, src, dst, times)
            backend.run_until(.02)
            self.assertEqual(len(times), 3)
            for value in times.values():
                self.assertAlmostEqual(value, .008, places=12)
            snapshot = backend.network_snapshot()
            self.assertEqual(snapshot['diagnostics']['retained_local_jobs'], 0)
            ports = {r['resource_id']:r for r in snapshot['resources']}
            self.assertEqual(ports['local:gpu-0:tx']['tx_bytes'], 1000000)
            self.assertEqual(ports['local:gpu-0:rx']['tx_bytes'], 1000000)

    def test_same_local_source_and_same_destination_share_their_ports(self):
        with Ns3Backend(self.topology, self.root/'network') as backend:
            times = {}
            for name, src, dst in [('a',0,1), ('b',0,2), ('c',3,2)]:
                self.send(backend, name, src, dst, times)
            backend.run_until(.03)
            self.assertAlmostEqual(times['a'], .008, places=12)
            self.assertAlmostEqual(times['b'], .016, places=12)
            self.assertAlmostEqual(times['c'], .024, places=12)

    def test_gpu_nics_limit_each_gpu_and_allow_node_aggregate_bandwidth(self):
        size = 1024*1024
        with Ns3Backend(self.topology, self.root/'network', congestion_control='none') as backend:
            times = {}
            self.send(backend, 'solo', 0, 4, times, size)
            backend.run_until(.02)
            solo = times['solo']
            self.assertGreaterEqual(solo, size*8/(2e9))
            for gpu in range(4):
                self.send(backend, f'parallel-{gpu}', gpu, gpu+4, times, size)
            backend.run_until(.04)
            parallel = max(times[f'parallel-{g}']-.02 for g in range(4))
            self.assertLess(parallel, solo*1.25)
            self.send(backend, 'incast-a', 0, 4, times, size)
            self.send(backend, 'incast-b', 1, 4, times, size)
            backend.run_until(.08)
            self.assertGreater(max(times['incast-a'], times['incast-b'])-.04, solo*1.8)
            ports = backend.network_snapshot()['resources']
            self.assertTrue(all(r['capacity_bps']==2e9 for r in ports if r['node_id']<16 and not r['resource_id'].startswith('local:')))


if __name__ == '__main__':
    unittest.main()
