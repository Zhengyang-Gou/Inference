import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import BackendError, require_backend, PACKAGE
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config
from joint_scheduling.feedback import NetworkTransfer
from joint_scheduling.observation import StateObserver


class Clock:
    time = 0.0

    def now_seconds(self):
        return self.time


def ready():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


class ObservationTests(unittest.TestCase):
    def test_delayed_visibility_immutability_and_time_integrals(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            counts = {"arrived_count": 0, "completed_count": 0, "failed_count": 0, "incomplete_count": 0}
            instances = {"p": {"role": "prefill", "queue_length": 2, "compute_busy": False, "execution_busy": False}}
            resources = {"gpu-0": {"capacity_bytes": 100, "occupied_bytes": 0, "reserved_bytes": 50, "available_bytes": 50}}
            with StateObserver(Path(directory) / "state", clock, 1000000) as observer:
                observer.capture(counts, instances, resources, "initial")
                self.assertIsNone(observer.latest_observed())
                clock.time = 1
                instances["p"].update(queue_length=0, compute_busy=True, execution_busy=True)
                observer.queue_started("p", 1)
                observer.capture(counts, instances, resources, "compute")
                visible = observer.latest_observed()
                self.assertFalse(visible["instances"]["p"]["compute_busy"])
                visible["resources"]["gpu-0"]["reserved_bytes"] = -1
                self.assertEqual(observer.latest_observed()["resources"]["gpu-0"]["reserved_bytes"], 50)
                clock.time = 1.999999999
                self.assertEqual(observer.latest_observed()["time_seconds"], 0)
                clock.time = 2
                self.assertEqual(observer.latest_observed()["time_seconds"], 1)
                report = observer.finish()
                self.assertEqual(report["instances"]["p"]["compute_busy_seconds"], 1)
                self.assertEqual(report["instances"]["p"]["mean_queue_length"], 1)
                self.assertEqual(report["instances"]["p"]["compute_utilization"], 0.5)

    def test_whole_message_inflight_semantics_and_zero_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            with StateObserver(Path(directory) / "state", clock) as observer:
                observer.transfer_started(NetworkTransfer("t", "kv", 0, 1, 100), "local")
                observer.capture({}, {}, {}, "start")
                clock.time = 2
                observer.transfer_completed("t")
                observer.capture({}, {}, {}, "end")
                report = observer.finish()["communication"]["kv"]
                self.assertEqual(report["completed_bytes"], 100)
                self.assertEqual(report["peak_inflight_count"], 1)
                self.assertEqual(report["inflight_byte_seconds"], 200)
            clock.time = 0
            with StateObserver(Path(directory) / "zero", clock) as observer:
                observer.capture({}, {}, {}, "zero")
                self.assertEqual(observer.finish()["observed_until_seconds"], 0)

    @unittest.skipUnless(ready(), "build ns-3 to test state integration")
    def test_network_run_observation_and_no_future_request_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config = load_config(PACKAGE / "configs/baseline_smoke.json")
            workload = {"schema_version": 1, "provenance": {"synthetic": True}, "requests": [
                {"request_id": "now", "arrival_seconds": 0.001, "input_tokens": 128, "output_tokens": 3},
                {"request_id": "future", "arrival_seconds": 1, "input_tokens": 128, "output_tokens": 3}]}
            path = directory / "workload.json"
            path.write_text(json.dumps(workload))
            result = run_baseline(config, directory / "run", workload_path=path)
            observation = directory / "run/observation"
            frames = [json.loads(line) for line in (observation / "state.jsonl").read_text().splitlines()]
            self.assertEqual(frames[0]["requests"]["arrived_count"], 0)
            self.assertEqual(frames[-1]["requests"]["completed_count"], 1)
            self.assertEqual(result.summary["total_requests_recorded"], 1)
            self.assertNotIn('"future"', (observation / "state.jsonl").read_text())
            summary = json.loads((observation / "summary.json").read_text())
            self.assertTrue(any(row["compute_busy_seconds"] > 0 for row in summary["instances"].values()))
            for row in summary["instances"].values():
                self.assertLessEqual(row["compute_busy_seconds"], row["execution_busy_seconds"] + 1e-12)
            for frame in frames:
                for row in frame["resources"].values():
                    self.assertLessEqual(row["occupied_bytes"] + row["reserved_bytes"], row["capacity_bytes"])
            self.assertFalse(frames[-1]["active_transfers"])
            self.assertTrue(all(row["occupied_bytes"] == row["reserved_bytes"] == 0 for row in frames[-1]["resources"].values()))
            visible = json.loads((observation / "observed_state.json").read_text())
            self.assertLessEqual(visible["state"]["visible_at_seconds"], visible["queried_at_seconds"])
            self.assertTrue((observation / "state.csv").exists())

    @unittest.skipUnless(ready(), "build ns-3 to test state integration")
    def test_cutoff_preserves_unfinished_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(PACKAGE / "configs/baseline_smoke.json")
            config = replace(config, runtime=replace(config.runtime, time_limit_seconds=0.00001),
                             evaluation=replace(config.evaluation, window_end_seconds=0.00001, time_bin_seconds=0.00001))
            run_baseline(config, Path(directory) / "run")
            report = json.loads((Path(directory) / "run/observation/summary.json").read_text())
            self.assertGreater(report["requests"]["incomplete_count"], 0)
            self.assertEqual(report["requests"]["completed_count"], 0)
            self.assertTrue(any(row["compute_busy_seconds"] > 0 for row in report["instances"].values()))
