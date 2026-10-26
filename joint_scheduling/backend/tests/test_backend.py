"""Numerical/capacity and real-network regression tests for the backend adapter."""

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.config import load_config
from joint_scheduling.feedback import NetworkTransfer
from joint_scheduling.reservations import PhysicalObjectReservation as Object
from joint_scheduling.reservations.ledger import CapacityLedger


def backend_available():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


class LedgerTests(unittest.TestCase):
    def test_atomic_capacity_rejection(self):
        ledger = CapacityLedger({"a": 100, "b": 100})
        self.assertTrue(ledger.try_commit("one", [Object("x", "a", 80, ("r",))]))
        before = ledger.snapshot()
        self.assertFalse(ledger.try_commit("two", [Object("y", "a", 30, ("s",)), Object("z", "b", 20, ("s",))]))
        self.assertEqual(ledger.snapshot(), before)
        self.assertNotIn(("b", "z"), ledger.objects)

    def test_shared_object_conversion_and_read_pins(self):
        ledger = CapacityLedger({"gpu": 100})
        ledger.try_commit("a", [Object("prefix", "gpu", 60, ("r0",))])
        ledger.try_commit("b", [Object("prefix", "gpu", 60, ("r1",))])
        self.assertEqual(ledger.snapshot()["gpu"]["reserved_bytes"], 60)
        ledger.materialize("gpu", "prefix", 40)
        self.assertEqual(ledger.snapshot()["gpu"]["available_bytes"], 40)
        self.assertEqual(ledger.snapshot()["gpu"]["occupied_bytes"], 40)
        self.assertEqual(ledger.snapshot()["gpu"]["reserved_bytes"], 20)
        ledger.pin("gpu", "prefix")
        ledger.release_request("r0")
        ledger.release_request("r1")
        self.assertEqual(ledger.snapshot()["gpu"]["available_bytes"], 40)
        ledger.unpin("gpu", "prefix")
        self.assertEqual(ledger.snapshot()["gpu"]["available_bytes"], 100)

    def test_idempotence_and_materialization_bounds(self):
        ledger = CapacityLedger({"gpu": 100})
        plan = [Object("x", "gpu", 60, ("r",))]
        self.assertTrue(ledger.try_commit("a", plan))
        self.assertTrue(ledger.try_commit("a", plan))
        self.assertEqual(ledger.snapshot()["gpu"]["reserved_bytes"], 60)
        with self.assertRaises(ValueError):
            ledger.try_commit("a", [Object("x", "gpu", 61, ("r",))])
        with self.assertRaises(ValueError):
            ledger.materialize("gpu", "x", 61)
        self.assertEqual(ledger.snapshot()["gpu"]["occupied_bytes"], 0)


class ProfileTests(unittest.TestCase):
    def test_interpolation_and_no_silent_extrapolation(self):
        profile = ComputeProfile(PACKAGE / "backend" / "fixtures" / "compute_profile.json")
        point = profile.lookup("prefill", 320, 4)
        self.assertAlmostEqual(point["compute_seconds"], 0.0019)
        self.assertEqual(point["collective_bytes"], 40960)
        with self.assertRaises(ValueError):
            profile.lookup("prefill", 100000, 4)
        with self.assertRaises(ValueError):
            profile.lookup("decode", 100, 8)


@unittest.skipUnless(backend_available(), "build the pinned ns-3 backend to run integration tests")
class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(PACKAGE / "configs" / "baseline_smoke.json")

    def test_actual_network_clock_and_completion(self):
        from joint_scheduling.backend.client import Ns3Backend
        with Ns3Backend(self.config.topology, self.root / "network") as backend:
            events = []
            backend.call_at(0, lambda: events.append(("zero", backend.now_seconds())))
            backend.run_until(0)
            self.assertEqual(events, [("zero", 0)])
            backend.call_at(0.000001, lambda: events.append(("timer", backend.now_seconds())))
            backend.submit(NetworkTransfer("kv", "kv", 0, 8, 2 * 1024 * 1024),
                           lambda progress: events.append(("flow", progress)))
            backend.run_until(0.00001)
            self.assertEqual(len(events), 2)
            backend.run_until(0.01)
            self.assertEqual(len(events), 3)
            progress = events[-1][1]
            self.assertTrue(progress.source_reads_complete and progress.destination_ready)
            self.assertGreater(progress.time_seconds, 2 * 1024 * 1024 * 8 / (200 * 1e9))
            self.assertEqual(backend.now_seconds(), 0.01)
            with self.assertRaises(BackendError):
                backend.call_at(0, lambda: None)
            with self.assertRaises(BackendError):
                backend.send_hosts(-1, 0, 100, lambda: None)

    def test_entry_point_preserves_caller_relative_paths(self):
        command = [str(PACKAGE / "jsim"), "--config", str(PACKAGE / "configs" / "baseline_smoke.json"),
                   "run-baseline", "--output", "relative-run"]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "relative-run" / "result" / "summary.json").is_file())
        doctor = subprocess.run([str(PACKAGE / "jsim"), "--config", "missing.json", "backend", "doctor"],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)

    def test_local_link_is_shared_and_independent_of_network_clock(self):
        from joint_scheduling.backend.client import Ns3Backend
        topology = replace(self.config.topology, intra_node_bandwidth_gbps=1.0, intra_node_latency_us=0)
        with Ns3Backend(topology, self.root / "network") as backend:
            times = []
            backend.send_hosts(0, 0, 1000000, lambda: times.append(backend.now_seconds()))
            backend.send_hosts(0, 0, 1000000, lambda: times.append(backend.now_seconds()))
            backend.run_until(0.02)
            self.assertAlmostEqual(times[0], 0.008)
            self.assertAlmostEqual(times[1], 0.016)

    def test_baseline_events_capacity_and_final_release(self):
        from joint_scheduling.backend.runner import run_baseline
        workload = {"schema_version": 1, "provenance": {"synthetic": True},
                    "requests": [{"request_id": "r{}".format(i), "arrival_seconds": 0,
                                  "input_tokens": 128, "output_tokens": 2} for i in range(3)]}
        input_file = self.root / "workload.json"
        input_file.write_text(json.dumps(workload))
        weights = 1073741824
        topology = replace(self.config.topology,
                           gpu_memory_gib=(weights + 200000) / 0.95 / 2**30)
        config = replace(self.config, topology=topology)
        output = self.root / "baseline"
        result = run_baseline(config, output, workload_path=input_file)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 3)
        events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        ready = {event["request_id"]: event["time_seconds"] for event in events if event["event"] == "kv_ready"}
        for token in result.tokens:
            if token["token_index"] == 2:
                self.assertGreaterEqual(token["returned_at_seconds"], ready[token["request_id"]])
        for event in events:
            if event["event"] == "resources":
                for domain in event["domains"].values():
                    self.assertLessEqual(domain["occupied_bytes"] + domain["reserved_bytes"], domain["capacity_bytes"])
        final = json.loads((output / "final_resources.json").read_text())
        self.assertTrue(all(row["occupied_bytes"] == row["reserved_bytes"] == 0 for row in final.values()))


if __name__ == "__main__":
    unittest.main()
