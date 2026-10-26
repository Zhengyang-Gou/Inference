import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.backend.runtime import load_workload, read_json
from joint_scheduling.workload import generate_workload, write_workload


ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "joint_scheduling/configs/workload_example.json"


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.spec = read_json(SPEC)

    def test_reproducible_and_arrival_changes_preserve_request_mix(self):
        original = copy.deepcopy(self.spec)
        first = generate_workload(self.spec)
        self.assertEqual(first, generate_workload(self.spec))
        self.assertEqual(self.spec, original)
        changed = copy.deepcopy(self.spec)
        changed["arrival"]["rate_rps"] *= 2
        second = generate_workload(changed)
        for left, right in zip(first["requests"], second["requests"]):
            self.assertEqual({k: v for k, v in left.items() if k != "arrival_seconds"},
                             {k: v for k, v in right.items() if k != "arrival_seconds"})
            self.assertAlmostEqual(left["arrival_seconds"] / 2, right["arrival_seconds"], places=8)
        self.assertNotEqual(first["requests"], generate_workload(self.spec, seed=19)["requests"])

    def test_periodic_and_burst_timing(self):
        self.spec["arrival"] = {"pattern": "periodic", "count": 4, "start_seconds": 1, "rate_rps": 2}
        self.assertEqual([r["arrival_seconds"] for r in generate_workload(self.spec)["requests"]], [1, 1.5, 2, 2.5])
        self.spec["arrival"] = {"pattern": "burst", "count": 7, "burst_size": 3,
                                "burst_interval_seconds": 0.01, "spread_seconds": 0}
        self.assertEqual([r["arrival_seconds"] for r in generate_workload(self.spec)["requests"]],
                         [0, 0, 0, 0.01, 0.01, 0.01, 0.02])

    def test_class_lengths_slos_and_native_loader_compatibility(self):
        self.spec["arrival"]["count"] = 100
        rows = generate_workload(self.spec)["requests"]
        for row in rows:
            if row["input_tokens"] <= 128:
                self.assertIn(row["output_tokens"], (2, 3, 4))
                self.assertEqual(row["ttft_slo_seconds"], 0.02)
            else:
                self.assertTrue(256 <= row["input_tokens"] <= 512)
                self.assertEqual(row["output_tokens"], 4)
                self.assertEqual(row["ttft_slo_seconds"], 0.03)
        path = self.output / "requests.json"
        generated = write_workload(self.spec, path)
        loaded, provenance = load_workload(path)
        self.assertEqual(rows, loaded)
        self.assertTrue(provenance["synthetic"])
        self.assertEqual(sum(generated["provenance"]["class_counts"].values()), 100)
        with self.assertRaises(FileExistsError):
            write_workload(self.spec, path)

    def test_invalid_specs_do_not_create_output(self):
        cases = []
        for field, value in (("count", -1), ("count", True), ("rate_rps", 0), ("rate_rps", float("nan"))):
            bad = copy.deepcopy(self.spec)
            bad["arrival"][field] = value
            cases.append(bad)
        for token_spec in (0, {"type": "uniform", "min": 10, "max": 2},
                           {"type": "choice", "values": [1, 2], "weights": [1]}):
            bad = copy.deepcopy(self.spec)
            bad["classes"][0]["input_tokens"] = token_spec
            cases.append(bad)
        bad = copy.deepcopy(self.spec)
        bad["classes"][0]["weight"] = 0
        cases.append(bad)
        for bad in cases:
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                write_workload(bad, self.output / "invalid.json")
        self.assertFalse((self.output / "invalid.json").exists())

    def test_empty_workload_and_cli_ignore_unrelated_config(self):
        self.spec["arrival"]["count"] = 0
        self.assertEqual(generate_workload(self.spec)["requests"], [])
        path = self.output / "from-cli.json"
        process = subprocess.run([sys.executable, "-m", "joint_scheduling", "--config", "missing.json",
                                  "generate-workload", "--spec", str(SPEC), "--seed", "19", "--output", str(path)],
                                 cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(path.read_text())["provenance"]["seed"], 19)
