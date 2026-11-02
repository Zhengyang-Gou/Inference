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

    def test_duration_keeps_pressure_until_same_end_across_rates(self):
        self.spec["arrival"] = {"pattern": "periodic", "duration_seconds": 0.1, "start_seconds": 1, "rate_rps": 100}
        first = generate_workload(self.spec)["requests"]
        self.assertEqual(len(first), 10)
        self.assertEqual(first[0]["arrival_seconds"], 1)
        self.assertEqual(first[-1]["arrival_seconds"], 1.09)
        self.spec["arrival"]["rate_rps"] = 200
        second = generate_workload(self.spec)["requests"]
        self.assertEqual(len(second), 20)
        self.assertEqual(second[-1]["arrival_seconds"], 1.095)
        for left, right in zip(first, second):
            self.assertEqual({k:v for k,v in left.items() if k != "arrival_seconds"},
                             {k:v for k,v in right.items() if k != "arrival_seconds"})
        self.spec["arrival"]["pattern"] = "poisson"
        a = generate_workload(self.spec)
        self.assertEqual(a, generate_workload(self.spec))
        self.assertTrue(all(1 <= r["arrival_seconds"] < 1.1 for r in a["requests"]))

    def test_duration_rejects_ambiguous_invalid_and_unbounded_inputs(self):
        for arrival in ({"pattern":"periodic","count":1,"duration_seconds":1,"rate_rps":10},
                        {"pattern":"periodic","rate_rps":10},
                        {"pattern":"periodic","duration_seconds":0,"rate_rps":10},
                        {"pattern":"poisson","duration_seconds":float('inf'),"rate_rps":10},
                        {"pattern":"burst","duration_seconds":1,"burst_size":2,"burst_interval_seconds":0.1}):
            with self.subTest(arrival=arrival), self.assertRaises(ValueError):
                generate_workload(dict(self.spec, arrival=arrival))
        # An arrival rounded to the duration endpoint is outside the interval.
        self.spec["arrival"] = {"pattern":"periodic", "duration_seconds":0.000000001, "rate_rps":1e9}
        self.assertEqual(len(generate_workload(self.spec)["requests"]), 1)

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

    def test_prefix_blocks_are_preserved_and_invalid_lengths_rejected(self):
        self.spec["classes"] = [dict(name="shared", weight=1, input_tokens=128, output_tokens=3,
                                     prefix_blocks=[dict(id="system", tokens=64)])]
        path = self.output / "prefix.json"
        generated = write_workload(self.spec, path)
        loaded, _ = load_workload(path)
        self.assertEqual(loaded, generated["requests"])
        self.assertTrue(all(r["prefix_blocks"] == [dict(id="system", tokens=64)] for r in loaded))
        self.spec["classes"][0]["prefix_blocks"][0]["tokens"] = 129
        with self.assertRaises(ValueError):
            write_workload(self.spec, self.output / "bad-prefix.json")
        self.assertFalse((self.output / "bad-prefix.json").exists())

    def test_declared_output_limit_is_preserved_and_validated(self):
        self.spec["classes"] = [dict(name="bounded", weight=1, input_tokens=128, output_tokens=3, max_output_tokens=8)]
        result = generate_workload(self.spec)
        self.assertTrue(all(r["max_output_tokens"] == 8 for r in result["requests"]))
        self.spec["classes"][0]["max_output_tokens"] = 2
        with self.assertRaises(ValueError):
            generate_workload(self.spec)

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
