"""GPU-rank attention DP and unequal P/D expert groups share one runtime."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.expert_parallel import ExpertParallelConfig, validate_profile
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.topology import InstancePlacement


def small_config(root):
    base=load_config(PACKAGE/'configs/ep_smoke.json')
    raw=json.loads(base.profile_path().read_text())
    for phase in ('prefill','decode'):raw['phases'][phase]['tp_size']=1
    raw.update(weights_bytes_per_gpu=1000,weights_bytes_per_gpu_by_phase={'prefill':1000,'decode':100})
    raw['provenance']['expert_parallel_sizes']={'prefill':2,'decode':4}
    profile=root/'profile.json';profile.write_text(json.dumps(raw))
    placements=tuple(InstancePlacement(('p'+str(g)) if g<2 else ('d'+str(g-2)),
        'prefill' if g<2 else 'decode',(g,)) for g in range(6))
    return replace(base,
        topology=replace(base.topology,num_leaves=1,nodes_per_leaf=3,gpus_per_node=2,placements=placements,
            nic_model='per_gpu',nic_bandwidth_gbps=800,uplink_bandwidth_gbps=1200,
            intra_node_model='gpu_ports',intra_node_bandwidth_gbps=1280),
        feedback=replace(base.feedback,compute_profile_path=str(profile)),
        binding=replace(base.binding,policy='balanced_random',seed=7,decision_overhead_us=0),
        expert_parallel=ExpertParallelConfig(True,(('p0','p1'),('d0','d1','d2','d3')),'gpu'),
        runtime=replace(base.runtime,workload_path=str(PACKAGE/'backend/fixtures/ep_workload.json'),network_priority_enabled=False))


class AsymmetricInputTests(unittest.TestCase):
    def test_phase_specific_profile_rejects_wrong_decode_group_and_non_tp1_gpu_ranks(self):
        with tempfile.TemporaryDirectory() as folder:
            config=small_config(Path(folder));profile=ComputeProfile(config.profile_path())
            validate_profile(config,profile)
            profile.provenance['expert_parallel_sizes']['decode']=2
            with self.assertRaisesRegex(ValueError,'EP group size'):validate_profile(config,profile)
            main=load_config(PACKAGE/'configs/cluster_128gpu_tp4_ep8_8gpu_per_node.json')
            with self.assertRaisesRegex(ValueError,'TP1'):
                validate_profile(replace(main,expert_parallel=replace(main.expert_parallel,rank_unit='gpu')),profile)


@unittest.skipUnless(backend_available(),'build native backend first')
class AsymmetricNetworkTests(unittest.TestCase):
    def test_three_kv_modes_complete_with_role_weights_and_no_tp_or_dp_transfers(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=small_config(root);plans=[]
            for mode in ('layer_ready_immediate','layer_ready_fifo','layer_ready_slo'):
                config=replace(base,runtime=replace(base.runtime,kv_transfer_mode=mode))
                run=root/mode;result=run_baseline(config,run)
                self.assertTrue(all(r['status']=='completed' for r in result.requests))
                self.assertEqual(len(result.tokens),24)
                plans.append(json.loads((run/'binding_plan.json').read_text()))
                resources=json.loads((run/'final_resources.json').read_text())
                usable=int(config.topology.gpu_memory_gib*2**30*(1-config.reservations.capacity_safety_fraction))
                self.assertEqual(resources['gpu-0']['capacity_bytes'],usable-1000)
                self.assertEqual(resources['gpu-2']['capacity_bytes'],usable-100)
                self.assertTrue(all(r['occupied_bytes']==r['reserved_bytes']==0 for r in resources.values()))
                events=[json.loads(line) for line in (run/'events.jsonl').read_text().splitlines()]
                types={e.get('collective_type') for e in events if e['event']=='collective_started'}
                self.assertEqual(types,{'ep_dispatch','ep_combine'})
                sends=[e for e in events if e['event']=='transfer_started' and e['transfer_kind']=='kv']
                self.assertEqual(sum(e['size_bytes'] for e in sends),6*8*8192)
                ep=json.loads((run/'expert_parallel_summary.json').read_text())
                self.assertEqual(ep['phase_ep_sizes'],{'prefill':2,'decode':4})
                self.assertEqual(ep['started_calls'],ep['completed_calls'])
                self.assertFalse(ep['active_calls_at_end'])
            self.assertEqual(plans[0],plans[1]);self.assertEqual(plans[1],plans[2])


if __name__=='__main__':unittest.main()
