"""Layer cost conservation, visible progress, EP order and token completion."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.execution.budgets import BudgetEstimator
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.experiments.ep_audit import EPFlowAudit,run_ep_audit
from joint_scheduling.experiments.sensitivity import perturb_profile


def fixture():
    raw=json.loads((PACKAGE/'backend/fixtures/ep_compute_profile.json').read_text())
    raw['phases']['prefill']['layers'].append(copy.deepcopy(raw['phases']['prefill']['layers'][-1]))
    raw['kv_bytes_per_token']+=raw['phases']['prefill']['layers'][-1]['kv_bytes_per_token']
    raw['expert_parallel'].update(prefill_layer_ids=[1,2],decode_layers=2,
        routing_mode='layerwise_periodic',routing_seed=7,communication_mode='token_rank',
        routing_pattern=[[0,1],[2,3]],local_combine_seconds_per_extra_assignment=1e-6)
    decode=raw['phases']['decode'];decode['collectives_per_step']=3
    families={}
    for name,fraction in (('dense',.25),('moe',.375)):
        curve=copy.deepcopy({k:v for k,v in decode.items() if k!='tp_size'})
        curve['collectives_per_step']=1
        for group in [curve]+curve['batches']:
            for point in group['points']:point['compute_seconds']*=fraction
        families[name]=curve
    decode.update(layers=['dense','moe','moe'],layer_profiles=families,step_overhead_seconds=0.)
    return raw


class LayeredDecodeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def load(self,raw):
        path=self.root/'profile.json';path.write_text(json.dumps(raw));return ComputeProfile(path)

    def test_batch_context_layer_costs_and_communication_are_conserved(self):
        p=self.load(fixture())
        for contexts in ([1],[10,60],[2,20,37],[64]*4):
            total=p.lookup_decode_batch(contexts,2);steps=p.decode_batch_steps(contexts,2)
            self.assertEqual([s['layer_id'] for s in steps],[0,1,2])
            self.assertAlmostEqual(sum(s['compute_seconds'] for s in steps),total['compute_seconds'])
            self.assertEqual(sum(s['collectives_per_step'] for s in steps),total['collectives_per_step'])
            self.assertTrue(all(s['collective_bytes']==total['collective_bytes'] for s in steps))
            self.assertNotEqual(steps[0]['compute_seconds'],steps[1]['compute_seconds'])

    def test_invalid_layer_layout_cost_and_batch_coverage_fail_explicitly(self):
        for kind in ('count','missing','cost','collectives','batch','overhead'):
            raw=fixture();d=raw['phases']['decode']
            if kind=='count':d['layers'].pop()
            if kind=='missing':d['layers'][0]='unknown'
            if kind=='cost':d['layer_profiles']['dense']['points'][0]['compute_seconds']*=2
            if kind=='collectives':d['layer_profiles']['moe']['collectives_per_step']=2
            if kind=='batch':d['layer_profiles']['moe']['batches'].pop()
            if kind=='overhead':d['step_overhead_seconds']=-1
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.load(raw)

    def test_sensitivity_scales_families_and_charges_serial_overhead_once(self):
        p=self.load(fixture());before=p.lookup_decode_batch([10,20],2)['compute_seconds']
        changed=self.load(perturb_profile(fixture(),2,.003))
        steps=changed.decode_batch_steps([10,20],2)
        self.assertAlmostEqual(sum(s['compute_seconds'] for s in steps),2*before+.003)
        self.assertAlmostEqual(steps[0]['compute_seconds'],2*before*.25+.003)

    def test_remaining_budget_excludes_completed_layers_and_hidden_eos(self):
        p=self.load(fixture());steps=p.decode_batch_steps([8],2)
        row=dict(segment_index=0,compute_segment_end=.005,decode_nonexpert_finished=False,output_tokens=4)
        fake=SimpleNamespace(active_decode_steps={'d':dict(steps=steps,index=0)},active_decode_batches={'d':('r',)},
            requests={'r':row},instances={'d':SimpleNamespace(gpu_ids=(2,3))},ep_enabled=False,
            backend=SimpleNamespace(now_seconds=lambda:.002),collective_seconds=lambda ranks,sample:0.)
        first=BudgetEstimator.layered_decode_remaining_seconds(fake,'d')
        self.assertAlmostEqual(first,.018)
        fake.active_decode_steps['d']['index']=2;row['compute_segment_end']=.025
        fake.backend.now_seconds=lambda:.020
        remaining=BudgetEstimator.layered_decode_remaining_seconds(fake,'d')
        self.assertAlmostEqual(remaining,.005)
        row['output_tokens']=99999
        self.assertEqual(BudgetEstimator.layered_decode_remaining_seconds(fake,'d'),remaining)
        row['decode_nonexpert_finished']=True
        self.assertEqual(BudgetEstimator.layered_decode_remaining_seconds(fake,'d'),0.)

    def test_audit_rejects_skipped_layers_and_early_batch_end(self):
        p=self.load(fixture());cfg=load_config(PACKAGE/'configs/ep_smoke.json')
        probe=EPFlowAudit(cfg,p)
        with self.assertRaisesRegex(ValueError,'layer order'):
            probe.event('compute_step_started',0,dict(instance_id='d0',phase='decode',layer_id=1,
                batch_id='b',context_tokens=[8]))
        probe=EPFlowAudit(cfg,p)
        probe.event('compute_step_started',0,dict(instance_id='d0',phase='decode',layer_id=0,
            batch_id='b',context_tokens=[8]))
        probe.event('compute_step_completed',1,dict(instance_id='d0'))
        with self.assertRaisesRegex(ValueError,'before all layers'):
            probe.event('decode_batch_completed',1,dict(instance_id='d0',batch_id='b'))


@unittest.skipUnless(backend_available(),'native backend not built')
class LayeredDecodeNetworkTests(unittest.TestCase):
    def test_layers_interleave_with_ep_and_all_members_return_after_last_layer(self):
        base=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'profile.json';path.write_text(json.dumps(fixture()))
            for mode in ('whole_ready_immediate','layer_ready_slo'):
                cfg=replace(base,feedback=replace(base.feedback,compute_profile_path=str(path)),
                    runtime=replace(base.runtime,kv_transfer_mode=mode),
                    kv_scheduler=replace(base.kv_scheduler,policy='slo_mlu' if mode=='layer_ready_slo' else 'slo_slack'))
                report=run_ep_audit(cfg,root/mode,True)['simulation']
                self.assertEqual(report['status'],'passed');self.assertEqual(report['active_decode_batches'],0)
                run=root/mode/'run'
                events=[json.loads(line) for line in (run/'events.jsonl').read_text().splitlines()]
                batches=[e for e in events if e['event']=='decode_batch_started']
                for batch in batches:
                    starts=[e for e in events if e['event']=='compute_step_started' and e.get('phase')=='decode' and e.get('batch_id')==batch['batch_id']]
                    self.assertEqual([e['layer_id'] for e in starts],[0,1,2])
                    end=next(e for e in events if e['event']=='decode_batch_completed' and e['batch_id']==batch['batch_id'])
                    for rid in batch['request_ids']:
                        self.assertTrue(any(e['event']=='token_returned' and e['request_id']==rid and
                                            e['time_seconds']==end['time_seconds'] for e in events))
                    prior=[e for e in events if e['event']=='ep_call_completed' and e.get('phase')=='decode' and
                           e['source_instance_id']==batch['instance_id'] and e['layer_id']==1 and
                           batch['time_seconds']<=e['time_seconds']<=end['time_seconds']]
                    self.assertEqual(len(prior),1)
                    self.assertLessEqual(prior[0]['time_seconds'],starts[2]['time_seconds'])
                resources=json.loads((run/'final_resources.json').read_text())
                self.assertTrue(all(v['occupied_bytes']==v['reserved_bytes']==0 for v in resources.values()))

    def test_cutoff_preserves_layer_progress_and_resources(self):
        base=load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'profile.json';path.write_text(json.dumps(fixture()))
            workload=json.loads((PACKAGE/'backend/fixtures/ep_workload.json').read_text());workload['requests']=workload['requests'][:1]
            wp=root/'workload.json';wp.write_text(json.dumps(workload))
            cfg=replace(base,feedback=replace(base.feedback,compute_profile_path=str(path)),
                runtime=replace(base.runtime,workload_path=str(wp),time_limit_seconds=.004,prefill_chunk_tokens=0,
                    prefill_batch_token_budget=0,prefill_batch_size=1,kv_transfer_mode='whole_ready_immediate'),
                evaluation=replace(base.evaluation,window_end_seconds=.004))
            run_baseline(cfg,root/'run')
            summary=json.loads((root/'run/decode_batch_summary.json').read_text())
            self.assertTrue(summary['active_layer_indices'])
            self.assertGreater(summary['started_batches'],summary['completed_batches'])


if __name__=='__main__':unittest.main()
