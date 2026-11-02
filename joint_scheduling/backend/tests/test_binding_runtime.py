"""Real-clock window dispatch and prediction-only integration checks."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config


def ready():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


@unittest.skipUnless(ready(), 'build ns-3 for binding runtime checks')
class BindingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(PACKAGE/'configs/binding_smoke.json')
        self.config = replace(self.config, recording=replace(self.config.recording, mode="debug"))

    def run_case(self, name, config=None, requests=None):
        workload = None
        if requests is not None:
            workload = self.root/(name+'-workload.json')
            workload.write_text(json.dumps(dict(schema_version=1, provenance={'synthetic': True}, requests=requests)))
        output = self.root/name
        result = run_baseline(config or self.config, output, workload_path=workload)
        events = [json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]
        return result, events, output

    def test_window_batches_urgent_trigger_and_rr_assignment_without_optimizer(self):
        result, events, output = self.run_case('windows')
        self.assertEqual(result.summary['completion_window']['completed_count'], 6)
        windows = [e for e in events if e['event'] == 'binding_window_triggered']
        self.assertEqual(windows[0]['request_ids'], ['binding-0', 'binding-1', 'binding-2'])
        self.assertEqual(windows[0]['reason'], 'ttft_deadline')
        self.assertAlmostEqual(windows[0]['time_seconds'], .00065)
        committed = [e for e in events if e['event'] == 'binding_committed']
        for index, event in enumerate(committed):
            self.assertEqual(event['prefill_instance_id'], 'p0')
            self.assertEqual(event['decode_instance_id'], 'd'+str(index % 2))
            self.assertGreaterEqual(event['time_seconds'], windows[0]['time_seconds']+.000005)
        predictions = [e for e in events if e['event'] == 'binding_prediction']
        self.assertTrue(predictions)
        for event in predictions:
            self.assertLessEqual(event['observed_at_seconds'], event['visible_at_seconds'])
            self.assertLessEqual(event['visible_at_seconds'], event['time_seconds']+1e-12)
            self.assertTrue(set(event['candidate_request_ids']) <= set(event['modeled_request_ids']))
        report = json.loads((output/'observation/binding_summary.json').read_text())
        self.assertFalse(report['joint_optimization'])
        self.assertEqual(report['endpoint_selection'], 'round_robin')
        self.assertEqual(report['predicted_candidates'], 6)
        self.assertEqual(report['prediction_calls'], len(windows))
        self.assertGreater(report['prediction_errors']['ttft_seconds']['samples'], 0)

    def test_prediction_only_does_not_change_actual_tokens_or_capacity(self):
        config = replace(self.config, binding=replace(self.config.binding,
            window_enabled=False, prediction_enabled=False, decision_overhead_us=0))
        a, _, off = self.run_case('off', config)
        b, events, on = self.run_case('prediction', replace(config,
            binding=replace(config.binding, prediction_enabled=True)))
        self.assertEqual(a.tokens, b.tokens)
        self.assertEqual(json.loads((off/'final_resources.json').read_text()),
                         json.loads((on/'final_resources.json').read_text()))
        self.assertTrue(any(e['event'] == 'binding_prediction' for e in events))

    def test_windows_without_predictor_and_max_wait_are_independent_switches(self):
        config = replace(self.config, binding=replace(self.config.binding,
            prediction_enabled=False, window_ms=2, max_wait_ms=.3, decision_overhead_us=0))
        requests = [dict(request_id='a', arrival_seconds=0, input_tokens=128, output_tokens=3)]
        result, events, _ = self.run_case('max-wait', config, requests)
        trigger = next(e for e in events if e['event'] == 'binding_window_triggered')
        self.assertEqual(trigger['reason'], 'max_wait')
        self.assertAlmostEqual(trigger['time_seconds'], .0003)
        self.assertFalse(any(e['event'] == 'binding_prediction' for e in events))
        self.assertEqual(result.summary['completion_window']['completed_count'], 1)

    def test_cutoff_keeps_pending_window_and_pending_decision_incomplete(self):
        for overhead, label in ((0, 'window'), (600, 'decision')):
            config = replace(self.config,
                binding=replace(self.config.binding, prediction_enabled=False, window_enabled=not bool(overhead),
                                window_ms=1,
                                decision_overhead_us=overhead),
                runtime=replace(self.config.runtime, time_limit_seconds=.0005),
                evaluation=replace(self.config.evaluation, window_end_seconds=.0005))
            result, events, output = self.run_case(label, config,
                [dict(request_id='a', arrival_seconds=0, input_tokens=128, output_tokens=3)])
            self.assertEqual(result.summary['arrival_cohort']['incomplete_count'], 1)
            self.assertFalse(any(e['event'] == 'binding_committed' for e in events))
            self.assertFalse(any(e['event'] == 'request_failed' for e in events))
            self.assertEqual(result.summary['completion_window']['completed_count'], 0)
            pending = result.summary['binding']
            if overhead:
                self.assertEqual(pending['pending_decisions'], ['a'])
            else:
                self.assertEqual(pending['window']['pending_request_ids'], ['a'])

    def test_delayed_visibility_no_future_arrivals_and_deterministic_replay(self):
        config = replace(self.config, feedback=replace(self.config.feedback, observation_delay_us=250))
        a, events, one = self.run_case('replay-1', config)
        b, _, two = self.run_case('replay-2', config)
        self.assertEqual((one/'events.jsonl').read_bytes(), (two/'events.jsonl').read_bytes())
        arrivals = {e['request_id']: e['time_seconds'] for e in events if e['event'] == 'request_arrived'}
        for event in events:
            if event['event'] == 'binding_prediction':
                self.assertTrue(all(arrivals[rid] <= event['time_seconds'] for rid in event['modeled_request_ids']))
                self.assertLessEqual(event['visible_at_seconds'], event['time_seconds']+1e-12)
        self.assertEqual(a.tokens, b.tokens)

    def test_state_and_network_observation_can_be_off_while_prediction_runs(self):
        config = replace(self.config, feedback=replace(self.config.feedback,
            state_observation_enabled=False, network_observation_enabled=False))
        result, events, output = self.run_case('minimal', config)
        self.assertEqual(result.summary['completion_window']['completed_count'], 6)
        self.assertFalse((output/'observation/state.jsonl').exists())
        self.assertFalse((output/'observation/network_samples.jsonl').exists())
        self.assertTrue((output/'observation/binding_summary.json').exists())
        self.assertTrue(any(e['event'] == 'binding_prediction' for e in events))


if __name__ == '__main__':
    unittest.main()
