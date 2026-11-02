"""Batch membership, shared communication and complete resource lifetimes."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.topology import InstancePlacement


def fixture():
    def points(seconds, size):
        return [dict(tokens=t, compute_seconds=seconds, collective_bytes=128*size) for t in (1,64)]
    return dict(schema_version=2,provenance=dict(synthetic=True,description='Synthetic batch lifecycle test'),
        network_communication_excluded=True,model='batch-test',weights_bytes_per_gpu=0,kv_bytes_per_token=2048,
        phases=dict(prefill=dict(tp_size=2,layers=[dict(kv_bytes_per_token=1024,collectives_per_step=1,
            points=points(.00005,1)) for _ in range(2)]),
            decode=dict(tp_size=2,collectives_per_step=2,points=points(.02,1),
                batches=[dict(batch_size=b,points=points(s,b)) for b,s in ((1,.02),(2,.023),(4,.029))])))


class BatchProfileTests(unittest.TestCase):
    def test_batch_interpolation_padded_context_and_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'profile.json'; raw=fixture()
            # Context-sensitive curve distinguishes max context from mean context.
            for batch in raw['phases']['decode']['batches']:
                batch['points'][1]['compute_seconds'] += .01
            raw['phases']['decode']['points']=copy.deepcopy(raw['phases']['decode']['batches'][0]['points'])
            path.write_text(json.dumps(raw)); p=ComputeProfile(path)
            sample=p.lookup_decode_batch([1,64,10],2)
            self.assertAlmostEqual(sample['compute_seconds'],.036)
            self.assertEqual(sample['collective_bytes'],384)
            self.assertEqual(sample['collectives_per_step'],2)
            for contexts,tp in (([],2),([1]*5,2),([65,1],2),([1,1],4)):
                with self.subTest(contexts=contexts,tp=tp),self.assertRaises(ValueError):
                    p.lookup_decode_batch(contexts,tp)
            raw['phases']['decode'].pop('batches'); path.write_text(json.dumps(raw)); p=ComputeProfile(path)
            self.assertEqual(p.lookup_decode_batch([1],2),p.lookup('decode',1,2))
            with self.assertRaisesRegex(ValueError,'batch coverage'):p.lookup_decode_batch([1,1],2)


@unittest.skipUnless(backend_available(),'build native backend first')
class ContinuousBatchNetworkTests(unittest.TestCase):
    def test_join_exit_shared_collectives_and_no_lost_tokens_across_kv_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'profile.json').write_text(json.dumps(fixture()))
            requests=[dict(request_id=f'r{i}',arrival_seconds=a,input_tokens=4+2*i,output_tokens=o,
                ttft_slo_seconds=1,itl_slo_seconds=.1) for i,(a,o) in enumerate(((0,6),(0,3),(0,4),(.025,2)))]
            (root/'workload.json').write_text(json.dumps(dict(schema_version=1,provenance=dict(synthetic=True),requests=requests)))
            base=load_config(PACKAGE/'configs/layered_smoke.json')
            config=replace(base,config_path=root/'config.json',
                recording=replace(base.recording,mode='debug'),
                topology=replace(base.topology,num_leaves=1,num_spines=1,nodes_per_leaf=4,gpus_per_node=1,
                    placements=(InstancePlacement('p0','prefill',(0,1)),InstancePlacement('d0','decode',(2,3)))),
                feedback=replace(base.feedback,compute_profile_path=str(root/'profile.json'),
                    state_observation_enabled=False,network_observation_enabled=False,scheduling_overhead_us=0),
                runtime=replace(base.runtime,workload_path=str(root/'workload.json'),time_limit_seconds=.5),
                evaluation=replace(base.evaluation,window_start_seconds=0,window_end_seconds=.5),
                kv_scheduler=replace(base.kv_scheduler,collective_protection_enabled=False,inflight_budget_mode='fixed',
                    max_resource_inflight_bytes=1048576))
            for mode in ('whole_ready_immediate','layer_ready_immediate','layer_ready_slo'):
                for limit in (1,4):
                    with self.subTest(mode=mode,limit=limit):
                        out=root/f'{mode}-{limit}'
                        result=run_baseline(replace(config,runtime=replace(config.runtime,kv_transfer_mode=mode,decode_batch_size=limit)),out)
                        self.assertEqual(len(result.tokens),15)
                        self.assertTrue(all(r['status']=='completed' for r in result.requests))
                        self.assertEqual(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((out/'final_resources.json').read_text()).values()),0)
                        events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
                        batches=[e for e in events if e['event']=='decode_batch_started']
                        self.assertEqual(sum(e['batch_size'] for e in batches),11)
                        self.assertEqual(sum(e['event']=='collective_started' for e in events),8+2*len(batches))
                        ends={e['batch_id']:e for e in events if e['event']=='decode_batch_completed'}
                        for batch in batches:
                            self.assertEqual(len(batch['request_ids']),len(set(batch['request_ids'])))
                            end=ends[batch['batch_id']]
                            for rid in batch['request_ids']:
                                self.assertTrue(any(t['event']=='token_returned' and t['request_id']==rid and t['time_seconds']==end['time_seconds'] for t in events))
                        if limit>1:
                            self.assertGreater(max(e['batch_size'] for e in batches),1)
                            late=next(e for e in batches if 'r3' in e['request_ids'])
                            self.assertGreaterEqual(late['time_seconds'],.025)
                            self.assertLess(len(batches),11)
                        else:self.assertTrue(all(e['batch_size']==1 for e in batches))

            cutoff_dir=root/'cutoff'
            run_baseline(replace(config,runtime=replace(config.runtime,time_limit_seconds=.01,decode_batch_size=4),
                evaluation=replace(config.evaluation,window_end_seconds=.01)),cutoff_dir)
            summary=json.loads((cutoff_dir/'decode_batch_summary.json').read_text())
            self.assertTrue(summary['active_batches_at_end'])
            self.assertGreater(summary['started_batches'],summary['completed_batches'])
            self.assertGreater(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((cutoff_dir/'final_resources.json').read_text()).values()),0)
