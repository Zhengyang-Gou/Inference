"""Chunk timing, prefill fairness, exact sharding and network dependencies."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.backend.tests.test_continuous_batching import fixture
from joint_scheduling.config import load_config
from joint_scheduling.kv_scheduler import split_layer_kv
from joint_scheduling.topology import InstancePlacement


def profile_fixture():
    raw=fixture()
    for layer,size in zip(raw['phases']['prefill']['layers'],(3,5)):
        layer['kv_bytes_per_token']=size
    raw['kv_bytes_per_token']=8
    raw['phases']['prefill']['chunked']=dict(collectives_per_layer=1,groups=[dict(chunk_tokens=q,points=[
        dict(cached_tokens=c,compute_seconds=.002*q+.0001*c,embedding_seconds=.0001*q,collective_bytes=16*q)
        for c in (0,64)]) for q in (1,4)])
    return raw


class ChunkInputTests(unittest.TestCase):
    def test_context_cost_tail_chunk_and_missing_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';raw=profile_fixture();path.write_text(json.dumps(raw))
            p=ComputeProfile(path);chunks=p.prefill_chunks(9,2,4)
            self.assertEqual([(c['cached_tokens'],c['chunk_tokens']) for c in chunks],[(0,4),(4,4),(8,1)])
            self.assertGreater(chunks[1]['steps'][1]['compute_seconds'],chunks[0]['steps'][1]['compute_seconds'])
            self.assertAlmostEqual(chunks[0]['steps'][0]['compute_seconds']-chunks[0]['steps'][1]['compute_seconds'],.0004)
            self.assertEqual(chunks[2]['steps'][0]['collective_bytes'],16)
            for tokens,tp,size in ((9,4,4),(9,2,5),(70,2,4)):
                with self.assertRaises(ValueError):p.prefill_chunks(tokens,tp,size)
            raw['phases']['prefill'].pop('chunked');path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError,'cached-context'):ComputeProfile(path).prefill_chunks(9,2,4)

    def test_odd_rank_ranges_partition_full_layer_without_rounding_growth(self):
        full=split_layer_kv('r',0,(0,1),(2,3),27,2,0)
        parts=[]
        for prefix,size in ((0,12),(12,12),(24,3)):
            parts.extend(split_layer_kv('r',0,(0,1),(2,3),size,2,0,start_bytes=prefix))
        self.assertEqual(len({b.block_id for b in parts}),len(parts))
        for gpu in (0,1):
            expected=sum(b.size_bytes for b in full if b.source_gpu_id==gpu)
            blocks=[b for b in parts if b.source_gpu_id==gpu]
            self.assertEqual(sum(b.size_bytes for b in blocks),expected)
            position=0
            for block in blocks:
                self.assertEqual(block.offset_bytes,position)
                position+=block.size_bytes
            self.assertEqual(position,expected)


@unittest.skipUnless(backend_available(),'build native backend first')
class ChunkNetworkTests(unittest.TestCase):
    def test_round_robin_first_token_full_kv_and_memory_for_all_transfer_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'profile.json').write_text(json.dumps(profile_fixture()))
            requests=[dict(request_id=rid,arrival_seconds=at,input_tokens=tokens,output_tokens=out)
                for rid,at,tokens,out in (('long',0,9,4),('short',.001,2,3),('single',.002,1,1))]
            (root/'workload.json').write_text(json.dumps(dict(schema_version=1,provenance=dict(synthetic=True),requests=requests)))
            base=load_config(PACKAGE/'configs/layered_smoke.json')
            config=replace(base,config_path=root/'config.json',recording=replace(base.recording,mode='debug'),
                topology=replace(base.topology,num_leaves=1,num_spines=1,nodes_per_leaf=4,gpus_per_node=1,
                    placements=(InstancePlacement('p0','prefill',(0,1)),InstancePlacement('d0','decode',(2,3)))),
                feedback=replace(base.feedback,compute_profile_path=str(root/'profile.json'),
                    state_observation_enabled=False,network_observation_enabled=False,scheduling_overhead_us=0),
                runtime=replace(base.runtime,workload_path=str(root/'workload.json'),time_limit_seconds=.5,
                    decode_batch_size=4,prefill_chunk_tokens=4),
                evaluation=replace(base.evaluation,window_start_seconds=0,window_end_seconds=.5),
                kv_scheduler=replace(base.kv_scheduler,max_block_bytes=2,max_resource_inflight_bytes=65536,
                    collective_protection_enabled=False,inflight_budget_mode='fixed'))
            for mode in ('whole_ready_immediate','whole_ready_chunked','layer_ready_immediate','layer_ready_fifo','layer_ready_slo'):
                with self.subTest(mode=mode):
                    out=root/mode
                    result=run_baseline(replace(config,runtime=replace(config.runtime,kv_transfer_mode=mode)),out)
                    self.assertEqual(len(result.tokens),8)
                    self.assertTrue(all(r['status']=='completed' for r in result.requests))
                    events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
                    starts=[(e['request_id'],e['chunk_index']) for e in events if e['event']=='prefill_chunk_started']
                    self.assertEqual(starts,[('long',0),('short',0),('single',0),('long',1),('long',2)])
                    chunkends=[e for e in events if e['event']=='prefill_chunk_completed']
                    first={e['request_id']:e['time_seconds'] for e in events if e['event']=='token_returned' and e['token_index']==1}
                    self.assertLess(first['short'],first['long'])
                    self.assertEqual(first['long'],next(e['time_seconds'] for e in chunkends if e['request_id']=='long' and e['chunk_index']==2))
                    sends=[e for e in events if e['event']=='transfer_started' and e.get('kind')=='kv']
                    # Network transfer events record kind as transfer_kind.
                    if not sends:sends=[e for e in events if e['event']=='transfer_started' and e.get('transfer_kind')=='kv']
                    self.assertEqual(sum(e['size_bytes'] for e in sends),(9+2)*8)
                    ready={e['request_id']:e['time_seconds'] for e in events if e['event']=='kv_all_received'}
                    for e in events:
                        if e['event']=='decode_started':
                            self.assertGreaterEqual(e['time_seconds'],ready[e['request_id']])
                            self.assertGreaterEqual(e['time_seconds'],first[e['request_id']])
                    self.assertEqual(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((out/'final_resources.json').read_text()).values()),0)
                    summary=json.loads((out/'prefill_chunk_summary.json').read_text())
                    self.assertEqual(summary['completed_chunks'],5)
                    self.assertEqual(summary['completed_query_tokens'],12)
                    if mode in ('layer_ready_fifo','layer_ready_slo'):
                        budget=json.loads((out/'observation/kv_scheduler_summary.json').read_text())
                        self.assertEqual(budget['pending_blocks'],0)
                        self.assertEqual(budget['inflight_blocks'],0)
            # A new FIFO head cannot reserve D memory while a partial prefill owns
            # it. The partial request must resume, drain, and release its memory.
            small=profile_fixture();small['weights_bytes_per_gpu']=2**30-60
            (root/'small_profile.json').write_text(json.dumps(small))
            limited=replace(config,feedback=replace(config.feedback,compute_profile_path=str(root/'small_profile.json')),
                topology=replace(config.topology,gpu_memory_gib=1),
                reservations=replace(config.reservations,capacity_safety_fraction=0))
            result=run_baseline(limited,root/'memory-pressure')
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            self.assertEqual(len(result.tokens),8)
            # End a run mid-chunk: no synthetic first token or early resource release.
            cutoff=root/'cutoff'
            result=run_baseline(replace(config,runtime=replace(config.runtime,time_limit_seconds=.005),
                evaluation=replace(config.evaluation,window_end_seconds=.005)),cutoff)
            self.assertEqual(len(result.tokens),0)
            state=json.loads((cutoff/'prefill_chunk_summary.json').read_text())
            self.assertEqual(state['started_chunks'],1)
            self.assertEqual(state['completed_chunks'],0)
            self.assertGreater(sum(v['occupied_bytes']+v['reserved_bytes'] for v in json.loads((cutoff/'final_resources.json').read_text()).values()),0)
