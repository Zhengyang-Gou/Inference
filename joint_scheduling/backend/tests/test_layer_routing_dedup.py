"""Layer identity, token/rank payload conservation and real ACK/reduction order."""
import copy
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import ExpertProfile
from joint_scheduling.backend.expert_parallel import ExpertParallelMixin
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.experiments.ep_audit import EPFlowAudit, run_ep_audit
from joint_scheduling.experiments.ep_reference import token_reference, stage_flows
from joint_scheduling.topology import InstancePlacement


def revised_profile():
    raw = json.loads((PACKAGE/'backend/fixtures/ep_compute_profile.json').read_text())
    raw['expert_parallel'].update(routing_mode='layerwise_periodic', routing_seed=7,
        communication_mode='token_rank', routing_pattern=[[0,1],[2,3]],
        local_combine_seconds_per_extra_assignment=1e-6)
    return raw


class LayerRoutingTests(unittest.TestCase):
    def test_layer_identity_reproducibility_and_chunk_batch_conservation(self):
        raw = copy.deepcopy(ComputeProfile(PACKAGE/'profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json').expert.raw)
        raw.update(routing_mode='layerwise_periodic',routing_seed=7,communication_mode='token_rank')
        ep = ExpertProfile(raw,61)
        self.assertNotEqual(ep.counts(1,374,3),ep.counts(1,374,4))
        self.assertEqual(ep.counts(1,374,3),ExpertProfile(copy.deepcopy(raw),61).counts(1,374,3))
        for layer in (3,31,60):
            whole, ranks = ep.routing_counts([(5001,19)],16,layer)
            chunks = [ep.routing_counts([(min(512,5001-i),19+i)],16,layer) for i in range(0,5001,512)]
            self.assertEqual(whole,[sum(c[0][i] for c in chunks) for i in range(256)])
            self.assertEqual(ranks,[sum(c[1][i] for c in chunks) for i in range(16)])
            a = ep.routing_counts([(13,41),(7,3)],16,layer)
            self.assertEqual(a,ep.routing_counts([(7,3),(13,41)],16,layer))
            twice = ep.routing_counts([(13,41),(13,41)],16,layer)
            one = ep.routing_counts([(13,41)],16,layer)
            self.assertEqual(twice,([2*n for n in one[0]],[2*n for n in one[1]]))
            huge = ep.routing_counts([(2240*10**8,0)],16,layer)
            period = ep.routing_counts([(2240,0)],16,layer)
            self.assertEqual(huge,([n*10**8 for n in period[0]],[n*10**8 for n in period[1]]))

    def test_rank_dedup_odd_tp_channels_keeps_all_expert_work(self):
        raw = revised_profile()['expert_parallel']
        raw.update(hidden_size=5,top_k=3,routing_pattern=[[0,1,2],[1,2,3]])
        ep = ExpertProfile(raw,2)
        places={'a':InstancePlacement('a','prefill',(0,1)),'b':InstancePlacement('b','prefill',(2,3))}
        runtime=SimpleNamespace(profile=SimpleNamespace(expert=ep),instances=places,ep_groups={'a':('a','b')})
        assignments,ranks=ep.routing_counts([(2,0),(2,0)],2,0)
        self.assertEqual(assignments,[2,4,4,2])
        self.assertEqual(ranks,[4,4])
        self.assertEqual(ExpertParallelMixin.ep_flows(runtime,'a',assignments,rank_tokens=ranks),
                         [(0,2,12),(1,3,8)])
        self.assertEqual(ExpertParallelMixin.ep_flows(runtime,'a',assignments,True,rank_tokens=ranks),
                         [(2,0,24),(3,1,16)])
        reference=token_reference(ep,('a','b'),places,'a',2,[(2,0),(2,0)],0)
        self.assertEqual(Counter(ExpertParallelMixin.ep_flows(runtime,'a',assignments,rank_tokens=ranks)),
                         stage_flows(reference,1,rank_deduplicated=True))
        with self.assertRaisesRegex(ValueError,'incidence'):
            ExpertParallelMixin.ep_flows(runtime,'a',assignments)

    def test_explicit_modes_and_invalid_costs_are_rejected(self):
        for change in ({'routing_mode':'random'}, {'communication_mode':'node'}, {'routing_seed':True},
                       {'routing_seed':-1}, {'local_combine_seconds_per_extra_assignment':-1},
                       {'communication_mode':'expert_assignment','local_combine_seconds_per_extra_assignment':1}):
            raw=revised_profile()['expert_parallel'];raw.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):ExpertProfile(raw,2)

    def test_independent_oracle_detects_reused_layer_and_assignment_payload(self):
        from joint_scheduling.experiments.ep_reference import audit_layout
        config=load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';path.write_text(json.dumps(revised_profile()))
            profile=ComputeProfile(path)
            counts=profile.expert.counts
            with patch.object(profile.expert,'counts',side_effect=lambda n,o=0,layer_id=None:counts(n,o,0)):
                with self.assertRaisesRegex(ValueError,'routing count'):audit_layout(config,profile)
            original=ExpertParallelMixin.ep_flows
            def duplicate(runtime,source,assignments,combine=False,**kwargs):
                width=profile.expert.raw['num_experts']//len(runtime.ep_groups[source])
                kwargs['rank_tokens']=[sum(assignments[i:i+width]) for i in range(0,len(assignments),width)]
                return original(runtime,source,assignments,combine,**kwargs)
            with patch.object(ExpertParallelMixin,'ep_flows',duplicate):
                with self.assertRaisesRegex(ValueError,'payload/endpoint'):audit_layout(config,profile)

    def test_audit_rejects_early_local_combine_and_expert_release(self):
        config=load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';path.write_text(json.dumps(revised_profile()))
            profile=ComputeProfile(path);probe=EPFlowAudit(config,profile)
            counts,_=profile.expert.routing_counts([(2,0)],2,0)
            probe.event('compute_step_started',0,dict(instance_id='p0',phase='prefill',tokens=2,layer_id=0))
            probe.event('ep_call_started',0,dict(ep_call_id='c',source_instance_id='p0',phase='prefill',
                                               layer_id=0,expert_assignments=counts))
            call=probe.calls['c'];expected=stage_flows(call['reference'],1,rank_deduplicated=True)
            probe.event('collective_started',0,dict(ep_call_id='c',collective_type='ep_dispatch',
                size_bytes=sum(s*n for (_,_,s),n in expected.items()),flow_count=sum(expected.values())))
            for index,(src,dst,size) in enumerate(expected):
                probe.event('transfer_started',0,dict(ep_call_id='c',collective_type='ep_dispatch',
                    transfer_id=str(index),source_gpu_id=src,destination_gpu_id=dst,size_bytes=size))
            for index in range(len(expected)):
                probe.event('transfer_completed',1,dict(ep_call_id='c',collective_type='ep_dispatch',
                    transfer_id=str(index),source_reads_complete=True,destination_ready=True))
            probe.event('collective_completed',1,dict(ep_call_id='c',collective_type='ep_dispatch'))
            probe.event('compute_segment_started',1,dict(task_kind='expert',ep_call_id='c',instance_id='p0',assignments=2))
            probe.event('compute_segment_completed',2,dict(task_kind='expert',ep_call_id='c',instance_id='p0'))
            probe.event('ep_local_combine_started',2,dict(ep_call_id='c',instance_id='p0',token_count=1,
                extra_assignments=1,modeled_compute_seconds=1e-6))
            with self.assertRaisesRegex(ValueError,'too early'):
                probe.event('ep_local_combine_completed',2,dict(ep_call_id='c',instance_id='p0',token_count=1))
            with self.assertRaisesRegex(ValueError,'before local combine'):
                probe.event('ep_expert_completed',2,dict(ep_call_id='c',instance_id='p0',assignments=2))


