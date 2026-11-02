"""Retention changes must not alter simulation decisions or measured service."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config
from joint_scheduling.evaluation import TraceRecorder, analyze_trace
from joint_scheduling.evaluation.config import EvaluationConfig
from joint_scheduling.evaluation.export import export_result
from joint_scheduling.evaluation.pressure import summarize_pressure_states
from joint_scheduling.recording import ESSENTIAL_AUXILIARY_EVENTS
from joint_scheduling.evaluation.recorder import LIFECYCLE_EVENTS


def ready():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class RecordingTests(unittest.TestCase):
    def test_omitted_setting_defaults_to_essential_and_modes_are_validated(self):
        raw = json.loads((PACKAGE / 'configs/baseline_smoke.json').read_text())
        self.assertNotIn('recording', raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(raw))
            self.assertEqual(load_config(path).recording.mode, 'essential')
            raw['recording'] = {'mode': 'debug'}
            path.write_text(json.dumps(raw))
            self.assertTrue(load_config(path).recording.detailed)
            raw['recording']['mode'] = 'typo'
            path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_essential_keeps_failures_unfinished_requests_and_clock_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            with TraceRecorder(path, 'test', detailed=False) as recorder:
                recorder.request_arrived('failed', 0)
                recorder.request_failed('failed', .1, 'capacity')
                recorder.request_arrived('unfinished', .2)
                recorder.token_returned('unfinished', .3, 1)
                recorder.record_event('resources', .4, domains={'large': 'detail'})
                with self.assertRaises(ValueError):
                    recorder.record_event('resources', .35)
                recorder.finish(1)
            result = analyze_trace(path, EvaluationConfig(window_end_seconds=1))
            self.assertEqual([r['status'] for r in result.requests], ['failed', 'incomplete'])
            self.assertEqual(result.requests[0]['failure_reason'], 'capacity')
            self.assertNotIn('resources', [e['event'] for e in events(path)])
            self.assertEqual(recorder.omitted_counts, {'resources': 1})

    def test_single_trace_reference_remains_valid_when_whole_run_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'run'
            with TraceRecorder(run / 'events.jsonl', 'test', detailed=False) as recorder:
                recorder.request_arrived('r', 0)
                recorder.token_returned('r', .1, 1)
                recorder.request_completed('r', .1)
                recorder.finish(1)
            result = analyze_trace(run / 'events.jsonl', EvaluationConfig(window_end_seconds=1))
            export_result(result, run / 'result', copy_trace=False)
            self.assertFalse((run / 'result/events.jsonl').exists())
            summary = json.loads((run / 'result/summary.json').read_text())
            moved = run.rename(Path(directory) / 'moved')
            trace = moved / 'result' / summary['raw_trace_file']
            self.assertEqual(analyze_trace(trace, EvaluationConfig(window_end_seconds=1)).requests, result.requests)


@unittest.skipUnless(ready(), 'build ns-3 for recording equivalence checks')
class RecordingNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = load_config(PACKAGE / 'configs/prefix_cache_smoke.json')
        self.config = replace(base,
            binding=replace(base.binding, policy='joint_lexicographic', joint_selection_enabled=True,
                            window_enabled=True, window_ms=.2, early_trigger_margin_ms=.05,
                            prediction_enabled=False, wall_clock_diagnostics=False, decision_overhead_us=5),
            feedback=replace(base.feedback, observation_delay_us=50),
            runtime=replace(base.runtime, time_limit_seconds=.015),
            evaluation=replace(base.evaluation, window_start_seconds=.001, window_end_seconds=.013))
        rows = [dict(request_id=str(i), arrival_seconds=arrival, input_tokens=128,
                     output_tokens=4, max_output_tokens=8, prefix_blocks=[dict(id='shared', tokens=64)])
                for i, arrival in enumerate((0, .004, .006))]
        self.workload = self.root / 'workload.json'
        self.workload.write_text(json.dumps(dict(schema_version=1, provenance={'synthetic': True}, requests=rows)))

    def compare_modes(self, config, name):
        # Match the suite contract: one immutable source capsule per comparison,
        # shared by cases. Its fixed cost is reported separately from records.
        from joint_scheduling.experiments.provenance import freeze_source
        source=freeze_source(self.root/(name+'-source'))
        results, outputs = {}, {}
        for mode in ('essential', 'debug'):
            outputs[mode] = self.root / (name + '-' + mode)
            results[mode] = run_baseline(replace(config, recording=replace(config.recording, mode=mode)),
                                         outputs[mode], workload_path=self.workload,source_snapshot=source)
        small, full = results['essential'], results['debug']
        self.assertEqual(small.requests, full.requests)
        self.assertEqual(small.tokens, full.tokens)
        for key in ('completion_window', 'arrival_cohort', 'pressure', 'prefix_cache', 'binding'):
            self.assertEqual(small.summary[key], full.summary[key])
        keep = LIFECYCLE_EVENTS | ESSENTIAL_AUXILIARY_EVENTS
        actual = [e for e in events(outputs['essential'] / 'events.jsonl') if e['event'] != 'run_metadata']
        expected = [e for e in events(outputs['debug'] / 'events.jsonl') if e['event'] in keep - {'run_metadata'}]
        self.assertEqual(actual, expected)
        for filename in ('observation/summary.json', 'observation/network_summary.json',
                         'observation/kv_scheduler_summary.json', 'final_resources.json'):
            if (outputs['debug'] / filename).exists():
                self.assertEqual(json.loads((outputs['essential'] / filename).read_text()),
                                 json.loads((outputs['debug'] / filename).read_text()))
        for filename in ('state.jsonl', 'state.csv', 'network_samples.jsonl', 'observed_state.json'):
            self.assertFalse((outputs['essential'] / 'observation' / filename).exists())
            self.assertTrue((outputs['debug'] / 'observation' / filename).exists())
        with (outputs['debug'] / 'observation/state.csv').open(newline='') as handle:
            expected_states = summarize_pressure_states(csv.DictReader(handle),
                config.evaluation.window_start_seconds, config.evaluation.window_end_seconds)
        recorded = json.loads((outputs['essential'] / 'observation/summary.json').read_text())['measurement_window']
        self.assertEqual(recorded['mean'], expected_states['mean'])
        self.assertEqual(recorded['maximum'], expected_states['maximum'])
        for mode, output in outputs.items():
            for filename in ('resolved_config.json', 'workload.json', 'compute_profile.json', 'events.jsonl',
                             'result/requests.csv', 'result/tokens.csv', 'result/summary.json',
                             'network/backend_metadata.json', 'network/backend.log'):
                self.assertTrue((output / filename).is_file(), filename)
            self.assertFalse((output / 'result/events.jsonl').exists())
        sizes = {mode: sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
                 for mode, output in outputs.items()}
        self.assertLess(sizes['essential'], sizes['debug'] / 5)
        identities=[json.loads((output/'reproducibility.json').read_text()) for output in outputs.values()]
        self.assertEqual(identities[0]['source']['source_tree_sha256'],identities[1]['source']['source_tree_sha256'])
        for output,identity in zip(outputs.values(),identities):
            self.assertEqual((output/identity['source']['archive']).resolve(),source/'source.tar.gz')
        print('Shared reproducibility capsule ({}, bytes): {}'.format(name,
            sum(p.stat().st_size for p in source.rglob('*') if p.is_file())))
        print('Recording footprint ({}, bytes): {}'.format(name, sizes))
        return small

    def test_retention_does_not_change_joint_binding_network_feedback_or_slo(self):
        for kv_mode in ('layer_ready_immediate', 'layer_ready_slo'):
            with self.subTest(kv_mode=kv_mode):
                result = self.compare_modes(replace(self.config,
                    runtime=replace(self.config.runtime, kv_transfer_mode=kv_mode)), kv_mode)
                self.assertEqual(result.summary['arrival_cohort']['incomplete_count'], 0)
                self.assertGreater(result.summary['binding']['candidate_scores'], 0)
                self.assertGreater(result.summary['prefix_cache']['hit_tokens'], 0)

    def test_cutoff_is_still_incomplete_with_memory_and_backlog_preserved(self):
        config = replace(self.config, runtime=replace(self.config.runtime, time_limit_seconds=.0008),
                         evaluation=replace(self.config.evaluation, window_start_seconds=0,
                                            window_end_seconds=.0008, time_bin_seconds=.0001))
        result = self.compare_modes(config, 'cutoff')
        self.assertGreater(result.summary['arrival_cohort']['incomplete_count'], 0)
        self.assertGreater(result.summary['pressure']['backlog_end'], 0)


if __name__ == '__main__':
    unittest.main()
