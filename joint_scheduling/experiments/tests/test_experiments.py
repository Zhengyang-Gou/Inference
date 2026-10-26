import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.backend.build import BackendError, PACKAGE, require_backend
from joint_scheduling.backend.runtime import read_json
from joint_scheduling.experiments import plan_suite, run_suite


def ready():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = read_json(PACKAGE / "configs/baseline_smoke.json")
        (self.root / "profile.json").write_bytes((PACKAGE / "backend/fixtures/compute_profile.json").read_bytes())
        base["feedback"]["compute_profile_path"] = "profile.json"
        base["runtime"]["workload_path"] = str(PACKAGE / "backend/fixtures/workload.json")
        (self.root / "base.json").write_text(json.dumps(base))
        generator = read_json(PACKAGE / "configs/workload_example.json")
        generator["arrival"].update(pattern="periodic", count=3, rate_rps=300)
        (self.root / "generator.json").write_text(json.dumps(generator))
        self.spec = {"schema_version": 1, "base_config": "base.json", "workload_spec": "generator.json",
                     "seeds": [7, 19], "sweep": {"topology.nic_bandwidth_gbps": [100, 200]}}
        self.path = self.root / "suite.json"
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.spec))
        return self.path

    def test_dry_run_is_portable_and_has_no_output_side_effect(self):
        plan = plan_suite(self.path)
        self.assertEqual(len(plan["runs"]), 4)
        self.assertEqual(plan["runs"][0]["workload_seed"], 7)
        self.assertEqual(plan["runs"][2]["workload_seed"], 7)
        output = self.root / "dry-output"
        process = subprocess.run([sys.executable, "-m", "joint_scheduling", "--config", "missing.json",
                                  "run-experiments", "--spec", str(self.path), "--output", str(output), "--dry-run"],
                                 cwd=PACKAGE.parent, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["total_runs"], 4)
        self.assertFalse(output.exists())

    def test_rejects_inactive_policy_sweeps_and_invalid_matrix(self):
        for sweep in ({"binding.policy": ["round_robin"]}, {"feedback.scheduling_overhead_us": [0, 10]},
                      {"topology.nic_bandwidth_gbps": []}, {"topology.nic_bandwidth_gbps": [100, 100]}):
            self.spec["sweep"] = sweep
            with self.subTest(sweep=sweep), self.assertRaises(ValueError):
                plan_suite(self.save())
        self.spec["sweep"] = {}
        self.spec["seeds"] = [7, 7]
        with self.assertRaises(ValueError):
            plan_suite(self.save())

    def test_fail_fast_records_failure_and_skipped_cases_without_zero_metrics(self):
        self.spec.update(seeds=[7], sweep={"topology.nic_bandwidth_gbps": [0, 200]})
        report = run_suite(self.save(), self.root / "failed", fail_fast=True)
        self.assertEqual((report["failed_runs"], report["skipped_runs"]), (1, 1))
        self.assertIsNone(report["cases"][0]["metrics"]["slo_goodput_rps"]["mean"])
        rows = [json.loads(line) for line in (self.root / "failed/runs.jsonl").read_text().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["failed", "skipped"])
        self.assertTrue((self.root / "failed/runs/case-0000-seed-7/error.json").is_file())
        with self.assertRaises(FileExistsError):
            run_suite(self.path, self.root / "failed")

    @unittest.skipUnless(ready(), "build ns-3 to run experiment matrices")
    def test_real_matrix_pairing_paths_and_aggregate_samples(self):
        report = run_suite(self.path, self.root / "success")
        self.assertEqual(report["successful_runs"], 4)
        self.assertEqual(report["failed_runs"], 0)
        rows = [json.loads(line) for line in (self.root / "success/runs.jsonl").read_text().splitlines()]
        for seed in (7, 19):
            paired = [row for row in rows if row["seed"] == seed]
            self.assertEqual(paired[0]["workload_sha256"], paired[1]["workload_sha256"])
        for row in rows:
            self.assertEqual(row["completed_count"], 3)
            self.assertTrue((self.root / "success" / row["output_dir"] / "run/observation/state.csv").is_file())
            self.assertEqual(row["effective_baseline"]["endpoint_selection"], "round_robin")
        for group in report["cases"]:
            self.assertEqual(group["metrics"]["completion_throughput_rps"]["samples"], 2)
            self.assertIsNotNone(group["metrics"]["completion_throughput_rps"]["sample_stddev"])
        self.assertTrue((self.root / "success/comparison.csv").is_file())

    @unittest.skipUnless(ready(), "build ns-3 to run experiment matrices")
    def test_failed_case_does_not_stop_next_case_and_cli_returns_nonzero(self):
        self.spec.update(seeds=[7], sweep={"topology.nic_bandwidth_gbps": [0, 200]})
        process = subprocess.run([sys.executable, "-m", "joint_scheduling", "run-experiments", "--spec", str(self.save()),
                                  "--output", str(self.root / "mixed")], cwd=PACKAGE.parent, capture_output=True, text=True)
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        report = json.loads((self.root / "mixed/summary.json").read_text())
        self.assertEqual((report["successful_runs"], report["failed_runs"]), (1, 1))

    @unittest.skipUnless(ready(), "build ns-3 to run experiment matrices")
    def test_suite_freezes_inputs_before_running(self):
        self.spec["seeds"] = [7]
        original = (self.root / "profile.json").read_bytes()
        report = run_suite(self.save(), self.root / "frozen", progress=lambda row: (
            (self.root / "profile.json").write_text("changed externally after the first run")))
        self.assertEqual(report["successful_runs"], 2)
        self.assertEqual((self.root / "frozen/inputs/compute_profile.json").read_bytes(), original)
