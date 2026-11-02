"""Chunk/batch forecasts use shared costs, visible progress and bounded work."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.tests.test_prefill_batching import batch_profile
from joint_scheduling.binding import Binding
from joint_scheduling.binding.fast import FastPlanPredictor, IncrementalJointPolicy, WorkBudget
from joint_scheduling.binding.predictor import WholePlanPredictor
from joint_scheduling.config import load_config
from joint_scheduling.topology import InstancePlacement


class BatchPredictionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        raw = batch_profile()
        for layer in raw['phases']['prefill']['layers']:
            layer['collectives_per_step'] = 0
        raw['phases']['prefill']['chunked']['collectives_per_layer'] = 0
        raw['phases']['decode']['collectives_per_step'] = 0
        path = root/'profile.json'
        path.write_text(json.dumps(raw))
        self.profile = ComputeProfile(path)
        base = load_config(PACKAGE/'configs/layered_smoke.json')
        self.config = replace(base,
            topology=replace(base.topology,num_leaves=1,num_spines=1,nodes_per_leaf=4,gpus_per_node=1,
                placements=(InstancePlacement('p0','prefill',(0,1)),InstancePlacement('d0','decode',(2,3)))),
            runtime=replace(base.runtime,decode_batch_size=4),
            binding=replace(base.binding,joint_selection_enabled=True,prediction_enabled=True,
                max_score_work=16384,decision_overhead_us=0),
            prefix_cache=replace(base.prefix_cache,enabled=False))

    def row(self, **values):
        return dict(dict(input_tokens=4,arrival_seconds=0,max_output_tokens=3,prefix_blocks=[],
                         state='pending_binding',p=None,d=None,ttft_slo_seconds=1,itl_slo_seconds=1),**values)

    def state(self, predictor, rows):
        state = predictor.empty_state(.001)
        state.update(requests=rows,observed_at_seconds=.001,visible_at_seconds=.001)
        return state

    def test_decode_shared_cost_and_new_binding_changes_existing_intervals(self):
        pred = FastPlanPredictor(self.config,self.profile)
        rows = {rid:self.row(p='p0',d='d0',state='queued_decode',token_times=[0.],
            prefill_finished=True,resources_committed=True) for rid in ('a','b')}
        rows['new'] = self.row()
        state = self.state(pred,rows)
        state['instances'] = {'d0':dict(queue=['a','b'])}
        before = copy.deepcopy(state)
        pred.begin(state,WorkBudget(16384))
        baseline = pred.score()[1]
        self.assertAlmostEqual(baseline['a'].handoff_seconds,.024)
        self.assertAlmostEqual(baseline['a'].later_max_itl_seconds,.023)
        candidate = pred.candidate('new',rows['new'],'p0','d0')
        after = pred.score(candidate)[1]
        self.assertGreater(after['a'].later_max_itl_seconds,baseline['a'].later_max_itl_seconds)
        self.assertAlmostEqual(after['a'].later_max_itl_seconds,.026)
        self.assertEqual(state['requests'],before['requests'])
        self.assertEqual(state['ledger'].snapshot(),before['ledger'].snapshot())
        full = WholePlanPredictor(self.config,self.profile)
        diagnostic = full.predict((candidate.binding,),state)
        self.assertEqual(after,diagnostic)

    def test_chunk_rr_and_packed_prefill_follow_queue_and_token_budget(self):
        for packed in (False,True):
            config = replace(self.config,runtime=replace(self.config.runtime,prefill_chunk_tokens=4,
                prefill_batch_token_budget=6 if packed else 0,prefill_batch_size=3 if packed else 1))
            pred = FastPlanPredictor(config,self.profile)
            state = self.state(pred,{'long':self.row(input_tokens=9,max_output_tokens=1),
                                    'short':self.row(input_tokens=2,max_output_tokens=1)})
            pred.begin(state,WorkBudget(16384))
            for rid in ('long','short'):
                pred.accept(pred.candidate(rid,state['requests'][rid],'p0','d0'))
            result = pred.score()[1]
            self.assertTrue(all(p.complete for p in result.values()))
            self.assertLess(result['short'].ttft_seconds,result['long'].ttft_seconds)
            if packed:
                expected = sum(s['compute_seconds'] for s in self.profile.prefill_batch_steps([
                    dict(chunk_tokens=4,cached_tokens=0),dict(chunk_tokens=2,cached_tokens=0)],2))
                self.assertAlmostEqual(result['short'].ttft_seconds,.001+1e-9+expected)

    def test_active_batches_resume_shared_progress_once(self):
        pred = FastPlanPredictor(self.config,self.profile)
        rows = {rid:self.row(p='p0',d='d0',state='decode',token_times=[0.,.01],
            resources_committed=True,compute_active=True,compute_remaining_seconds=.005,
            segment_index=0) for rid in ('a','b')}
        state = self.state(pred,rows)
        state.update(now_seconds=.011,observed_at_seconds=.011,visible_at_seconds=.011)
        state['instances'] = {'d0':dict(queue=[],active_request_id='a',active_request_ids=('a','b'))}
        pred.begin(state,WorkBudget(16384))
        result = pred.score()[1]
        self.assertEqual(result['a'].predicted_token_times,result['b'].predicted_token_times)
        self.assertAlmostEqual(result['a'].predicted_token_times[-1],.016)
        config = replace(self.config,runtime=replace(self.config.runtime,prefill_chunk_tokens=4,
            prefill_batch_token_budget=6,prefill_batch_size=3))
        pred = FastPlanPredictor(config,self.profile)
        rows = {rid:self.row(p='p0',d='d0',state='prefill',input_tokens=1,max_output_tokens=1,
            resources_committed=True,prefill_started=True,prefill_computed_tokens=0,
            active_prefill_chunk=dict(chunk_tokens=1,cached_tokens=0),
            prefill_layer_index=1,compute_active=True,compute_remaining_seconds=.001,segment_index=0)
            for rid in ('a','b')}
        state = self.state(pred,rows)
        state['instances'] = {'p0':dict(queue=[],active_request_id='a',active_request_ids=('a','b'))}
        pred.begin(state,WorkBudget(16384))
        result = pred.score()[1]
        self.assertEqual(result['a'].predicted_token_times,result['b'].predicted_token_times)
        self.assertAlmostEqual(result['a'].predicted_token_times[0],.002)

    def test_deterministic_budget_exact_eos_and_no_future_information(self):
        pred = FastPlanPredictor(self.config,self.profile)
        state = self.state(pred,{'a':self.row(),'b':self.row()})
        policy = IncrementalJointPolicy(self.config,pred)
        decision = policy.select(('a','b'),state)
        self.assertEqual(len(decision.bindings),2)
        state['requests']['a']['output_tokens'] = 999999
        self.assertEqual(policy.select(('a','b'),state),decision)
        self.assertEqual(policy.select(('a','b'),state),decision)
        tiny = replace(self.config,binding=replace(self.config.binding,max_score_work=1))
        policy = IncrementalJointPolicy(tiny,FastPlanPredictor(tiny,self.profile))
        self.assertFalse(policy.select(('a','b'),state).bindings)
        self.assertTrue(policy.last_report['budget_exhausted'])
        self.assertLessEqual(policy.last_report['score_work'],1)
        bounded = replace(self.config,binding=replace(self.config.binding,prediction_max_events=1))
        result = WholePlanPredictor(bounded,self.profile).predict((Binding('a','p0','d0'),),state)
        self.assertFalse(result['a'].complete)
        self.assertIsNone(result['a'].minimum_normalized_margin)
        state['requests']['a']['token_times'] = [.002]
        with self.assertRaisesRegex(ValueError,'future'):
            pred.begin(state,WorkBudget(16384))
            pred.candidate('a',state['requests']['a'],'p0','d0')

    def test_uniform_eighty_layer_chunks_fit_budget_and_capacity_failure_is_explicit(self):
        raw = copy.deepcopy(self.profile.raw)
        raw['phases']['prefill']['layers'] *= 40
        raw['kv_bytes_per_token'] = sum(layer['kv_bytes_per_token'] for layer in raw['phases']['prefill']['layers'])
        path = Path(self.folder.name)/'large.json'
        path.write_text(json.dumps(raw))
        profile = ComputeProfile(path)
        config = replace(self.config,runtime=replace(self.config.runtime,prefill_chunk_tokens=4,
            prefill_batch_token_budget=6,prefill_batch_size=3))
        pred = FastPlanPredictor(config,profile)
        state = self.state(pred,{'a':self.row(input_tokens=32,ttft_slo_seconds=100,itl_slo_seconds=100)})
        policy = IncrementalJointPolicy(config,pred)
        self.assertEqual(len(policy.select(('a',),state).bindings),1)
        self.assertLessEqual(policy.last_report['score_work'],16384)
        for domain in state['ledger'].capacities:
            state['ledger'].capacities[domain] = 1
        result = WholePlanPredictor(config,profile).predict((Binding('a','p0','d0'),),state)['a']
        self.assertFalse(result.feasible)
        self.assertIsNone(result.minimum_normalized_margin)
        self.assertEqual(result.reason,'capacity_exceeded')
