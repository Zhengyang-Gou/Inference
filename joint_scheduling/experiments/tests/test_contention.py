"""One explicit replay specification distinguishes transport from request gains."""
import hashlib
import csv
import json
import tempfile
import unittest
from pathlib import Path
from joint_scheduling.config import load_config
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.experiments import plan_suite,run_suite


class ContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        cfg=load_config(PACKAGE/'configs/ep_mlu_smoke.json');raw=json.loads(cfg.config_path.read_text())
        raw['runtime'].update(kv_transfer_mode='layer_ready_immediate',network_priority_enabled=False,workload_path='workload.json')
        raw['kv_scheduler'].update(policy='slo_slack',slo_priority_enabled=False,collective_protection_enabled=False)
        raw['feedback']['compute_profile_path']='profile.json'
        raw['feedback']['network_observation_interval_us']=1000
        (self.root/'base.json').write_text(json.dumps(raw))
        (self.root/'profile.json').write_bytes(cfg.profile_path().read_bytes())
        (self.root/'workload.json').write_bytes((cfg.config_path.parent/cfg.runtime.workload_path).read_bytes())
        self.spec=dict(schema_version=1,experiment_type='frozen_network_replay',base_config='base.json',profile='profile.json',workload='workload.json',seeds=[7],cases=[dict(id='baseline',overrides={})],comparison=dict(purpose='validation',allowed_changes=['runtime.network_priority_enabled'],fixed={'binding.policy':'round_robin'},input_sha256={k:hashlib.sha256((self.root/(k+'.json')).read_bytes()).hexdigest() for k in ('profile','workload')}))
        self.path=self.root/'spec.json'

    def save(self):
        self.path.write_text(json.dumps(self.spec));return self.path

    def test_unsupported_replay_conditions_fail_without_output(self):
        self.spec['cases'][0]['overrides']={'runtime.network_priority_enabled':True}
        with self.assertRaisesRegex(ValueError,'native priority off'):run_suite(self.save(),self.root/'out')
        self.assertFalse((self.root/'out').exists())
        self.spec['experiment_type']='pretend_measurement'
        with self.assertRaisesRegex(ValueError,'experiment_type'):plan_suite(self.save())

    @unittest.skipUnless(backend_available(),'native backend not built')
    def test_live_baseline_and_three_replays_have_separate_identities(self):
        result=run_suite(self.save(),self.root/'out');out=self.root/'out'
        self.assertEqual(result['status'],'completed');self.assertEqual(result['frozen_replays'],3)
        self.assertLessEqual(result['baseline_flow_replay_max_ack_error_ns'],1)
        live=json.loads((out/'run/reproducibility.json').read_text())
        self.assertEqual(live['experiment_type'],'end_to_end')
        self.assertEqual(result['profile_status'],live['profile_status'])
        for case in ('both','collective','kv'):
            replay=json.loads((out/('replay-'+case)/'reproducibility.json').read_text())
            self.assertEqual(replay['experiment_type'],'frozen_network_replay')
            self.assertEqual(replay['source']['source_tree_sha256'],live['source']['source_tree_sha256'])
        self.assertTrue((out/'report.md').exists());self.assertTrue((out/'replay_inputs.json').exists())

    @unittest.skipUnless(backend_available(),'native backend not built')
    def test_gpu_nic_endpoint_ids_survive_frozen_replay(self):
        raw=json.loads((self.root/'base.json').read_text())
        raw['topology'].update(nic_model='per_gpu',nic_bandwidth_gbps=800,
            uplink_bandwidth_gbps=800,intra_node_model='gpu_ports',intra_node_bandwidth_gbps=1280)
        (self.root/'base.json').write_text(json.dumps(raw))
        result=run_suite(self.save(),self.root/'out');out=self.root/'out'
        self.assertEqual(result['status'],'completed')
        self.assertLessEqual(result['baseline_flow_replay_max_ack_error_ns'],1)
        with (out/'flows.csv').open() as handle:
            flows=list(csv.DictReader(handle))
        self.assertTrue(any(int(row['source_endpoint'])>=4 for row in flows))
        self.assertTrue(all(int(row['source_endpoint'])//2 != int(row['destination_endpoint'])//2 for row in flows))
