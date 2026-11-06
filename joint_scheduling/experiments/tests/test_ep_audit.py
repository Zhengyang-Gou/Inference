"""Independent EP payload oracle, corruption detection and native event audit."""
import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_parallel import ExpertParallelMixin
from joint_scheduling.backend.expert_profile import ExpertProfile
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.experiments.ep_audit import EPFlowAudit, run_ep_audit
from joint_scheduling.experiments.ep_reference import audit_layout, audit_routing_constraint, payload_totals, stage_flows, token_reference
from joint_scheduling.topology import InstancePlacement


class ReferenceTests(unittest.TestCase):
    def test_rank_collisions_odd_tp_shards_and_combine_reversal(self):
        raw = copy.deepcopy(ComputeProfile(PACKAGE/'backend/fixtures/ep_compute_profile.json').expert.raw)
        raw.update(hidden_size=5,top_k=3,routing_pattern=[[0,1,2],[1,2,3]])
        expert = ExpertProfile(raw, 2)
        placements = {'a':InstancePlacement('a','prefill',(0,1)), 'b':InstancePlacement('b','prefill',(2,3))}
        reference = token_reference(expert, ('a','b'), placements, 'a', 2, [(2,0)])
        self.assertEqual(reference['assignments'], [1,2,2,1])
        self.assertEqual(reference['assignment_channels'], {(0,2):9,(1,3):6})
        self.assertEqual(reference['rank_channels'], {(0,2):6,(1,3):4})
        self.assertEqual(stage_flows(reference,2,True), Counter({(2,0,18):1,(3,1,12):1}))
        totals = payload_totals(reference,raw,2)
        self.assertEqual(totals['dispatch']['direct_bytes'],15)
        self.assertEqual(totals['dispatch']['distinct_rank_bytes'],10)
        self.assertEqual(totals['combine']['direct_inter_node_bytes'],30)
        self.assertEqual(totals['combine']['single_copy_per_remote_node_bytes'],20)

    def test_independent_batch_members_keep_token_identity_and_offsets(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        expert = ComputeProfile(config.profile_path()).expert
        places = {p.instance_id:p for p in config.topology.placements}
        group = config.expert_parallel.groups[0]
        a = token_reference(expert,group,places,'p0',2,[(3,1),(3,1)])
        b = token_reference(expert,group,places,'p0',2,[(3,1)])
        self.assertEqual(a['tokens'],6)
        self.assertEqual(a['assignments'],[2*n for n in b['assignments']])
        self.assertEqual(a['rank_channels'],Counter({k:2*v for k,v in b['rank_channels'].items()}))

    def test_reference_160gpu_routing_has_group_limited_prefill_node_fanout(self):
        config = load_config(PACKAGE/'configs/research/base_160gpu.json')
        report = audit_layout(config,ComputeProfile(config.profile_path()))
        self.assertEqual(report['status'],'passed')
        self.assertEqual(report['checked_source_cursor_cases'],160*4*len(report['layer_ids_checked']))
        phases = report['phase_period_totals']
        for phase in ('prefill','decode'):
            row = phases[phase]['dispatch']
            self.assertGreaterEqual(row['direct_bytes'],row['distinct_rank_bytes'])
            self.assertGreaterEqual(row['distinct_rank_inter_node_bytes'],row['single_copy_per_remote_node_bytes'])
        p,d = phases['prefill']['dispatch'],phases['decode']['dispatch']
        # Four physical P nodes, two gate groups per node. Choosing four of
        # eight groups touches a node with probability 1 - C(6,4)/C(8,4).
        self.assertEqual(p['direct_inter_node_bytes'],6*phases['prefill']['tokens']*7168)
        self.assertEqual(p['single_copy_per_remote_node_bytes']*14,33*phases['prefill']['tokens']*7168)
        self.assertEqual(d['direct_inter_node_bytes']*2,15*phases['decode']['tokens']*7168)

    def test_main_gate_matches_official_limit_and_legacy_route_is_detected(self):
        config = load_config(PACKAGE/'configs/research/base.json')
        expert = ComputeProfile(config.profile_path()).expert
        result = audit_routing_constraint(expert,8,4)
        self.assertEqual(result['status'],'matches_reference_constraint')
        self.assertEqual(result['violating_rows'],0)
        self.assertEqual(result['groups_touched_histogram'],{4:2240})
        raw = copy.deepcopy(expert.raw)
        raw.pop('n_group');raw.pop('topk_group')
        raw['routing_pattern'] = [[(i+j*32)%256 for j in range(8)] for i in range(256)]
        result = audit_routing_constraint(ExpertProfile(raw,61),8,4)
        self.assertEqual(result['status'],'differs_from_reference_constraint')
        self.assertEqual(result['violating_rows'],256)
        for groups,limit in ((None,4),(8,None),(0,0),(3,2),(8,9),(True,1)):
            with self.subTest(groups=groups,limit=limit),self.assertRaisesRegex(ValueError,'routing reference'):
                audit_routing_constraint(expert,groups,limit)

    def test_audit_detects_wrong_production_bytes_and_wrong_counts(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        profile = ComputeProfile(config.profile_path())
        original = ExpertParallelMixin.ep_flows
        def doubled(runtime,*args):
            return [(s,d,2*n) for s,d,n in original(runtime,*args)]
        with patch.object(ExpertParallelMixin,'ep_flows',doubled):
            with self.assertRaisesRegex(ValueError,'payload/endpoint'):
                audit_layout(config,profile)
        with patch.object(profile.expert,'counts',return_value=[0,0,0,0]):
            with self.assertRaisesRegex(ValueError,'routing count'):
                audit_layout(config,profile)

    def test_dense_and_cached_live_audits_fail_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError,'EP audit'):
                run_ep_audit(load_config(PACKAGE/'configs/baseline_smoke.json'),root/'dense')
            config = load_config(PACKAGE/'configs/ep_smoke.json')
            config = replace(config,prefix_cache=replace(config.prefix_cache,enabled=True))
            with self.assertRaisesRegex(ValueError,'prefix_cache'):
                run_ep_audit(config,root/'cache',True)
            self.assertFalse((root/'dense').exists())
            self.assertFalse((root/'cache').exists())

    def test_cli_reports_implementation_and_reference_alignment_separately(self):
        from joint_scheduling.cli import main
        with tempfile.TemporaryDirectory() as directory,redirect_stdout(io.StringIO()):
            output = Path(directory)/'audit'
            code = main(['--config',str(PACKAGE/'configs/research/base.json'),'audit-ep',
                '--output',str(output),'--expert-groups','8','--max-groups-per-token','4'])
            self.assertEqual(code,0)
            report = json.loads((output/'audit_summary.json').read_text())
            self.assertEqual(report['layout']['status'],'passed')
            self.assertEqual(report['routing_constraint']['status'],'matches_reference_constraint')
            self.assertIsNone(report['simulation'])

    def probe(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        profile = ComputeProfile(config.profile_path())
        probe = EPFlowAudit(config,profile)
        probe.event('compute_step_started',0,dict(instance_id='p0',phase='prefill',tokens=1,layer_id=0))
        probe.event('ep_call_started',0,dict(ep_call_id='ep-1',source_instance_id='p0',phase='prefill',
                                           layer_id=0,expert_assignments=profile.expert.counts(1)))
        return probe

    def test_probe_rejects_wrong_cursor_premature_compute_or_combine_and_missing_layers(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        probe = EPFlowAudit(config,ComputeProfile(config.profile_path()))
        probe.event('compute_step_started',0,dict(instance_id='p0',phase='prefill',tokens=1,layer_id=0))
        with self.assertRaisesRegex(ValueError,'cursor/assignment'):
            probe.event('ep_call_started',0,dict(ep_call_id='bad',source_instance_id='p0',phase='prefill',
                layer_id=0,expert_assignments=[0,0,0,0]))
        probe = self.probe()
        with self.assertRaisesRegex(ValueError,'combine before'):
            probe.event('collective_started',0,dict(ep_call_id='ep-1',collective_type='ep_combine'))
        probe = self.probe()
        call = probe.calls['ep-1']
        expected = stage_flows(call['reference'],probe.profile.expert.raw['dispatch_element_bytes'])
        probe.event('collective_started',0,dict(ep_call_id='ep-1',collective_type='ep_dispatch',
            size_bytes=sum(s*n for (_,_,s),n in expected.items()),flow_count=sum(expected.values())))
        with self.assertRaisesRegex(ValueError,'before dispatch ACK'):
            probe.event('compute_segment_started',0,dict(task_kind='expert',ep_call_id='ep-1',instance_id='p0',assignments=1))
        with self.assertRaisesRegex(ValueError,'before every expected flow ACK'):
            probe.event('collective_completed',0,dict(ep_call_id='ep-1',collective_type='ep_dispatch'))
        with self.assertRaisesRegex(ValueError,'before all declared MoE'):
            probe.event('compute_step_completed',0,dict(instance_id='p0'))

    def test_probe_rejects_changed_native_flow_and_false_ack_readiness(self):
        probe = self.probe()
        expected = stage_flows(probe.calls['ep-1']['reference'],probe.profile.expert.raw['dispatch_element_bytes'])
        probe.event('collective_started',0,dict(ep_call_id='ep-1',collective_type='ep_dispatch',
            size_bytes=sum(s*n for (_,_,s),n in expected.items()),flow_count=sum(expected.values())))
        src,dst,size = next(iter(expected))
        row = dict(ep_call_id='ep-1',collective_type='ep_dispatch',transfer_id='flow-1',
                   source_gpu_id=src,destination_gpu_id=dst,size_bytes=size)
        with self.assertRaisesRegex(ValueError,'payload or endpoints'):
            probe.event('transfer_started',0,dict(row,size_bytes=size+1))
        probe.event('transfer_started',0,row)
        with self.assertRaisesRegex(ValueError,'sender ACK readiness'):
            probe.event('transfer_completed',1,dict(ep_call_id='ep-1',collective_type='ep_dispatch',
                transfer_id='flow-1',source_reads_complete=False,destination_ready=True))

    def test_cutoff_excludes_unstarted_combine_but_requires_all_flow_submissions(self):
        for submit in (True,False):
            probe = self.probe()
            reference = probe.calls['ep-1']['reference']
            expected = stage_flows(reference,probe.profile.expert.raw['dispatch_element_bytes'])
            totals = payload_totals(reference,probe.profile.expert.raw,probe.config.topology.gpus_per_node)['dispatch']
            probe.event('collective_started',0,dict(ep_call_id='ep-1',collective_type='ep_dispatch',
                size_bytes=totals['direct_bytes'],flow_count=sum(expected.values())))
            if submit:
                for index,(src,dst,size) in enumerate(expected):
                    probe.event('transfer_started',0,dict(ep_call_id='ep-1',collective_type='ep_dispatch',
                        source_gpu_id=src,destination_gpu_id=dst,size_bytes=size,transfer_id=str(index)))
            summary = dict(started_calls=1,completed_calls=0,active_calls_at_end={'ep-1':{}},
                routed_assignments=sum(reference['assignments']),dispatch_bytes=totals['direct_bytes'],
                inter_node_dispatch_bytes=totals['direct_inter_node_bytes'],combine_bytes=0,inter_node_combine_bytes=0)
            if submit:
                report = probe.finish(summary)
                self.assertEqual(report['incomplete_calls'],1)
                self.assertEqual(report['active_logical_flows'],sum(expected.values()))
            else:
                with self.assertRaisesRegex(ValueError,'omitted a declared flow'):
                    probe.finish(summary)


@unittest.skipUnless(backend_available(),'native backend not built')
class NativeAuditTests(unittest.TestCase):
    def test_legacy_and_packed_steps_pass_live_routing_flow_and_dependency_audit(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            for packed in (False,True):
                cfg = replace(config,runtime=replace(config.runtime,
                    prefill_batch_token_budget=6 if packed else 0,prefill_batch_size=3 if packed else 1))
                report = run_ep_audit(cfg,Path(directory)/str(packed),True)['simulation']
                self.assertEqual(report['status'],'passed')
                self.assertEqual(report['started_calls'],report['completed_calls'])
                self.assertGreater(report['checked_logical_flows'],0)
                self.assertEqual(report['incomplete_calls']+report['active_logical_flows']+report['active_compute_steps'],0)

    def test_cutoff_reports_incomplete_calls_without_fabricating_completion(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        cfg = replace(config,runtime=replace(config.runtime,time_limit_seconds=.00001),
                      evaluation=replace(config.evaluation,window_end_seconds=.00001))
        with tempfile.TemporaryDirectory() as directory:
            report = run_ep_audit(cfg,Path(directory)/'cutoff',True)['simulation']
            self.assertEqual(report['status'],'passed')
            self.assertEqual(report['completed_calls'],0)
            self.assertGreater(report['active_compute_steps'],0)


if __name__ == '__main__':
    unittest.main()
