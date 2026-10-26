"""Independent numerical oracles for the evaluation pipeline."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.config import DEFAULT_CONFIG, load_config
from joint_scheduling.evaluation.config import EvaluationConfig, parse_config
from joint_scheduling.evaluation.demo import create_demo_trace
from joint_scheduling.evaluation.export import export_result
from joint_scheduling.evaluation.metrics import EvaluationError, analyze_trace, quantile
from joint_scheduling.evaluation.recorder import TraceRecorder
from joint_scheduling.validation import ConfigError


REPO = Path(__file__).resolve().parents[3]


class TraceFixtures(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def trace(self, specs, horizon=6.0, name="events.jsonl"):
        events = []
        for rid, arrival, tokens, completion, expected, failed in specs:
            events.append((arrival, 0, rid, "arrival", expected))
            for index, time in enumerate(tokens, 1):
                events.append((time, 1, rid, "token", index))
            if completion is not None:
                events.append((completion, 2, rid, "complete", None))
            if failed is not None:
                events.append((failed, 2, rid, "fail", None))
        path = self.root / name
        with TraceRecorder(path, "oracle", 3) as recorder:
            for time, _, rid, kind, value in sorted(events):
                if kind == "arrival":
                    recorder.request_arrived(rid, time, 0.1, 0.1, value)
                elif kind == "token":
                    recorder.token_returned(rid, time, value)
                elif kind == "complete":
                    recorder.request_completed(rid, time)
                else:
                    recorder.request_failed(rid, time, "cancelled")
            recorder.finish(horizon)
        return path

    def oracle(self):
        return self.trace([
            ("carry", 0.5, [0.55, 0.6], 1.0, 2, None),
            ("pass", 1.0, [1.1, 1.2, 1.3], 1.3, 3, None),
            ("handoff_fail", 1.1, [1.15, 1.5, 1.55], 1.55, 3, None),
            ("single", 2.0, [2.05], 2.05, 1, None),
            ("failed", 2.5, [], None, 3, 2.7),
            ("drained", 3.5, [3.6, 4.0, 4.1], 4.2, 3, None),
            ("pending", 3.8, [], None, 3, None),
            ("end_arrival", 4.0, [4.1], 4.1, 1, None),
        ])


class EvaluationTests(TraceFixtures):
    def test_token_metrics_and_joint_slo(self):
        result = analyze_trace(self.oracle(), EvaluationConfig(window_start_seconds=1, window_end_seconds=4))
        rows = {row["request_id"]: row for row in result.requests}
        row = rows["handoff_fail"]
        self.assertAlmostEqual(row["ttft_seconds"], 0.05)
        self.assertAlmostEqual(row["handoff_seconds"], 0.35)
        self.assertAlmostEqual(row["later_max_itl_seconds"], 0.05)
        self.assertAlmostEqual(row["max_itl_seconds"], 0.35)
        self.assertAlmostEqual(row["mean_tpot_seconds"], 0.2)
        self.assertAlmostEqual(row["e2e_seconds"], 0.45)
        self.assertTrue(row["ttft_slo_met"])
        self.assertTrue(row["later_itl_slo_met"])
        self.assertFalse(row["slo_met"])
        self.assertTrue(rows["pass"]["slo_met"])
        self.assertAlmostEqual(row["minimum_normalized_slo_margin"], -2.5)

    def test_single_token_is_not_zero_filled(self):
        result = analyze_trace(self.oracle(), EvaluationConfig(window_start_seconds=1, window_end_seconds=4))
        single = next(row for row in result.requests if row["request_id"] == "single")
        for metric in ("handoff_seconds", "later_max_itl_seconds", "max_itl_seconds", "mean_tpot_seconds"):
            self.assertIsNone(single[metric])
        self.assertTrue(single["slo_met"])
        self.assertEqual(result.summary["latencies"]["ttft_seconds"]["count"], 4)
        self.assertEqual(result.summary["latencies"]["handoff_seconds"]["count"], 3)

    def test_two_tokens_only_have_handoff(self):
        path = self.trace([("two", 0, [0.05, 0.12], 0.12, 2, None)])
        row = analyze_trace(path, EvaluationConfig(window_end_seconds=6)).requests[0]
        self.assertAlmostEqual(row["handoff_seconds"], 0.07)
        self.assertIsNone(row["later_max_itl_seconds"])
        self.assertTrue(row["slo_met"])

    def test_later_stall_is_not_hidden_by_mean_tpot(self):
        path = self.trace([("stall", 0, [0.01, 0.02, 0.2, 0.21, 0.22, 0.23], 0.23, 6, None)])
        row = analyze_trace(path, EvaluationConfig(window_end_seconds=6)).requests[0]
        self.assertLess(row["mean_tpot_seconds"], row["itl_slo_seconds"])
        self.assertTrue(row["handoff_slo_met"])
        self.assertAlmostEqual(row["later_max_itl_seconds"], 0.18)
        self.assertFalse(row["slo_met"])

    def test_per_request_slo_overrides_default(self):
        path = self.trace([("slow_first", 0, [0.2], 0.2, 1, None)])
        row = analyze_trace(path, EvaluationConfig(window_end_seconds=6, default_ttft_slo_seconds=1)).requests[0]
        self.assertEqual(row["ttft_slo_seconds"], 0.1)
        self.assertFalse(row["ttft_slo_met"])
        self.assertFalse(row["slo_met"])

    def test_window_carry_in_drain_and_censoring(self):
        result = analyze_trace(self.oracle(), EvaluationConfig(window_start_seconds=1, window_end_seconds=4))
        window = result.summary["completion_window"]
        self.assertEqual(window["completed_count"], 4)
        self.assertEqual(window["slo_met_count"], 3)
        self.assertEqual(window["carry_in_completed_count"], 1)
        self.assertAlmostEqual(window["completion_throughput_rps"], 4 / 3)
        self.assertEqual(window["slo_goodput_rps"], 1)
        self.assertEqual(window["slo_attainment"], 0.75)
        cohort = result.summary["arrival_cohort"]
        self.assertEqual(cohort["arrived_count"], 6)
        self.assertEqual(cohort["completed_by_observation_end"], 4)
        self.assertEqual(cohort["failed_count"], 1)
        self.assertEqual(cohort["incomplete_count"], 1)
        self.assertEqual(cohort["slo_attainment_among_completed"], 0.5)
        self.assertAlmostEqual(cohort["completed_and_slo_fraction"], 1 / 3)
        pending = next(row for row in result.requests if row["request_id"] == "pending")
        self.assertIsNone(pending["slo_met"])

    def test_end_is_exclusive_and_last_bin_uses_its_width(self):
        path = self.trace([("last", 3.8, [3.9], 3.9, 1, None),
                           ("on_end", 3.9, [4.0], 4.0, 1, None)])
        config = EvaluationConfig(window_start_seconds=1, window_end_seconds=4, time_bin_seconds=1.3)
        result = analyze_trace(path, config)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 1)
        self.assertAlmostEqual(result.timeseries[-1]["end_seconds"] - result.timeseries[-1]["start_seconds"], 0.4)
        self.assertAlmostEqual(result.timeseries[-1]["completion_throughput_rps"], 2.5)

    def test_empty_run_has_zero_throughput_and_null_attainment(self):
        result = analyze_trace(self.trace([]), EvaluationConfig(window_end_seconds=6))
        self.assertEqual(result.summary["completion_window"]["slo_goodput_rps"], 0)
        self.assertIsNone(result.summary["completion_window"]["slo_attainment"])
        self.assertIsNone(result.summary["latencies"]["ttft_seconds"]["mean"])
        self.assertIsNone(result.summary["latencies"]["ttft_seconds"]["quantiles"]["0.99"])

    def test_waiting_is_included_in_ttft_and_defaults_apply(self):
        path = self.root / "waiting.jsonl"
        with TraceRecorder(path, "waiting") as recorder:
            recorder.request_arrived("r", 1)
            recorder.record_event("binding_committed", 2, request_id="r")
            recorder.token_returned("r", 3, 1)
            recorder.request_completed("r", 3)
            recorder.finish(6)
        result = analyze_trace(path, EvaluationConfig(window_end_seconds=6, default_ttft_slo_seconds=1))
        self.assertEqual(result.requests[0]["ttft_seconds"], 2)
        self.assertFalse(result.requests[0]["slo_met"])
        self.assertEqual(result.summary["auxiliary_event_count"], 1)

    def test_quantile_linear_interpolation(self):
        self.assertEqual(quantile([0, 10], 0.95), 9.5)
        self.assertEqual(quantile([4], 0.99), 4)
        self.assertIsNone(quantile([], 0.99))

    def test_bad_traces_are_rejected(self):
        base = [json.loads(line) for line in self.oracle().read_text().splitlines()]
        variants = []
        variants.append(("missing end", base[:-1]))
        bad = copy.deepcopy(base)
        next(event for event in bad if event["event"] == "token_returned")["token_index"] = 2
        variants.append(("missing token", bad))
        bad = copy.deepcopy(base)
        next(event for event in bad if event["event"] == "request_arrived")["ttft_slo_seconds"] = 0
        variants.append(("zero slo", bad))
        bad = copy.deepcopy(base)
        bad[-1]["time_seconds"] = 0
        variants.append(("backwards clock", bad))
        variants.append(("event after end", base + [{"event": "extra", "time_seconds": 7}]))
        variants.append(("unknown request", [base[0], {"event": "token_returned", "time_seconds": 0, "request_id": "ghost", "token_index": 1}, base[-1]]))
        bad = copy.deepcopy(base)
        next(event for event in bad if event["event"] == "request_arrived")["expected_output_tokens"] = 10
        variants.append(("premature completion", bad))
        for label, events in variants:
            with self.subTest(label=label):
                path = self.root / "bad.jsonl"
                path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                with self.assertRaises(EvaluationError):
                    analyze_trace(path, EvaluationConfig(window_end_seconds=4))
        with self.assertRaises(EvaluationError):
            analyze_trace(self.root / "events.jsonl", EvaluationConfig(window_end_seconds=7))

    def test_recorder_refuses_overwrite_and_backwards_events(self):
        path = self.root / "new.jsonl"
        with TraceRecorder(path, "x") as recorder:
            recorder.request_arrived("r", 1)
            with self.assertRaises(ValueError):
                recorder.token_returned("r", 0, 1)
            recorder.finish(6)
            with self.assertRaises(ValueError):
                recorder.record_event("other", 7)
        with self.assertRaises(FileExistsError):
            TraceRecorder(path, "x")

    def test_export_is_self_contained_and_refuses_overwrite(self):
        source = self.oracle()
        result = analyze_trace(source, EvaluationConfig(window_end_seconds=4))
        output = export_result(result, self.root / "result")
        self.assertEqual((output / "events.jsonl").read_bytes(), source.read_bytes())
        for name in ("summary.json", "config_snapshot.json", "requests.csv", "tokens.csv", "timeseries.csv"):
            self.assertGreater((output / name).stat().st_size, 0)
        self.assertIn(",,,", (output / "requests.csv").read_text())
        with self.assertRaises(FileExistsError):
            export_result(result, output)

    def test_evaluation_configuration_and_old_config_compatibility(self):
        raw = json.loads(DEFAULT_CONFIG.read_text())
        raw.pop("evaluation", None)
        old = self.root / "old.json"
        old.write_text(json.dumps(raw))
        self.assertEqual(load_config(old).evaluation.window_end_seconds, 10)
        for data in ({"window_end_seconds": 0}, {"percentiles": [0.99, 0.5]},
                     {"percentiles": [True]}, {"plot_formats": ["png", "png"]},
                     {"charts": ["unknown"]}, {"plot_dpi": False}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)

    def test_cli_evaluate_and_nonzero_invalid_input_exit(self):
        trace = create_demo_trace(self.root / "demo.jsonl")
        output = self.root / "cli-result"
        command = [sys.executable, "-m", "joint_scheduling", "evaluate", "--trace", str(trace), "--output", str(output)]
        completed = subprocess.run(command, cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads((output / "summary.json").read_text())
        self.assertTrue(summary["run"]["metadata"]["synthetic"])
        self.assertEqual(subprocess.run(command, cwd=str(REPO), capture_output=True).returncode, 2)


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is optional")
class PlotTests(TraceFixtures):
    def test_all_figures_and_formats_and_offline_replot(self):
        from joint_scheduling.evaluation.plots import plot_results
        source = self.oracle()
        config = EvaluationConfig(window_start_seconds=1, window_end_seconds=4,
                                  plot_formats=("png", "pdf", "svg"))
        output = export_result(analyze_trace(source, config), self.root / "result")
        source.unlink()  # Replot must not depend on the original trace path.
        figures = plot_results([output], self.root / "figures", config)
        self.assertEqual(len(figures), 12)
        for path in figures:
            self.assertGreater(path.stat().st_size, 1000)
        with self.assertRaises(FileExistsError):
            plot_results([output], self.root / "figures", config)
        empty_trace = self.trace([], name="empty.jsonl")
        empty_result = export_result(analyze_trace(empty_trace, config), self.root / "empty")
        self.assertEqual(len(plot_results([empty_result], self.root / "empty-figures", replace(config, plot_formats=("png",)))), 4)

    def test_comparison_and_window_mismatch(self):
        from joint_scheduling.evaluation.plots import plot_results
        config = EvaluationConfig(window_end_seconds=6, charts=("slo_throughput",), plot_formats=("png",))
        outputs = []
        for name, seed in (("a", 7), ("b", 9)):
            path = create_demo_trace(self.root / (name + '.jsonl'), name, seed, horizon=6)
            outputs.append(export_result(analyze_trace(path, config), self.root / name))
        self.assertEqual(len(plot_results(outputs, self.root / "comparison", config)), 1)
        with self.assertRaises(EvaluationError):
            plot_results([outputs[0], outputs[0]], self.root / "duplicate", config)
        other = export_result(analyze_trace(self.root / 'b.jsonl', replace(config, window_end_seconds=5)), self.root / "other-window")
        with self.assertRaises(EvaluationError):
            plot_results([outputs[0], other], self.root / "bad-comparison", config)


if __name__ == "__main__":
    unittest.main()
