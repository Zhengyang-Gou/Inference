import copy
import unittest
from dataclasses import replace

from joint_scheduling.binding.fast import FastPlanPredictor, IncrementalJointPolicy, WorkBudget
from joint_scheduling.binding.tests import test_binding as helpers
from joint_scheduling.prefix_cache import PrefixCache, prefix_chain
from joint_scheduling.reservations import PhysicalObjectReservation as Object
from joint_scheduling.reservations.ledger import CapacityLedger


class JointTests(unittest.TestCase):
    def setUp(self):
        helper = helpers.PredictorTests()
        helper.setUp()
        self.profile = helper.profile
        self.config = replace(helper.config, binding=replace(helper.config.binding,
            joint_selection_enabled=True, prediction_enabled=False, max_score_work=100000))
        self.predictor = FastPlanPredictor(self.config, self.profile)
        self.policy = IncrementalJointPolicy(self.config, self.predictor)

    def row(self, **values):
        return dict(dict(input_tokens=128, arrival_seconds=0, max_output_tokens=16,
                         prefix_blocks=[], state='pending_binding', p=None, d=None), **values)

    def state(self, rows):
        state = self.predictor.empty_state(.001)
        state.update(requests=rows, observed_at_seconds=.001, visible_at_seconds=.001)
        return state

    def select(self, state, ids=None):
        return self.policy.select(ids or tuple(r for r, v in state['requests'].items() if not v.get('p')), state)

    def warm(self, state, instance, ready=.0001):
        blocks = [dict(id='shared', tokens=64)]
        cache = PrefixCache(self.config.prefix_cache, state['ledger'], self.predictor.namespace)
        cache.entries = state['cache_entries']
        objects = tuple(Object('warm:'+instance, 'gpu-'+str(g), 64*32768//4, ('warm',))
                        for g in self.predictor.placements[instance].gpu_ids)
        self.assertTrue(state['ledger'].try_commit(instance, objects))
        for o in objects:
            state['ledger'].materialize(o.memory_domain_id, o.object_id, o.total_size_bytes)
        cache.publish(instance, prefix_chain(blocks, self.predictor.namespace)[0], objects, ready)
        state['ledger'].release_request('warm')
        return blocks

    def test_greedy_updates_queues_and_is_pure_and_deterministic(self):
        state = self.state({str(i): self.row() for i in range(4)})
        original = copy.deepcopy(state)
        decision = self.select(state)
        self.assertEqual(len(decision.bindings), 4)
        self.assertEqual(len({b.prefill_instance_id for b in decision.bindings}), 2)
        self.assertEqual(len({b.decode_instance_id for b in decision.bindings}), 2)
        self.assertEqual(self.select(state), decision)
        self.assertEqual(state['requests'], original['requests'])
        self.assertEqual(state['ledger'].snapshot(), original['ledger'].snapshot())
        self.assertLessEqual(self.policy.last_report['candidate_scores'], 16)
        self.assertTrue(self.policy.last_report['prediction_scope'].startswith('finite'))

    def test_new_binding_changes_existing_slo_score(self):
        state = self.state({'old': self.row(p='p0', d='d0', state='queued_decode', token_times=[.0009],
            prefill_finished=True, resources_committed=True), 'new': self.row()})
        pred = self.predictor
        pred.begin(state, WorkBudget(100000))
        old = pred.score()[1]['old']
        c = pred.candidate('new', state['requests']['new'], 'p1', 'd0')
        new = pred.score(c)[1]['old']
        self.assertGreater(new.later_max_itl_seconds, old.later_max_itl_seconds)
        self.assertLess(new.minimum_normalized_margin, old.minimum_normalized_margin)

    def test_shared_directed_link_load_affects_other_decode_instance(self):
        state = self.state({'a': self.row(), 'b': self.row()})
        pred = self.predictor
        pred.begin(state, WorkBudget(100000))
        a = pred.candidate('a', state['requests']['a'], 'p0', 'd0')
        b = pred.candidate('b', state['requests']['b'], 'p0', 'd1')
        self.assertTrue(set(a.kv) & set(b.kv))
        pred.accept(a)
        before = pred.score()[1]['a']
        after = pred.score(b)[1]['a']
        self.assertGreater(after.handoff_seconds, before.handoff_seconds)

    def test_independent_prefix_hits_not_ready_hits_and_shared_references(self):
        self.config = replace(self.config, prefix_cache=replace(self.config.prefix_cache, enabled=True))
        self.predictor = FastPlanPredictor(self.config, self.profile)
        state = self.state({'a': self.row(), 'b': self.row()})
        blocks = self.warm(state, 'p0')
        self.warm(state, 'd1', ready=.002)  # not visible/ready at sampled time
        for row in state['requests'].values():
            row['prefix_blocks'] = blocks
        pred = self.predictor
        before = copy.deepcopy(state['cache_entries'])
        pred.begin(state, WorkBudget(100000))
        a = pred.candidate('a', state['requests']['a'], 'p0', 'd0')
        b = pred.candidate('b', state['requests']['b'], 'p1', 'd1')
        self.assertEqual((a.ph, a.dh, b.ph, b.dh), (64, 0, 0, 0))
        pred.accept(a)
        c = pred.candidate('b', state['requests']['b'], 'p0', 'd0')
        shared = before['p0', prefix_chain(blocks, pred.namespace)[0]].objects[0]
        pred.accept(c)
        self.assertTrue({'a', 'b'} <= pred.memory.refs[shared.memory_domain_id, shared.object_id])
        self.assertEqual(state['cache_entries'], before)
        # D-only reuse must reduce transfer without accelerating P compute.
        state['cache_entries']['d1', prefix_chain(blocks, pred.namespace)[0]].ready_at_seconds = 0
        pred.begin(state, WorkBudget(100000))
        d = pred.candidate('b', state['requests']['b'], 'p1', 'd1')
        cold = pred.candidate('a', self.row(), 'p1', 'd1')
        self.assertEqual((d.ph, d.dh), (0, 64))
        self.assertEqual(d.ps, cold.ps)
        self.assertLess(sum(d.kv.values()), sum(cold.kv.values()))

    def test_capacity_accounts_for_prior_selections_and_output_bound(self):
        state = self.state({'a': self.row(max_output_tokens=64), 'b': self.row(max_output_tokens=64)})
        for domain in state['ledger'].capacities:
            state['ledger'].capacities[domain] = 1700000
        # Force one endpoint pair so the second request cannot overbook it.
        self.predictor.pairs = (('p0', 'd0'),)
        result = self.select(state)
        self.assertEqual(len(result.bindings), 1)
        self.assertEqual(len(result.deferred_request_ids), 1)
        original = self.select(state)
        state['requests']['a']['output_tokens'] = 999999
        self.assertEqual(self.select(state), original)
        growth = [o.total_size_bytes for o in self.policy.last_candidates['a'].objects if o.object_id.startswith('d:')]
        self.assertEqual(growth, [math_ceil((128+64)*32768/4)]*4)

    def test_caps_work_and_retains_results_and_aging(self):
        state = self.state({str(i): self.row() for i in range(8)})
        cfg = replace(self.config, binding=replace(self.config.binding, max_requests_per_round=2, candidate_count=1))
        policy = IncrementalJointPolicy(cfg, FastPlanPredictor(cfg, self.profile))
        result = policy.select(tuple(state['requests']), state)
        self.assertEqual(len(result.bindings), 2)
        self.assertEqual(policy.last_report['candidate_scores'], 2)
        for work in (1, 100, 200):
            cfg2 = replace(cfg, binding=replace(cfg.binding, max_score_work=work))
            pol = IncrementalJointPolicy(cfg2, FastPlanPredictor(cfg2, self.profile))
            pol.select(tuple(state['requests']), state)
            self.assertLessEqual(pol.last_report['score_work'], work)
            self.assertTrue(pol.last_report['budget_exhausted'])
        aged = self.state({'old': self.row(arrival_seconds=0, ttft_slo_seconds=1),
                           'urgent': self.row(arrival_seconds=.019, ttft_slo_seconds=.0001)})
        aged.update(now_seconds=.02, observed_at_seconds=.02, visible_at_seconds=.02)
        cfg = replace(cfg, binding=replace(cfg.binding, max_requests_per_round=1))
        pol = IncrementalJointPolicy(cfg, FastPlanPredictor(cfg, self.profile))
        self.assertEqual(pol.select(('urgent', 'old'), aged).bindings[0].request_id, 'old')

    def test_aging_admits_count_neutral_work_despite_worse_margin(self):
        state = self.state({'old': self.row(p='p0', d='d0', state='queued_decode',
            token_times=[.0199], resources_committed=True, ttft_slo_seconds=1),
            'new': self.row(arrival_seconds=.0199, ttft_slo_seconds=.000001)})
        state.update(now_seconds=.02, observed_at_seconds=.02, visible_at_seconds=.02)
        self.predictor.pairs = (('p1', 'd0'),)
        self.assertEqual(self.select(state).bindings, ())
        state['requests']['new']['arrival_seconds'] = 0
        result = self.select(state)
        self.assertEqual(result.bindings[0].request_id, 'new')

    def test_memoized_layout_matches_execution_for_odd_per_layer_shards(self):
        import json
        import tempfile
        from pathlib import Path
        from joint_scheduling.backend.runtime import ComputeProfile
        from joint_scheduling.prefix_cache.layout import PrefixPlan
        raw = copy.deepcopy(self.profile.raw)
        raw['kv_bytes_per_token'] = 37
        for layer, size in zip(raw['phases']['prefill']['layers'], (10, 9, 7, 11)):
            layer['kv_bytes_per_token'] = size
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'profile.json'
            path.write_text(json.dumps(raw))
            config = replace(self.config, prefix_cache=replace(self.config.prefix_cache, enabled=True))
            profile = ComputeProfile(path)
            pred = FastPlanPredictor(config, profile)
            state = pred.empty_state()
            row = self.row(input_tokens=129, prefix_blocks=[dict(id='odd', tokens=63)])
            state['requests'] = {'a': row}
            pred.begin(state, WorkBudget(100000))
            candidate = pred.candidate('a', row, 'p0', 'd0')
            trial = dict(request_id='a', input_tokens=129, output_tokens=16,
                         prefix_blocks=row['prefix_blocks'], prefill_steps=profile.prefill_steps(129, 4))
            expected = PrefixPlan(pred.cache, trial, pred.placements['p0'], pred.placements['d0'], profile, 0)
            self.assertEqual(candidate.objects, tuple(expected.objects))
            self.assertEqual(candidate.layout.counts, expected.counts)

    def test_budget_exhaustion_keeps_a_previously_selected_binding(self):
        state = self.state({str(i): self.row() for i in range(8)})
        cfg = replace(self.config, binding=replace(self.config.binding, max_score_work=1500))
        policy = IncrementalJointPolicy(cfg, FastPlanPredictor(cfg, self.profile))
        result = policy.select(tuple(state['requests']), state)
        self.assertTrue(policy.last_report['budget_exhausted'])
        self.assertGreater(len(result.bindings), 0)
        self.assertGreater(len(result.deferred_request_ids), 0)

    def test_stale_real_capacity_rejects_batch_without_partial_reservation(self):
        from types import SimpleNamespace
        from joint_scheduling.binding.runtime import BindingRuntime
        state = self.state({'a': self.row(), 'b': self.row()})
        decision = self.select(state)
        ledger = state['ledger'].clone()
        # Capacity changes after the delayed snapshot; preflight must use live
        # occupancy and leave every request/reference untouched on rejection.
        domain = self.policy.last_candidates['b'].objects[0].memory_domain_id
        ledger.try_commit('occupant', [Object('occupant', domain, ledger.capacities[domain], ('occupant',))])
        rows = {'a': {}, 'b': {}}
        host = SimpleNamespace(joint_policy=self.policy, ledger=ledger, prefix_cache=None,
                               requests=rows, binding_active=set())
        before = copy.deepcopy(ledger.__dict__)
        self.assertFalse(BindingRuntime.reserve_binding_batch(host, decision.bindings))
        self.assertEqual(ledger.__dict__, before)
        self.assertEqual(rows, {'a': {}, 'b': {}})

    def test_future_information_is_rejected_and_profile_horizon_is_explicit(self):
        state = self.state({'a': self.row()})
        state['visible_at_seconds'] = .002
        with self.assertRaises(ValueError):
            self.select(state)
        state['visible_at_seconds'] = .001
        state['requests']['a']['arrival_seconds'] = .002
        with self.assertRaises(ValueError):
            self.select(state)
        state['requests']['a'] = self.row(input_tokens=4096, max_output_tokens=100000)
        self.assertEqual(self.select(state).bindings, ())
        state['requests']['a'] = self.row(max_output_tokens=100000)
        self.select(state)
        self.assertEqual(self.policy.last_candidates['a'].horizon, 9)


class BatchLedgerTests(unittest.TestCase):
    def test_atomic_failure_validation_replay_and_pinned_shared_object(self):
        ledger = CapacityLedger({'g': 100})
        shared = Object('shared', 'g', 50, ('cache',))
        self.assertTrue(ledger.try_commit('cache', [shared]))
        ledger.materialize('g', 'shared', 50)
        ledger.pin('g', 'shared')
        before = copy.deepcopy(ledger.__dict__)
        a = [Object('shared', 'g', 50, ('a',)), Object('a', 'g', 30, ('a',))]
        b = [Object('shared', 'g', 50, ('b',)), Object('b', 'g', 30, ('b',))]
        self.assertFalse(ledger.try_commit_batch([('a', a), ('b', b)]))
        self.assertEqual(ledger.__dict__, before)
        with self.assertRaises(ValueError):
            ledger.try_commit_batch([('a', a), ('invalid', [Object('x', 'missing', 1, ('x',))])])
        self.assertEqual(ledger.__dict__, before)
        b[-1] = Object('b', 'g', 20, ('b',))
        self.assertTrue(ledger.try_commit_batch([('a', a), ('b', b)]))
        after = copy.deepcopy(ledger.__dict__)
        self.assertTrue(ledger.try_commit_batch([('a', a), ('b', b)]))
        self.assertEqual(ledger.__dict__, after)
        ledger.release_request('a')
        ledger.release_request('b')
        ledger.release_request('cache')
        self.assertIn(('g', 'shared'), ledger.objects)
        ledger.unpin('g', 'shared')
        self.assertEqual(ledger.objects, {})


def math_ceil(value):
    import math
    return math.ceil(value)
