import copy
import heapq
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.binding import Binding, BindingConfig
from joint_scheduling.binding.predictor import WholePlanPredictor
from joint_scheduling.binding.window import BindingWindow
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.config import load_config
from joint_scheduling.prefix_cache import PrefixCache, prefix_chain
from joint_scheduling.reservations import PhysicalObjectReservation as Object


class Clock:
    def __init__(self):
        self.now, self.events, self.sequence = 0.0, [], 0

    def now_seconds(self):
        return self.now

    def call_at(self, at, callback):
        assert at >= self.now
        self.sequence += 1
        heapq.heappush(self.events, (at, self.sequence, callback))

    def run_until(self, end):
        steps = 0
        while self.events and self.events[0][0] <= end:
            self.now, _, callback = heapq.heappop(self.events)
            callback()
            steps += 1
            assert steps < 100, 'zero-time binding retry loop'
        self.now = end


class WindowTests(unittest.TestCase):
    def window(self, **options):
        self.clock, self.calls = Clock(), []
        config = replace(BindingConfig(), window_enabled=True, **options)
        def accept(ids, reason):
            self.calls.append((self.clock.now_seconds(), ids, reason))
            return ids
        return BindingWindow(config, self.clock, accept, .01)

    def test_regular_windows_batch_only_arrived_requests(self):
        window = self.window()
        window.add_request('a', 1)
        self.clock.call_at(.0004, lambda: window.add_request('b', 1))
        self.clock.call_at(.0015, lambda: window.add_request('c', 1))
        self.clock.run_until(.003)
        self.assertEqual(self.calls, [(.001, ('a', 'b'), 'regular'), (.002, ('c',), 'regular')])
        self.assertEqual(window.snapshot()['dispatched_requests'], 3)

    def test_early_deadline_reschedules_and_stale_timer_is_ignored(self):
        window = self.window(early_trigger_margin_ms=.1)
        window.add_request('a', 1)
        self.clock.call_at(.0002, lambda: window.add_request('urgent', .0006))
        self.clock.run_until(.002)
        self.assertEqual(len(self.calls), 1)
        self.assertAlmostEqual(self.calls[0][0], .0005)
        self.assertEqual(self.calls[0][1:], (('a', 'urgent'), 'ttft_deadline'))

    def test_max_wait_may_be_shorter_than_regular_interval(self):
        window = self.window(window_ms=2, max_wait_ms=.3)
        window.add_request('a', 1)
        self.clock.run_until(.001)
        self.assertEqual(self.calls, [(.0003, ('a',), 'max_wait')])

    def test_deferred_expired_requests_do_not_spin_and_cutoff_preserves_pending(self):
        window = self.window(early_trigger_margin_ms=0)
        window.on_window = lambda ids, reason: ()
        window.add_request('a', 0)
        self.clock.run_until(.003)
        self.assertLessEqual(window.snapshot()['windows'], 4)
        self.assertEqual(window.snapshot()['pending_request_ids'], ['a'])
        cutoff = self.window(window_ms=20, max_wait_ms=20)
        cutoff.add_request('a', 1)
        self.clock.run_until(.01)
        self.assertEqual(cutoff.snapshot()['windows'], 0)
        self.assertEqual(cutoff.snapshot()['pending_request_ids'], ['a'])

    def test_invalid_callback_and_duplicate_ids_are_rejected(self):
        window = self.window()
        window.add_request('a', 1)
        with self.assertRaises(ValueError):
            window.add_request('a', 1)
        window.on_window = lambda ids, reason: ('unknown',)
        with self.assertRaises(ValueError):
            self.clock.run_until(.001)


