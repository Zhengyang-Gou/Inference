"""Native-network combinations: prefix ranges, chunk/batch execution and binding."""
import json
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.backend.tests import test_prefill_batching as batch_helpers
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.binding.predictor import WholePlanPredictor
from joint_scheduling.experiments import plan_suite, run_suite
from joint_scheduling.config import load_config


@unittest.skipUnless(backend_available(),'build native backend first')
class ComposedExecutionTests(unittest.TestCase):
    def config(self, root, packed=True, joint=True):
        blocks = [dict(id='odd',tokens=3),dict(id='tail',tokens=2)]
        requests = [dict(request_id=rid,arrival_seconds=at,input_tokens=n,output_tokens=o,
            max_output_tokens=4,prefix_blocks=blocks if rid!='short' else [],
            ttft_slo_seconds=1,itl_slo_seconds=.2)
            for rid,at,n,o in [('warm',0,9,3),('reuse',.2,9,4),('full',.2,5,3),('short',.201,2,2)]]
        base = batch_helpers.BatchNetworkTests().make_config(root,requests)
        return replace(base,
            prefix_cache=replace(base.prefix_cache,enabled=True),
            binding=replace(base.binding,joint_selection_enabled=joint,policy='joint_lexicographic' if joint else 'round_robin',
                window_enabled=joint,prediction_enabled=True,prediction_sample_every=1,
                decision_overhead_us=5,max_score_work=16384),
            feedback=replace(base.feedback,observation_delay_us=100),
            runtime=replace(base.runtime,prefill_batch_token_budget=6 if packed else 0,
                prefill_batch_size=3 if packed else 1))

    def check_run(self, result, output):
        self.assertTrue(all(r['status']=='completed' for r in result.requests))
        self.assertEqual(len(result.tokens),12)
        events = [json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]
        hits = {e['request_id']:e for e in events if e['event']=='prefix_cache_lookup'}
        self.assertEqual((hits['reuse']['prefill_hit_tokens'],hits['reuse']['decode_hit_tokens']),(5,5))
        self.assertEqual((hits['full']['prefill_hit_tokens'],hits['full']['decode_hit_tokens']),(5,5))
        chunks = [e for e in events if e['event']=='prefill_chunk_completed']
        self.assertEqual(sum(e['chunk_tokens'] for e in chunks if e['request_id']=='reuse'),4)
        self.assertEqual(sum(e['chunk_tokens'] for e in chunks if e['request_id']=='full'),1)
        sends = [e for e in events if e['event']=='transfer_started' and e['transfer_kind']=='kv']
        self.assertEqual(sum(e['size_bytes'] for e in sends),120)
        self.assertFalse(any(e['request_id']=='full' for e in sends))
        first = {e['request_id']:e['time_seconds'] for e in events if e['event']=='token_returned' and e['token_index']==1}
        ready = {e['request_id']:e['time_seconds'] for e in events if e['event']=='kv_ready'}
        for e in events:
            if e['event']=='decode_started':
                self.assertGreaterEqual(e['time_seconds'],first[e['request_id']])
                self.assertGreaterEqual(e['time_seconds'],ready[e['request_id']])
        resources = json.loads((output/'final_resources.json').read_text())
        self.assertEqual(sum(r['reserved_bytes'] for r in resources.values()),0)
        self.assertEqual(sum(r['occupied_bytes'] for r in resources.values()),result.summary['prefix_cache']['resident_bytes'])
        self.assertEqual(sum(e['event']=='binding_committed' for e in events),4)
        return events

    def test_prefix_chunk_batch_joint_and_diagnostic_across_five_kv_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for packed in (False,True):
                for joint in (False,True):
                    case = root/f'{packed}-{joint}'
                    case.mkdir()
                    config = self.config(case,packed,joint)
                    for mode in ('whole_ready_immediate','whole_ready_chunked','layer_ready_immediate','layer_ready_fifo','layer_ready_slo'):
                        with self.subTest(packed=packed,joint=joint,mode=mode):
                            output = case/mode
                            result = run_baseline(replace(config,runtime=replace(config.runtime,kv_transfer_mode=mode)),output)
                            events = self.check_run(result,output)
                            predictions = [e for e in events if e['event']=='binding_prediction']
                            self.assertTrue(predictions)
                            self.assertTrue(all(e['model']=='bounded_chunk_batch_resource_forecast' for e in predictions))
                            self.assertTrue(all(p['complete'] for e in predictions for p in e['predictions'].values()))
                            if packed and joint and mode=='layer_ready_slo':
                                again = case/'replay'
                                second = run_baseline(replace(config,runtime=replace(config.runtime,kv_transfer_mode=mode)),again)
                                self.assertEqual(result.tokens,second.tokens)
                                self.assertEqual((output/'events.jsonl').read_bytes(),(again/'events.jsonl').read_bytes())

    def test_independent_reuse_and_cutoff_keep_physical_lifetimes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root)
            for prefill,decode in ((True,False),(False,True),(False,False)):
                cfg = replace(config,prefix_cache=replace(config.prefix_cache,prefill_reuse_enabled=prefill,
                    decode_reuse_enabled=decode))
                output = root/f'{prefill}-{decode}'
                result = run_baseline(cfg,output)
                self.assertTrue(all(r['status']=='completed' for r in result.requests))
                events = [json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]
                reuse = next(e for e in events if e['event']=='prefix_cache_lookup' and e['request_id']=='reuse')
                self.assertEqual((reuse['prefill_hit_tokens'],reuse['decode_hit_tokens']),
                                 (5 if prefill else 0,5 if decode else 0))
                transferred = sum(e['size_bytes'] for e in events if e['event']=='transfer_started' and e['transfer_kind']=='kv')
                self.assertEqual(transferred,120 if decode else 200)
                resources = json.loads((output/'final_resources.json').read_text())
                self.assertEqual(sum(r['reserved_bytes'] for r in resources.values()),0)
                self.assertEqual(sum(r['occupied_bytes'] for r in resources.values()),result.summary['prefix_cache']['resident_bytes'])
            cutoff = replace(config,runtime=replace(config.runtime,time_limit_seconds=.005),
                             evaluation=replace(config.evaluation,window_end_seconds=.005))
            result = run_baseline(cutoff,root/'cutoff')
            self.assertEqual(len(result.tokens),0)
            self.assertEqual(result.requests[0]['status'],'incomplete')
            resources = json.loads((root/'cutoff/final_resources.json').read_text())
            self.assertGreater(sum(r['occupied_bytes']+r['reserved_bytes'] for r in resources.values()),0)

    def test_delayed_active_batch_snapshots_and_recording_equivalence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root)
            workload = json.loads((root/'workload.json').read_text())
            workload['requests'][0]['output_tokens'] = 4
            for index,at in enumerate((.07,.1,.205)):
                workload['requests'].append(dict(workload['requests'][2],request_id=f'during-{index}',arrival_seconds=at))
            (root/'workload.json').write_text(json.dumps(workload))
            captured = []
            original = WholePlanPredictor.predict
            def inspect(predictor, plan, state):
                captured.append(copy.deepcopy(state))
                return original(predictor,plan,state)
            with patch.object(WholePlanPredictor,'predict',inspect):
                debug = run_baseline(config,root/'debug')
            essential = run_baseline(replace(config,recording=replace(config.recording,mode='essential')),root/'essential')
            self.assertEqual(debug.tokens,essential.tokens)
            self.assertTrue(all(r['status']=='completed' for r in debug.requests))
            self.assertTrue(any(len(s['instances']['p0']['active_request_ids'])>1 for s in captured))
            self.assertTrue(any(len(s['instances']['d0']['active_request_ids'])>1 for s in captured))
            for state in captured:
                self.assertLessEqual(state['visible_at_seconds'],state['now_seconds']+1e-12)
                for row in state['requests'].values():
                    self.assertNotIn('output_tokens',row)
                    self.assertLessEqual(row['arrival_seconds'],state['now_seconds'])
                    self.assertTrue(all(t<=state['observed_at_seconds']+1e-12 for t in row.get('token_times',())))
                    if row['state']=='prefill':
                        self.assertIn('prefill_computed_tokens',row)
                        self.assertIsNotNone(row['active_prefill_chunk'])

    def test_experiment_sweeps_execute_composed_modes_and_freeze_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = PACKAGE/'configs/composed_execution_smoke.json'
            spec = dict(schema_version=1,base_config=str(source),seeds=[7],sweep={
                'binding.joint_selection_enabled':[False,True], 'runtime.decode_batch_size':[1,4],
                'runtime.prefill_chunk_tokens':[4], 'runtime.prefill_batch_size':[3],
                'runtime.prefill_batch_token_budget':[6],
                'feedback.network_observation_enabled':[False], 'feedback.state_observation_enabled':[False]})
            path = root/'spec.json'
            path.write_text(json.dumps(spec))
            self.assertEqual(len(plan_suite(path)['runs']),4)
            report = run_suite(path,root/'suite')
            self.assertEqual(report['successful_runs'],4)
            rows = [json.loads(line) for line in (root/'suite/runs.jsonl').read_text().splitlines()]
            self.assertEqual(len({r['profile_sha256'] for r in rows}),1)
            self.assertEqual(len({r['workload_sha256'] for r in rows}),1)
            for row in rows:
                self.assertEqual(row['completed_count'],4)
                self.assertEqual(row['incomplete_count'],0)
                self.assertGreater(row['prefix_hit_tokens'],0)
                actual = row['effective_baseline']
                self.assertEqual(actual['prefill'],'token_budget_running_first_fcfs')
                self.assertEqual(actual['decode_batch_size'],row['parameters']['runtime.decode_batch_size'])
                self.assertEqual(actual['joint_binding_optimization'],row['parameters']['binding.joint_selection_enabled'])

    def test_tight_capacity_defers_then_drains_and_unsampled_joint_uses_no_full_predictor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root)
            raw = json.loads((root/'profile.json').read_text())
            raw['weights_bytes_per_gpu'] = 2**30-60
            (root/'profile.json').write_text(json.dumps(raw))
            limited = replace(config,topology=replace(config.topology,gpu_memory_gib=1),
                reservations=replace(config.reservations,capacity_safety_fraction=0))
            result = run_baseline(limited,root/'limited')
            self.check_run(result,root/'limited')
            self.assertGreater(result.summary['binding']['window']['deferred_checks'],0)
            immediate = replace(config,binding=replace(config.binding,window_enabled=False,prediction_sample_every=0))
            with patch.object(WholePlanPredictor,'predict',side_effect=AssertionError('online full predictor')):
                result = run_baseline(immediate,root/'immediate')
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            self.assertEqual(result.summary['binding']['prediction_calls'],0)

    def test_joint_decode_batches_with_legacy_whole_prefill_and_cached_points(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = json.loads((PACKAGE/'backend/fixtures/prefix_compute_profile.json').read_text())
            decode = raw['phases']['decode']
            for point in decode['points']:
                point['compute_seconds'] = .003
            decode['batches'] = [dict(batch_size=b,points=[dict(point,
                compute_seconds=point['compute_seconds']*factor,collective_bytes=point['collective_bytes']*b)
                for point in decode['points']]) for b,factor in ((1,1),(2,1.2),(4,1.5))]
            profile = root/'profile.json'
            profile.write_text(json.dumps(raw))
            requests = [dict(request_id=str(i),arrival_seconds=at,input_tokens=128,output_tokens=4,
                max_output_tokens=8,prefix_blocks=[dict(id='shared',tokens=64)],
                ttft_slo_seconds=1,itl_slo_seconds=.1) for i,at in enumerate((0,0,.005,.005,.006,.008))]
            workload = root/'workload.json'
            workload.write_text(json.dumps(dict(schema_version=1,provenance=dict(synthetic=True),requests=requests)))
            base = load_config(PACKAGE/'configs/prefix_cache_smoke.json')
            config = replace(base,recording=replace(base.recording,mode='debug'),
                feedback=replace(base.feedback,compute_profile_path=str(profile),network_observation_enabled=False,
                    state_observation_enabled=False,observation_delay_us=100),
                binding=replace(base.binding,joint_selection_enabled=True,window_enabled=True,
                    prediction_enabled=True,prediction_sample_every=1),
                runtime=replace(base.runtime,workload_path=str(workload),decode_batch_size=4,time_limit_seconds=.1),
                evaluation=replace(base.evaluation,window_end_seconds=.1))
            result = run_baseline(config,root/'run')
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            self.assertEqual(len(result.tokens),24)
            self.assertGreater(result.summary['prefix_cache']['hit_tokens'],0)
            batches = json.loads((root/'run/decode_batch_summary.json').read_text())
            self.assertGreater(max(map(int,batches['completed_batch_size_histogram'])),1)
