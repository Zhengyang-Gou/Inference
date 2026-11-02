"""Engine-style token packing, independent attention and shared communication."""
import json,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runtime import ComputeProfile,parse_config
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.backend.tests.test_chunked_prefill import profile_fixture
from joint_scheduling.config import load_config
from joint_scheduling.topology import InstancePlacement
from joint_scheduling.experiments.sensitivity import perturb_profile


def batch_profile():
    raw=profile_fixture();chunked=raw['phases']['prefill']['chunked']
    chunked['linear_points']=[dict(tokens=q,compute_seconds=.004+.001*q,
        embedding_seconds=.0001*q,collective_bytes=16*q) for q in (1,2,4,8,16)]
    for group in chunked['groups']:
        q=group['chunk_tokens']
        for point in group['points']:
            a=.0001*q*q+.0002*point['cached_tokens']
            point.update(attention_seconds=a,compute_seconds=.004+.001*q+a)
    return raw


class BatchProfileTests(unittest.TestCase):
    def test_shared_linear_and_independent_contexts_not_concatenated_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';raw=batch_profile();path.write_text(json.dumps(raw));profile=ComputeProfile(path)
            chunks=[dict(chunk_tokens=1,cached_tokens=0),dict(chunk_tokens=1,cached_tokens=4)]
            steps=profile.prefill_batch_steps(chunks,2)
            # One 2-token linear(.006), independent attentions(.0001 + .0009).
            self.assertAlmostEqual(steps[1]['compute_seconds'],.007)
            self.assertAlmostEqual(steps[0]['compute_seconds'],.0072)
            self.assertEqual(steps[0]['collective_bytes'],32)
            self.assertEqual(steps[0]['collectives_per_step'],1)
            solo=sum(profile.prefill_batch_steps([c],2)[1]['compute_seconds'] for c in chunks)
            self.assertLess(steps[1]['compute_seconds'],solo)
            joined=profile.prefill_batch_steps([dict(chunk_tokens=2,cached_tokens=4)],2)
            self.assertNotAlmostEqual(joined[1]['compute_seconds'],steps[1]['compute_seconds'])
            changed=perturb_profile(raw,1.2,.001);path.write_text(json.dumps(changed));other=ComputeProfile(path)
            after=other.prefill_batch_steps(chunks,2)
            self.assertAlmostEqual(sum(s['compute_seconds'] for s in after),1.2*sum(s['compute_seconds'] for s in steps)+.001)
            profile.validate_prefill_batch_coverage(9,4,8,2)
            for c,tp in (([],2),([dict(chunk_tokens=1,cached_tokens=0)],4),([dict(chunk_tokens=5,cached_tokens=0)],2)):
                with self.assertRaises(ValueError):profile.prefill_batch_steps(c,tp)
            legacy=profile_fixture();path.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(ValueError,'decomposed'):ComputeProfile(path).prefill_batch_steps(chunks,2)

    def test_configuration_requires_chunk_cap_and_budget(self):
        for raw in (dict(prefill_batch_size=4),dict(prefill_batch_token_budget=4),dict(prefill_batch_size=0)):
            with self.assertRaises(ValueError):parse_config(raw)
        self.assertEqual(parse_config(dict(prefill_chunk_tokens=4,prefill_batch_token_budget=8,prefill_batch_size=2)).prefill_batch_size,2)


