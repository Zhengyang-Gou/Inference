"""Joint selection uses actual ns-3 dispatch and the existing ACK lifecycle."""
import json
import unittest
from dataclasses import replace

from joint_scheduling.backend.tests import test_binding_runtime as helpers
from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.config import load_config


@unittest.skipUnless(helpers.ready(), 'build ns-3 for joint binding checks')
class JointRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.helper = helpers.BindingRuntimeTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        base = load_config(PACKAGE/'configs/layered_smoke.json')
        base = replace(base, recording=replace(base.recording, mode="debug"))
        self.config = replace(base, binding=replace(base.binding, joint_selection_enabled=True,
            window_enabled=True, prediction_enabled=False, decision_overhead_us=5),
            feedback=replace(base.feedback, compute_profile_path='../backend/fixtures/prefix_compute_profile.json',
                observation_delay_us=200, network_observation_enabled=False, state_observation_enabled=False),
            prefix_cache=replace(base.prefix_cache, enabled=True),
            runtime=replace(base.runtime, time_limit_seconds=.03),
            evaluation=replace(base.evaluation, window_end_seconds=.03))
        self.rows = [dict(request_id=str(i), arrival_seconds=i//3*.003, input_tokens=128,
                         output_tokens=4, max_output_tokens=16,
                         prefix_blocks=[dict(id='shared', tokens=64)]) for i in range(6)]

    def run_case(self, name, config=None, rows=None):
        return self.helper.run_case(name, config or self.config, rows or self.rows)

    def test_actual_joint_dispatch_shared_prefix_and_delayed_determinism(self):
        a, events, one = self.run_case('one')
        b, _, two = self.run_case('two')
        self.assertEqual(a.tokens, b.tokens)
        self.assertEqual((one/'events.jsonl').read_bytes(), (two/'events.jsonl').read_bytes())
        self.assertEqual(a.summary['completion_window']['completed_count'], 6)
        self.assertTrue(a.summary['binding']['joint_optimization'])
        self.assertEqual(a.summary['binding']['prediction_calls'], 0)
        self.assertEqual(a.summary['binding']['reservation_failures'], 0)
        self.assertTrue((one/'observation/binding_timing.json').is_file())
        cache = json.loads((one/'observation/prefix_cache_summary.json').read_text())
        resources = json.loads((one/'final_resources.json').read_text())
        self.assertEqual(sum(r['reserved_bytes'] for r in resources.values()), 0)
        self.assertEqual(sum(r['occupied_bytes'] for r in resources.values()), cache['resident_bytes'])
        self.assertGreater(cache['hit_tokens'], 0)
        arrivals = {e['request_id']: e['time_seconds'] for e in events if e['event'] == 'request_arrived'}
        for e in events:
            if e['event'] == 'binding_selection':
                self.assertLessEqual(e['visible_at_seconds'], e['time_seconds']+1e-12)
                self.assertTrue(all(arrivals[r] <= e['time_seconds'] for r in e['modeled_request_ids']))
                self.assertLessEqual(e['candidate_scores'], 4*16)
        commits = [e for e in events if e['event'] == 'binding_committed']
        self.assertEqual(len(commits), len({e['request_id'] for e in commits}))

    def test_insufficient_memory_defers_then_retries_without_zero_time_loop(self):
        cfg = self.config
        placements = tuple(p for p in cfg.topology.placements if p.instance_id in ('p0', 'd0'))
        # Enough for one request with its visible output growth, never two.
        memory = (2**30+int(1.3*2**20))/(2**30*(1-cfg.reservations.capacity_safety_fraction))
        cfg = replace(cfg, topology=replace(cfg.topology, placements=placements, gpu_memory_gib=memory),
            prefix_cache=replace(cfg.prefix_cache, enabled=False))
        rows = [dict(r, arrival_seconds=0) for r in self.rows[:3]]
        result, events, output = self.run_case('memory', cfg, rows)
        self.assertEqual(result.summary['completion_window']['completed_count'], 3)
        windows = [e for e in events if e['event'] == 'binding_window_triggered']
        self.assertGreater(len(windows), 1)
        self.assertEqual(len({e['time_seconds'] for e in windows}), len(windows))
        self.assertLess(len(windows), 31)
        for row in json.loads((output/'final_resources.json').read_text()).values():
            self.assertEqual(row['reserved_bytes']+row['occupied_bytes'], 0)

    def test_joint_shared_prefix_lifecycle_in_other_four_kv_modes(self):
        for mode in ('whole_ready_immediate', 'whole_ready_chunked', 'layer_ready_fifo', 'layer_ready_slo'):
            with self.subTest(mode=mode):
                cfg = replace(self.config, runtime=replace(self.config.runtime, kv_transfer_mode=mode))
                rows = [dict(self.rows[0], arrival_seconds=0), dict(self.rows[1], arrival_seconds=.004)]
                result, _, output = self.run_case(mode, cfg, rows)
                self.assertEqual(result.summary['completion_window']['completed_count'], 2)
                cache = result.summary['prefix_cache']
                self.assertGreater(cache['hit_tokens'], 0)
                resources = json.loads((output/'final_resources.json').read_text())
                self.assertEqual(sum(r['reserved_bytes'] for r in resources.values()), 0)
                self.assertEqual(sum(r['occupied_bytes'] for r in resources.values()), cache['resident_bytes'])

    def test_full_predictor_is_not_called_when_joint_sampling_is_zero(self):
        from unittest.mock import patch
        from joint_scheduling.binding.predictor import WholePlanPredictor
        cfg = replace(self.config, binding=replace(self.config.binding,
            prediction_enabled=True, prediction_sample_every=0))
        with patch.object(WholePlanPredictor, 'predict', side_effect=AssertionError('online full rollout')):
            result, events, _ = self.run_case('no-full-rollout', cfg, self.rows[:1])
        self.assertEqual(result.summary['completion_window']['completed_count'], 1)
        self.assertFalse(any(e['event'] == 'binding_prediction' for e in events))

    def test_joint_without_collection_window_and_budget_zero_progress(self):
        cfg = replace(self.config, binding=replace(self.config.binding, window_enabled=False, max_score_work=1))
        result, events, _ = self.run_case('budget', cfg, self.rows[:1])
        self.assertEqual(result.summary['completion_window']['completed_count'], 0)
        self.assertFalse(any(e['event'] == 'binding_committed' for e in events))
        self.assertGreater(result.summary['binding']['budget_exhaustions'], 0)
        cfg = replace(cfg, binding=replace(cfg.binding, max_score_work=16384))
        result, events, _ = self.run_case('immediate', cfg, self.rows[:1])
        commit = next(e for e in events if e['event'] == 'binding_committed')
        self.assertAlmostEqual(commit['time_seconds'], .000005)
        self.assertEqual(result.summary['completion_window']['completed_count'], 1)
