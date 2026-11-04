"""Compact H800 deployments keep complete model weights and network endpoints."""
import json
import unittest

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import validate_profile
from joint_scheduling.backend.moe_profile import build_moe_profile
from joint_scheduling.config import load_config


class ScaledDeploymentTests(unittest.TestCase):
    def test_compact_configs_cover_all_gpus_and_preserve_compute_and_link_rates(self):
        main=load_config(PACKAGE/'configs/research/base_160gpu.json')
        reference=ComputeProfile(main.profile_path())
        for total,p_size,d_size in ((32,16,16),(48,16,32)):
            with self.subTest(gpus=total):
                cfg=load_config(PACKAGE/f'configs/research/base_{total}gpu.json')
                profile=ComputeProfile(cfg.profile_path());validate_profile(cfg,profile)
                self.assertEqual(cfg.topology.num_gpus,total)
                self.assertEqual([len(g) for g in cfg.expert_parallel.groups],[p_size,d_size])
                self.assertEqual(sorted(g for p in cfg.topology.placements for g in p.gpu_ids),list(range(total)))
                self.assertTrue(all(len(p.gpu_ids)==1 for p in cfg.topology.placements))
                self.assertEqual(cfg.topology.endpoint_bandwidth_gbps,400)
                self.assertEqual(cfg.topology.num_spines*cfg.topology.uplink_bandwidth_gbps,
                                 cfg.topology.nodes_per_leaf*cfg.topology.nic_bandwidth_gbps)
                self.assertEqual(profile.raw['phases'],reference.raw['phases'])
                self.assertEqual(profile.kv_bytes_per_token,70272)
                self.assertEqual(profile.expert.raw,reference.expert.raw)
                available=cfg.topology.gpu_memory_gib*2**30*(1-cfg.reservations.capacity_safety_fraction)
                for phase,size in (('prefill',p_size),('decode',d_size)):
                    self.assertEqual(profile.provenance['expert_parallel_sizes'][phase],size)
                    self.assertLess(profile.weights_for_phase(phase),available)
                    self.assertGreater(profile.weights_for_phase(phase),reference.weights_for_phase('decode'))

    def test_canonical_research_entry_points_use_32gpu_one_to_one(self):
        from joint_scheduling.experiments import plan_suite
        root=PACKAGE/'configs/research'
        current=load_config(root/'base.json');profile=ComputeProfile(current.profile_path())
        self.assertEqual(current.topology.num_gpus,32)
        self.assertEqual([len(g) for g in current.expert_parallel.groups],[16,16])
        self.assertEqual(profile.provenance['expert_parallel_sizes'],{'prefill':16,'decode':16})
        for name in ('kv_policy','network_priority','validation','contention_diagnostic'):
            spec=root/(name+'.json');raw=json.loads(spec.read_text())
            self.assertEqual(raw['base_config'],'base.json')
            self.assertEqual((root/raw['profile']).resolve(),current.profile_path().resolve())
            plan=plan_suite(spec)
            self.assertEqual(plan['base_config']['topology']['num_leaves'],2)
        reference=load_config(root/'base_160gpu.json')
        self.assertEqual(reference.topology.num_gpus,160)
        for spec in root.glob('kv_step1_160gpu*.json'):
            self.assertEqual(json.loads(spec.read_text())['base_config'],'base_160gpu.json')

    def test_tp1_ep8_does_not_fit_the_current_h800_capacity_model(self):
        anchor_path=PACKAGE/'profiles/deepseek_v3_h800_tp1_ep32_ep128_estimated.audit.json'
        anchors=json.loads(anchor_path.read_text())['source_anchors']
        raw,_=build_moe_profile(PACKAGE.parent/'third_party/vidur/data/hf_configs/deepseek_v3_config.json',
                                anchors,tp_size=1,ep_size=8)
        self.assertGreater(raw['weights_bytes_per_gpu'],80*2**30)
        compact=ComputeProfile(PACKAGE/'profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json')
        self.assertLess(compact.weights_for_phase('prefill'),76*2**30)


if __name__=='__main__':unittest.main()