@unittest.skipUnless(backend_available(),'build native backend first')
class BatchNetworkTests(unittest.TestCase):
    def make_config(self,root,requests=None):
        (root/'profile.json').write_text(json.dumps(batch_profile()))
        if requests is None:requests=[dict(request_id=rid,arrival_seconds=arrival,input_tokens=inp,output_tokens=out)
            for rid,arrival,inp,out in (('a',0,7,4),('b',0,3,2),('c',0,6,3),('late',.03,1,1))]
        (root/'workload.json').write_text(json.dumps(dict(schema_version=1,provenance=dict(synthetic=True),requests=requests)))
        base=load_config(PACKAGE/'configs/layered_smoke.json')
        return replace(base,config_path=root/'config.json',recording=replace(base.recording,mode='debug'),
            topology=replace(base.topology,num_leaves=1,num_spines=1,nodes_per_leaf=4,gpus_per_node=1,
                placements=(InstancePlacement('p0','prefill',(0,1)),InstancePlacement('d0','decode',(2,3)))),
            feedback=replace(base.feedback,compute_profile_path=str(root/'profile.json'),state_observation_enabled=False,
                network_observation_enabled=False,scheduling_overhead_us=0),
            runtime=replace(base.runtime,workload_path=str(root/'workload.json'),time_limit_seconds=1,
                decode_batch_size=4,prefill_chunk_tokens=4,prefill_batch_size=3,prefill_batch_token_budget=6),
            evaluation=replace(base.evaluation,window_start_seconds=0,window_end_seconds=1),
            kv_scheduler=replace(base.kv_scheduler,max_block_bytes=2,max_resource_inflight_bytes=65536,
                collective_protection_enabled=False,inflight_budget_mode='fixed'))

    def test_dynamic_budget_running_first_late_join_tokens_kv_and_collectives(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=self.make_config(root)
            for mode in ('whole_ready_immediate','whole_ready_chunked','layer_ready_immediate','layer_ready_fifo','layer_ready_slo'):
                with self.subTest(mode=mode):
                    out=root/mode;result=run_baseline(replace(config,runtime=replace(config.runtime,kv_transfer_mode=mode)),out)
                    self.assertEqual(len(result.tokens),10)
                    self.assertTrue(all(r['status']=='completed' for r in result.requests))
                    events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
                    batches=[e for e in events if e['event']=='prefill_batch_started']
                    self.assertEqual(batches[0]['scheduled_tokens'],{'a':4,'b':2})
                    self.assertEqual(batches[1]['scheduled_tokens'],{'a':3,'b':1,'c':2})
                    self.assertEqual(batches[1]['cached_tokens'],{'a':4,'b':2,'c':0})
                    self.assertTrue(all(e['total_query_tokens']<=6 and e['batch_size']<=3 for e in batches))
                    self.assertEqual(sum(e['total_query_tokens'] for e in batches),17)
                    self.assertTrue(all(e['time_seconds']>=.03 for e in batches if 'late' in e['request_ids']))
                    p_batches=len(batches);d_batches=sum(e['event']=='decode_batch_started' for e in events)
                    self.assertEqual(sum(e['event']=='collective_started' for e in events),2*p_batches+2*d_batches)
                    sends=[e for e in events if e['event']=='transfer_started' and e['transfer_kind']=='kv']
                    self.assertEqual(sum(e['size_bytes'] for e in sends),16*8)
                    first={e['request_id']:e['time_seconds'] for e in events if e['event']=='token_returned' and e['token_index']==1}
                    received={e['request_id']:e['time_seconds'] for e in events if e['event']=='kv_all_received'}
                    for e in events:
                        if e['event']=='decode_started':
                            self.assertGreaterEqual(e['time_seconds'],first[e['request_id']])
                            self.assertGreaterEqual(e['time_seconds'],received[e['request_id']])
                    self.assertEqual(first['a'],first['b'],'members finishing in the same batch return together')
                    self.assertEqual(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((out/'final_resources.json').read_text()).values()),0)
                    summary=json.loads((out/'prefill_batch_summary.json').read_text())
                    self.assertEqual(summary['completed_query_tokens'],17)
                    self.assertEqual(summary['started_batches'],summary['completed_batches'])
                    self.assertFalse(summary['active_batches_at_end'])
                    self.assertLessEqual(summary['peak_running_requests']['p0'],3)

    def test_memory_deferral_running_slot_cap_and_cutoff_keep_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=self.make_config(root)
            raw=batch_profile();raw['weights_bytes_per_gpu']=2**30-50
            (root/'small.json').write_text(json.dumps(raw))
            limited=replace(config,topology=replace(config.topology,gpu_memory_gib=1),
                reservations=replace(config.reservations,capacity_safety_fraction=0),
                feedback=replace(config.feedback,compute_profile_path=str(root/'small.json')))
            result=run_baseline(limited,root/'small')
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            summary=json.loads((root/'small/prefill_batch_summary.json').read_text())
            self.assertGreater(summary['memory_deferred_admissions'],0)
            result=run_baseline(replace(config,runtime=replace(config.runtime,prefill_batch_size=1)),root/'cap1')
            cap=json.loads((root/'cap1/prefill_batch_summary.json').read_text())
            self.assertEqual(set(cap['batch_size_histogram']),{'1'})
            self.assertEqual(cap['peak_running_requests'],{'p0':1})
            result=run_baseline(replace(config,runtime=replace(config.runtime,time_limit_seconds=.005),
                evaluation=replace(config.evaluation,window_end_seconds=.005)),root/'cutoff')
            self.assertEqual(len(result.tokens),0)
            state=json.loads((root/'cutoff/prefill_batch_summary.json').read_text())
            self.assertTrue(state['active_batches_at_end'])
            self.assertEqual(state['completed_batches'],0)
            self.assertGreater(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((root/'cutoff/final_resources.json').read_text()).values()),0)

    def test_old_profile_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=self.make_config(root)
            (root/'profile.json').write_text(json.dumps(profile_fixture()))
            with self.assertRaisesRegex(ValueError,'decomposed'):run_baseline(config,root/'invalid')
            self.assertFalse((root/'invalid').exists())