@unittest.skipUnless(backend_available(),'native backend not built')
class LayerRoutingNetworkTests(unittest.TestCase):
    def test_live_single_packed_and_mlu_keep_work_and_reduce_payload(self):
        config=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            raw=revised_profile();path=root/'profile.json';path.write_text(json.dumps(raw))
            shared=copy.deepcopy(raw);shared['expert_parallel']['communication_mode']='expert_assignment'
            shared['expert_parallel']['local_combine_seconds_per_extra_assignment']=0.
            shared_path=root/'assignment.json';shared_path.write_text(json.dumps(shared))
            reports=[];summaries=[]
            for name,packed,profile_path in (('single',False,path),('packed',True,path),('assignment',True,shared_path)):
                cfg=replace(config,feedback=replace(config.feedback,compute_profile_path=str(profile_path)),
                    runtime=replace(config.runtime,prefill_batch_token_budget=6 if packed else 0,
                                    prefill_batch_size=3 if packed else 1))
                report=run_ep_audit(cfg,root/name,True)['simulation'];reports.append(report)
                summary=json.loads((root/name/'run/expert_parallel_summary.json').read_text());summaries.append(summary)
                self.assertEqual(report['status'],'passed')
                self.assertEqual(report['started_calls'],report['completed_calls'])
                self.assertEqual(report['active_logical_flows']+report['incomplete_calls']+report['active_compute_steps'],0)
                resources=json.loads((root/name/'run/final_resources.json').read_text())
                self.assertTrue(all(v['occupied_bytes']==v['reserved_bytes']==0 for v in resources.values()))
            self.assertEqual({s['routed_assignments'] for s in summaries},{264})
            self.assertEqual(summaries[0]['expert_assignments'],summaries[1]['expert_assignments'])
            self.assertEqual(summaries[1]['expert_assignments'],summaries[2]['expert_assignments'])
            self.assertEqual(summaries[0]['dispatch_bytes'],summaries[1]['dispatch_bytes'])
            self.assertEqual(summaries[1]['dispatch_bytes']*2,summaries[2]['dispatch_bytes'])
            self.assertEqual(summaries[1]['combine_bytes']*2,summaries[2]['combine_bytes'])
            self.assertGreater(summaries[1]['modeled_local_combine_seconds'],0)
            events=[json.loads(line) for line in (root/'packed/run/events.jsonl').read_text().splitlines()]
            calls=[e for e in events if e['event']=='ep_call_started' and e['phase']=='decode']
            by_layer={layer:{tuple(e['expert_assignments']) for e in calls if e['layer_id']==layer} for layer in (0,1)}
            self.assertTrue(by_layer[0] and by_layer[1])
            self.assertTrue(any(e['event']=='ep_local_combine_completed' for e in events))
            observer=json.loads((root/'packed/run/observation/summary.json').read_text())
            recorded=sum(v['compute_busy_seconds'] for v in observer['instances'].values())
            expected=sum(e['modeled_compute_seconds'] for e in events if e['event']=='compute_segment_started')
            expected+=summaries[1]['modeled_local_combine_seconds']
            self.assertAlmostEqual(recorded,expected,places=7)

    def test_deduplicated_cutoff_reports_active_dependencies(self):
        config=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=revised_profile()
            raw['expert_parallel']['local_combine_seconds_per_extra_assignment']=.1
            path=root/'profile.json';path.write_text(json.dumps(raw))
            cfg=replace(config,feedback=replace(config.feedback,compute_profile_path=str(path)),
                runtime=replace(config.runtime,time_limit_seconds=.05),
                evaluation=replace(config.evaluation,window_end_seconds=.05))
            report=run_ep_audit(cfg,root/'audit',True)['simulation']
            self.assertEqual(report['status'],'passed')
            self.assertGreater(report['incomplete_calls'],0)


if __name__=='__main__':unittest.main()