class PredictorTests(unittest.TestCase):
    def setUp(self):
        self.profile = ComputeProfile(PACKAGE/'backend/fixtures/prefix_compute_profile.json')
        base = load_config(PACKAGE/'configs/layered_smoke.json')
        self.config = replace(base, binding=replace(base.binding, prediction_enabled=True,
            prediction_decode_steps=8), prefix_cache=replace(base.prefix_cache, enabled=False))
        self.predictor = WholePlanPredictor(self.config, self.profile)

    def row(self, **values):
        return dict(dict(input_tokens=128, arrival_seconds=0, max_output_tokens=3,
                         state='pending_binding', p=None, d=None), **values)

    def state(self, rows, now=0):
        state = self.predictor.empty_state(now)
        state.update(observed_at_seconds=now, visible_at_seconds=now, requests=rows)
        return state

    def test_entire_plan_accounts_for_fifo_and_is_pure(self):
        state = self.state({'a': self.row(), 'b': self.row()})
        plan = [Binding('a', 'p0', 'd0'), Binding('b', 'p0', 'd1')]
        before = copy.deepcopy(state)
        single = self.predictor.predict(plan[:1], state)['a']
        result = self.predictor.predict(plan, state)
        self.assertGreater(result['b'].ttft_seconds, result['a'].ttft_seconds)
        self.assertEqual(single.ttft_seconds, result['a'].ttft_seconds)
        self.assertEqual(state['ledger'].snapshot(), before['ledger'].snapshot())
        self.assertEqual(state['requests'], before['requests'])
        self.assertEqual(self.predictor.predict(plan, state), result)

    def test_window_wait_and_declared_decision_overhead_are_in_ttft(self):
        state = self.state({'a': self.row()}, now=.004)
        plan = [Binding('a', 'p0', 'd0')]
        baseline = self.predictor.predict(plan, state)['a']
        config = replace(self.config, binding=replace(self.config.binding, decision_overhead_us=50))
        delayed = WholePlanPredictor(config, self.profile).predict(plan, state)['a']
        self.assertGreater(baseline.ttft_seconds, .004)
        self.assertAlmostEqual(delayed.ttft_seconds-baseline.ttft_seconds, .00005, places=10)

    def test_new_plan_changes_existing_requests_decode_contention(self):
        state = self.state({'existing': self.row(state='queued_decode', p='p0', d='d0',
            max_output_tokens=32, token_times=[.0009], prefill_finished=True, ready_layers=4,
            received_transfer_bytes_by_rank=[128*32768//4]*4), 'new': self.row(arrival_seconds=.001)}, now=.001)
        state['instances'] = {'d0': dict(queue=['existing'], active_request_id=None)}
        alone = self.predictor.predict([], state)['existing']
        together = self.predictor.predict([Binding('new', 'p1', 'd0')], state)
        self.assertIn('existing', together)
        self.assertGreater(together['existing'].later_max_itl_seconds, alone.later_max_itl_seconds)

    def test_cache_hits_change_cost_and_shared_memory_is_counted_once(self):
        config = replace(self.config, prefix_cache=replace(self.config.prefix_cache, enabled=True))
        predictor = WholePlanPredictor(config, self.profile)
        blocks = [dict(id='shared', tokens=64)]
        state = predictor.empty_state(.001)
        state.update(observed_at_seconds=.001, visible_at_seconds=.001,
            requests={'a': self.row(prefix_blocks=blocks), 'b': self.row(prefix_blocks=blocks)})
        plan = [Binding('a', 'p0', 'd0'), Binding('b', 'p0', 'd0')]
        cold = predictor.predict(plan, state)['a']
        cache = PrefixCache(config.prefix_cache, state['ledger'], predictor.namespace)
        key = prefix_chain(blocks, predictor.namespace)[0]
        for instance in ('p0', 'd0'):
            placement = predictor.placements[instance]
            objects = [Object('warm:'+instance, 'gpu-{}'.format(g), 64*32768//4, ('warm',)) for g in placement.gpu_ids]
            state['ledger'].try_commit('warm:'+instance, objects)
            for obj in objects:
                state['ledger'].materialize(obj.memory_domain_id, obj.object_id, obj.total_size_bytes)
            cache.publish(instance, key, objects, 0)
        state['ledger'].release_request('warm')
        state['cache_entries'] = cache.entries
        for domain in state['ledger'].capacities:
            state['ledger'].capacities[domain] = int(1.6*2**20)
        before = copy.deepcopy(cache.snapshot())
        hot = predictor.predict(plan, state)
        self.assertEqual((hot['a'].prefill_hit_tokens, hot['a'].decode_hit_tokens), (64, 64))
        self.assertLess(hot['a'].ttft_seconds, cold.ttft_seconds)
        self.assertTrue(hot['a'].feasible)
        self.assertTrue(hot['b'].feasible)
        self.assertEqual(cache.snapshot(), before)

    def test_capacity_failure_does_not_mutate_ledger(self):
        state = self.state({'a': self.row(), 'b': self.row()})
        for domain in state['ledger'].capacities:
            state['ledger'].capacities[domain] = int(1.5*2**20)
        before = state['ledger'].snapshot()
        result = self.predictor.predict([Binding('a', 'p0', 'd0'), Binding('b', 'p0', 'd0')], state)
        self.assertFalse(result['a'].feasible)
        self.assertEqual(result['a'].reason, 'capacity_exceeded')
        self.assertIsNone(result['a'].minimum_normalized_margin)
        self.assertEqual(state['ledger'].snapshot(), before)

    def test_exact_future_eos_is_not_used_and_prediction_budget_is_explicit(self):
        state = self.state({'a': self.row(output_tokens=1, max_output_tokens=64)})
        plan = [Binding('a', 'p0', 'd0')]
        a = self.predictor.predict(plan, state)
        state['requests']['a']['output_tokens'] = 10000
        self.assertEqual(self.predictor.predict(plan, state), a)
        self.assertEqual(a['a'].horizon_output_tokens, 9)
        config = replace(self.config, binding=replace(self.config.binding, prediction_max_events=1))
        truncated = WholePlanPredictor(config, self.profile).predict(plan, state)['a']
        self.assertFalse(truncated.complete)
        self.assertIsNone(truncated.minimum_normalized_margin)

    def test_future_state_and_invalid_plans_are_rejected(self):
        plan = [Binding('a', 'p0', 'd0')]
        for state in (self.state({'a': self.row(arrival_seconds=1)}),
                      dict(self.state({'a': self.row()}), visible_at_seconds=1),
                      self.state({'a': self.row(token_times=[1])})):
            with self.assertRaises(ValueError):
                self.predictor.predict(plan, state)
        state = self.state({'a': self.row()})
        for invalid in (plan+plan, [Binding('missing', 'p0', 'd0')], [Binding('a', 'd0', 'p0')]):
            with self.assertRaises(ValueError):
                self.predictor.predict(invalid, state)

    def test_kv_wait_is_not_added_wholesale_to_ttft(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = copy.deepcopy(self.profile.raw)
            raw['phases']['prefill']['tp_size'] = raw['phases']['decode']['tp_size'] = 1
            for layer in raw['phases']['prefill']['layers']:
                layer['collectives_per_step'] = 0
            raw['phases']['decode']['collectives_per_step'] = 0
            path = Path(folder)/'single-rank.json'
            path.write_text(json.dumps(raw))
            placements = tuple(replace(p, gpu_ids=(p.gpu_ids[0],)) for p in self.config.topology.placements)
            config = replace(self.config, topology=replace(self.config.topology, placements=placements, nic_bandwidth_gbps=1))
            predictor = WholePlanPredictor(config, ComputeProfile(path))
            state = predictor.empty_state()
            state['requests'] = {'a': self.row(max_output_tokens=1)}
            plan = [Binding('a', 'p0', 'd0')]
            single = predictor.predict(plan, state)['a']
            state['requests']['a']['max_output_tokens'] = 3
            multi = predictor.predict(plan, state)['a']
            self.assertAlmostEqual(single.ttft_seconds, multi.ttft_seconds)
            self.assertGreater(multi.handoff_seconds, multi.ttft_seconds)


if __name__ == '__main__':
    unittest.main()
