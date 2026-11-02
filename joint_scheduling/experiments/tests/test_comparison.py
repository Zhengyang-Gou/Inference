"""Matched policy cases reject hidden scenario differences before execution."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.experiments import plan_suite,run_suite


ALLOWED=['runtime.kv_transfer_mode','kv_scheduler.policy','kv_scheduler.slo_priority_enabled',
         'kv_scheduler.collective_protection_enabled']


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        cfg=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        self.base=json.loads(cfg.config_path.read_text())
        self.base['feedback']['compute_profile_path']='profile.json'
        self.base['runtime']['workload_path']='workload.json'
        self.base['runtime']['network_priority_enabled']=False
        (self.root/'base.json').write_text(json.dumps(self.base))
        (self.root/'profile.json').write_bytes(cfg.profile_path().read_bytes())
        wp=cfg.config_path.parent/cfg.runtime.workload_path
        (self.root/'workload.json').write_bytes(wp.read_bytes())
        identity=lambda n:hashlib.sha256((self.root/n).read_bytes()).hexdigest()
        self.spec=dict(schema_version=1,base_config='base.json',profile='profile.json',workload='workload.json',seeds=[7],
            cases=[dict(id='immediate',overrides={'runtime.kv_transfer_mode':'layer_ready_immediate',
                'kv_scheduler.policy':'slo_slack','kv_scheduler.slo_priority_enabled':False,'kv_scheduler.collective_protection_enabled':False}),
                dict(id='fifo',overrides={'runtime.kv_transfer_mode':'layer_ready_fifo','kv_scheduler.policy':'slo_slack',
                    'kv_scheduler.slo_priority_enabled':False,'kv_scheduler.collective_protection_enabled':False}),dict(id='mlu',overrides={})],
            comparison=dict(purpose='validation',allowed_changes=ALLOWED.copy(),
                fixed={'prefix_cache.enabled':False,'binding.joint_selection_enabled':False,'runtime.network_priority_enabled':False},
                input_sha256=dict(profile=identity('profile.json'),workload=identity('workload.json'))))
        self.path=self.root/'suite.json'

    def save(self):
        self.path.write_text(json.dumps(self.spec));return self.path

    def test_named_cases_audit_all_actual_differences_and_pin_inputs(self):
        plan=plan_suite(self.save());audit=plan['comparison_audit']
        self.assertEqual([r['run_id'] for r in plan['runs']],['immediate-seed-7','fifo-seed-7','mlu-seed-7'])
        self.assertTrue(audit['passed']);self.assertEqual(len(audit['pairwise_differences']),3)
        for pair in audit['pairwise_differences']:
            self.assertLessEqual(set(pair['differences']),set(ALLOWED))
        before=self.path.read_bytes();plan_suite(self.path);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(audit['model']['ep_size_unit'],'TP_teams')
        self.assertEqual(audit['model']['status'],'synthetic_profile')

    def test_hidden_bandwidth_or_batch_changes_rejected_before_output(self):
        original=copy.deepcopy(self.spec)
        for key,value in [('topology.nic_bandwidth_gbps',1),('runtime.decode_batch_size',1)]:
            self.spec=copy.deepcopy(original);self.spec['cases'][0]['overrides'][key]=value
            output=self.root/'unexpected'
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'forbids'):
                run_suite(self.save(),output)
            self.assertFalse(output.exists())

    def test_fixed_features_input_identity_and_invalid_policy_fail_preflight(self):
        self.base['prefix_cache']['enabled']=True
        (self.root/'base.json').write_text(json.dumps(self.base))
        with self.assertRaisesRegex(ValueError,'fixed setting'):plan_suite(self.save())
        self.base['prefix_cache']['enabled']=False;(self.root/'base.json').write_text(json.dumps(self.base))
        self.spec['cases'][0]['overrides']={'runtime.kv_transfer_mode':'layer_ready_immediate'}
        with self.assertRaisesRegex(ValueError,'slo_mlu'):plan_suite(self.save())
        self.spec['cases']=[dict(id='mlu',overrides={})]
        (self.root/'workload.json').write_bytes((self.root/'workload.json').read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'SHA-256 mismatch'):plan_suite(self.save())

    def test_case_names_and_cartesian_mix_are_rejected(self):
        original=copy.deepcopy(self.spec)
        for cases in ([],[dict(id='mlu',overrides={})]*2,[dict(id='../bad',overrides={})]):
            self.spec=copy.deepcopy(original);self.spec['cases']=cases
            with self.subTest(cases=cases),self.assertRaises(ValueError):plan_suite(self.save())
        self.spec=original;self.spec['sweep']={}
        with self.assertRaisesRegex(ValueError,'not both'):plan_suite(self.save())

    def test_priority_ablation_changes_only_native_priority(self):
        self.spec['cases']=[dict(id='off',overrides={}),dict(id='on',overrides={'runtime.network_priority_enabled':True})]
        c=self.spec['comparison'];c['purpose']='network_priority';c['allowed_changes']=['runtime.network_priority_enabled']
        c['fixed'].pop('runtime.network_priority_enabled')
        plan=plan_suite(self.save());pair=plan['comparison_audit']['pairwise_differences'][0]
        self.assertEqual(set(pair['differences']),{'runtime.network_priority_enabled'})

    def test_input_change_after_planning_rejected_before_output(self):
        path=self.save();plan=plan_suite(path)
        (self.root/'profile.json').write_bytes((self.root/'profile.json').read_bytes()+b'\n')
        output=self.root/'unexpected'
        with patch('joint_scheduling.experiments.runner.plan_suite',return_value=plan):
            with self.assertRaisesRegex(ValueError,'changed before input freeze'):
                run_suite(path,output)
        self.assertFalse(output.exists())

    def test_common_window_is_shared_and_cannot_bypass_fixed_features(self):
        self.spec['common_overrides']={'evaluation.window_end_seconds':.5}
        plan=plan_suite(self.save())
        self.assertEqual(plan['comparison_audit']['fixed_conditions']['window_end_seconds'],.5)
        self.spec['common_overrides']['prefix_cache.enabled']=True
        with self.assertRaisesRegex(ValueError,'fixed setting'):plan_suite(self.save())

    @unittest.skipUnless(backend_available(),'native backend not built')
    def test_three_native_policy_cases_share_workload_profile_and_scope(self):
        report=run_suite(self.save(),self.root/'runs')
        self.assertEqual(report['successful_runs'],3)
        rows=[json.loads(line) for line in (self.root/'runs/runs.jsonl').read_text().splitlines()]
        self.assertEqual(len({r['workload_sha256'] for r in rows}),1)
        self.assertEqual(len({r['profile_sha256'] for r in rows}),1)
        self.assertTrue((self.root/'runs/comparison_audit.json').is_file())
        self.assertEqual([r['effective_baseline']['kv_priority'] for r in rows],['immediate','fifo','mlu_multilevel'])
        self.assertTrue(all(not r['effective_baseline']['network_priority_enabled'] for r in rows))
