"""Layer timing, byte conservation and lifetime checks on the real network."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.runtime import ComputeProfile, IMMEDIATE_KV_MODES
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config
from joint_scheduling.kv_scheduler import split_layer_kv


def backend_available():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


class LayerInputTests(unittest.TestCase):
    def test_uneven_shards_and_offsets_conserve_every_byte(self):
        blocks = split_layer_kv("r", 2, (0, 1, 2), (3, 4, 5), 11, 2, 0.001)
        self.assertEqual(sum(block.size_bytes for block in blocks), 11)
        self.assertEqual(len({block.block_id for block in blocks}), len(blocks))
        for gpu, expected in ((0, 4), (1, 4), (2, 3)):
            shard = [block for block in blocks if block.source_gpu_id == gpu]
            self.assertEqual(sum(block.size_bytes for block in shard), expected)
            self.assertEqual([block.offset_bytes for block in shard], [0, 2])
            self.assertTrue(all(0 < block.size_bytes <= 2 for block in shard))
        self.assertEqual(split_layer_kv("r", 0, (0,), (1,), 0, 2, 0), ())
        with self.assertRaises(ValueError):
            split_layer_kv("r", 0, (0,), (1, 2), 10, 2, 0)

    def test_layer_profile_interpolates_and_rejects_inconsistent_inputs(self):
        path = PACKAGE / "backend/fixtures/layered_compute_profile.json"
        profile = ComputeProfile(path)
        steps = profile.prefill_steps(320, 4)
        self.assertEqual(len(steps), 4)
        self.assertAlmostEqual(sum(step["compute_seconds"] for step in steps), 0.0019)
        self.assertEqual(sum(step["kv_bytes_per_token"] for step in steps), profile.kv_bytes_per_token)
        with self.assertRaises(ValueError):
            profile.prefill_steps(4097, 4)
        with self.assertRaises(ValueError):
            profile.prefill_steps(128, 2)
        raw = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "profile.json"
            for mutation in ("bytes", "missing", "schema"):
                broken = copy.deepcopy(raw)
                if mutation == "bytes":
                    broken["kv_bytes_per_token"] += 1
                elif mutation == "missing":
                    broken["phases"]["prefill"]["layers"][0]["points"] = []
                else:
                    broken["schema_version"] = 1
                target.write_text(json.dumps(broken))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    ComputeProfile(target)


@unittest.skipUnless(backend_available(), "build ns-3 to run layer-wise integration tests")
class LayerNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(PACKAGE / "configs/layered_smoke.json")
        self.config = replace(self.config, recording=replace(self.config.recording, mode="debug"))

    def workload(self, requests):
        path = self.root / "workload.json"
        path.write_text(json.dumps({"schema_version": 1, "provenance": {"synthetic": True}, "requests": requests}))
        return path

    def test_three_modes_share_work_and_obey_dependencies(self):
        workload = self.workload([{"request_id": "r", "arrival_seconds": 0, "input_tokens": 128, "output_tokens": 3},
                                  {"request_id": "one", "arrival_seconds": 0.01, "input_tokens": 128, "output_tokens": 1}])
        signatures = []
        chunk_signatures = []
        for mode in IMMEDIATE_KV_MODES:
            output = self.root / mode
            result = run_baseline(self.config, output, workload_path=workload, kv_mode=mode)
            self.assertEqual(result.summary["completion_window"]["completed_count"], 2)
            events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            layers = {e["layer_id"]: e["time_seconds"] for e in events if e["event"] == "layer_ready" and e["request_id"] == "r"}
            prefill_end = next(t["returned_at_seconds"] for t in result.tokens if t["request_id"] == "r" and t["token_index"] == 1)
            kv = [e for e in events if e["event"] == "transfer_started" and e["transfer_kind"] == "kv"]
            self.assertTrue(all(e["request_id"] == "r" for e in kv), "one-token requests must not transfer KV")
            self.assertEqual(sum(e["size_bytes"] for e in kv), 128 * 32768)
            if mode == "layer_ready_immediate":
                self.assertTrue(any(e["time_seconds"] < prefill_end for e in kv))
            else:
                self.assertTrue(all(e["time_seconds"] >= prefill_end for e in kv))
            if mode != "whole_ready_immediate":
                self.assertTrue(all(e["size_bytes"] <= self.config.kv_scheduler.max_block_bytes for e in kv))
                self.assertTrue(all(e["time_seconds"] >= layers[e["layer_id"]] for e in kv))
                chunk_signatures.append([(e["block_id"], e["source_gpu_id"], e["destination_gpu_id"], e["size_bytes"]) for e in kv])
            kv_end = max(e["time_seconds"] for e in events if e["event"] == "transfer_completed" and e["transfer_kind"] == "kv")
            decode_start = next(e["time_seconds"] for e in events if e["event"] == "compute_step_started" and e["phase"] == "decode")
            self.assertGreaterEqual(decode_start, max(kv_end, prefill_end))
            compute = [(e["request_id"], e["phase"], e.get("layer_id"), e["modeled_compute_seconds"])
                       for e in events if e["event"] == "compute_step_started"]
            collective_bytes = sum(e["size_bytes"] for e in events if e["event"] == "transfer_started" and e["transfer_kind"] == "collective")
            signatures.append((sorted(compute), collective_bytes))
            for event in events:
                if event["event"] == "resources":
                    for state in event["domains"].values():
                        self.assertLessEqual(state["occupied_bytes"] + state["reserved_bytes"], state["capacity_bytes"])
            resources = json.loads((output / "final_resources.json").read_text())
            self.assertTrue(all(r["occupied_bytes"] == r["reserved_bytes"] == 0 for r in resources.values()))
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(signatures[1], signatures[2])
        self.assertEqual(chunk_signatures[0], chunk_signatures[1])

    def test_partial_run_keeps_dependencies_and_deterministic_replay(self):
        workload = self.workload([{"request_id": "r", "arrival_seconds": 0, "input_tokens": 512, "output_tokens": 3}])
        raw = json.loads((PACKAGE / "backend/fixtures/layered_compute_profile.json").read_text())
        for layer in raw["phases"]["prefill"]["layers"]:
            layer["collectives_per_step"] = 0
        profile = self.root / "compute-only.json"
        profile.write_text(json.dumps(raw))
        config = replace(self.config, topology=replace(self.config.topology, nic_bandwidth_gbps=1),
                         feedback=replace(self.config.feedback, compute_profile_path=str(profile)),
                         runtime=replace(self.config.runtime, time_limit_seconds=0.004),
                         evaluation=replace(self.config.evaluation, window_end_seconds=0.004))
        for name in ("a", "b"):
            output = self.root / name
            result = run_baseline(config, output, workload_path=workload)
            self.assertEqual(result.summary["arrival_cohort"]["incomplete_count"], 1)
            self.assertEqual(len(result.tokens), 1, "prefill must have finished while KV is still in flight")
            resources = json.loads((output / "final_resources.json").read_text())
            for placement in config.topology.placements:
                if placement.role == "prefill" and placement.instance_id == "p0":
                    self.assertTrue(all(resources["gpu-{}".format(gpu)]["occupied_bytes"] > 0 for gpu in placement.gpu_ids))
        self.assertEqual((self.root / "a/events.jsonl").read_bytes(), (self.root / "b/events.jsonl").read_bytes())

    def test_layer_mode_requires_explicit_layer_data_before_creating_output(self):
        output = self.root / "invalid"
        config = load_config(PACKAGE / "configs/baseline_smoke.json")
        with self.assertRaises(ValueError):
            run_baseline(config, output, kv_mode="layer_ready_immediate")
        self.assertFalse(output.exists())

    def test_cli_selects_layer_mode(self):
        output = self.root / "cli"
        result = subprocess.run([sys.executable, "-m", "joint_scheduling", "--config", str(self.config.config_path),
                                 "run-baseline", "--kv-mode", "layer_ready_immediate", "--output", str(output)],
                                cwd=PACKAGE.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads((output / "resolved_config.json").read_text())
        self.assertEqual(snapshot["effective_baseline"]["kv"], "layer_ready_immediate")


if __name__ == "__main__":
    unittest.main()
