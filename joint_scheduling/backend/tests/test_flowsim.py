"""flowSim fluid backend: clock, completion, contention and observation contract.

Every completion time below is derived from the configured link bandwidth and
latency, not from a recorded run: 1 MiB over one 200 Gbps endpoint uplink is
41.94304 ms/1000 = 41.94304 us of serialisation, plus 1 us of propagation per
hop. The tests assert both the model's arithmetic and the adapter's contract
with the rest of the runtime (one callback per transfer, monotone time, progress
that advances with service).
"""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError
from joint_scheduling.backend.flow_build import doctor as flowsim_doctor
from joint_scheduling.backend.flow_client import FlowSimBackend
from joint_scheduling.config import load_config
from joint_scheduling.feedback import NetworkTransfer


def flowsim_available():
    return flowsim_doctor()["backend_ready"]


MIB = 1024 * 1024


def network_config():
    return load_config(PACKAGE / "configs" / "baseline_smoke.json")


def run_transfers(topology, transfers, deadline=0.05, timer=None, backend_class=FlowSimBackend, **options):
    """Submit logical transfers at t=0 and return (finish times, snapshot)."""
    finished = []
    with tempfile.TemporaryDirectory() as directory:
        with backend_class(topology, Path(directory) / "network", **options) as backend:
            if timer is not None:
                backend.call_at(timer, lambda: finished.append(("timer", backend.now_seconds())))
            with backend.submission_batch():
                for name, kind, source, destination, size, priority in transfers:
                    backend.submit(NetworkTransfer(name, kind, source, destination, size, priority),
                                   lambda progress, name=name: finished.append((name, backend.now_seconds(),
                                                                               progress.source_reads_complete,
                                                                               progress.destination_ready)))
            backend.run_until(deadline)
            snapshot = backend.network_snapshot()
            times = {name: seconds for name, seconds, *_ in finished if name != "timer"}
    return times, snapshot


@unittest.skipUnless(flowsim_available(), "flowSim backend not built")
class FluidTimingTests(unittest.TestCase):
    def setUp(self):
        self.topology = network_config().topology

    def test_single_flow_matches_bandwidth_plus_path_latency_and_conserves_bytes(self):
        times, snapshot = run_transfers(self.topology, [("a", "kv", 0, 12, MIB, 2)])
        # 8 * MiB bits over 200 Gbps = 41.94304 us, four hops of 1 us.
        self.assertAlmostEqual(times["a"], 41.94304e-6 + 4e-6, places=9)
        busy = {row["resource_id"]: row for row in snapshot["resources"]
                if row["resource_id"].startswith("link:") and row["tx_bytes"]}
        self.assertEqual(len(busy), 4, "one path segment was not accounted")
        for row in busy.values():
            self.assertEqual(row["tx_bytes"], MIB)
            self.assertEqual(row["kv_bytes"], MIB)
            self.assertEqual(row["other_bytes"], 0)
            self.assertEqual(row["collective_bytes"], 0)
        self.assertEqual(snapshot["flows"], [])

    def test_completion_fires_once_and_time_is_monotone(self):
        finished = []
        with tempfile.TemporaryDirectory() as directory:
            with FlowSimBackend(self.topology, Path(directory) / "network") as backend:
                backend.submit(NetworkTransfer("a", "kv", 0, 12, MIB),
                               lambda progress: finished.append(backend.now_seconds()))
                backend.run_until(0.01)
                for _ in range(3):
                    backend.run_until(0.02)
        self.assertEqual(len(finished), 1)
        self.assertLessEqual(finished[0], 0.01)

    def test_zero_delay_timer_fires_at_current_instant(self):
        fired = []
        with tempfile.TemporaryDirectory() as directory:
            with FlowSimBackend(self.topology, Path(directory) / "network") as backend:
                backend.call_at(0.0, lambda: fired.append(backend.now_seconds()))
                backend.advance(0.0)
        self.assertEqual(fired, [0.0])


@unittest.skipUnless(flowsim_available(), "flowSim backend not built")
class ContentionTests(unittest.TestCase):
    def setUp(self):
        self.topology = network_config().topology
        self.solo = 41.94304e-6 + 4e-6

    def test_shared_sender_port_halves_each_flow(self):
        times, _ = run_transfers(self.topology, [("a", "kv", 0, 12, MIB, 2), ("b", "kv", 1, 13, MIB, 2)])
        # GPUs 0/1 share host 0's uplink and 12/13 share host 6's downlink; the
        # path latency is paid once, only the serialisation term doubles.
        for name in ("a", "b"):
            self.assertAlmostEqual(times[name], 2 * 41.94304e-6 + 4e-6, delta=2e-6)

    def test_shared_receiver_halves_both_sources(self):
        times, _ = run_transfers(self.topology, [("a", "kv", 0, 12, MIB, 2), ("b", "kv", 2, 12, MIB, 2)])
        for name in ("a", "b"):
            self.assertAlmostEqual(times[name], 2 * 41.94304e-6 + 4e-6, delta=2e-6)

    def test_independent_pairs_run_in_parallel(self):
        times, _ = run_transfers(self.topology, [("a", "kv", 0, 12, MIB, 2), ("b", "kv", 2, 14, MIB, 2)])
        for name in ("a", "b"):
            self.assertAlmostEqual(times[name], self.solo, delta=2e-6)

    def test_midflow_arrival_reallocates_and_replaces_the_old_completion(self):
        times = {}
        with tempfile.TemporaryDirectory() as directory:
            with FlowSimBackend(self.topology, Path(directory) / "network") as backend:
                backend.submit(NetworkTransfer("a", "kv", 0, 12, 2 * MIB),
                               lambda progress: times.setdefault("a", backend.now_seconds()))
                backend.run_until(10e-6)
                self.assertNotIn("a", times, "the first flow should still be running")
                backend.submit(NetworkTransfer("b", "kv", 2, 12, 2 * MIB),
                               lambda progress: times.setdefault("b", backend.now_seconds()))
                backend.run_until(0.05)
        solo_two_mib = 2 * 41.94304e-6 + 4e-6
        # Both flows share host 6's downlink from the second arrival onward, so
        # neither may keep the uncontended finish time.
        for name in ("a", "b"):
            self.assertGreater(times[name], solo_two_mib * 1.5)
            self.assertLess(times[name], 4 * solo_two_mib)

    def test_inflight_progress_is_visible_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            with FlowSimBackend(self.topology, Path(directory) / "network") as backend:
                backend.submit(NetworkTransfer("a", "kv", 0, 12, 4 * MIB), lambda progress: None)
                start = backend.network_snapshot()
                self.assertEqual(start["flows"][0]["acked_bytes"], 0)
                backend.run_until(20e-6)
                middle = backend.network_snapshot()
                self.assertGreater(middle["flows"][0]["acked_bytes"], 0)
                self.assertLess(middle["flows"][0]["acked_bytes"], 4 * MIB)
                self.assertEqual(middle["time_ns"], backend.now_ns)
                self.assertEqual(middle, backend.network_snapshot(),
                                 "reading telemetry must not advance the clock")
                backend.run_until(0.05)


@unittest.skipUnless(flowsim_available(), "flowSim backend not built")
class LocalFabricTests(unittest.TestCase):
    def setUp(self):
        self.topology = replace(network_config().topology, intra_node_model="gpu_ports",
                                intra_node_bandwidth_gbps=1600.0, intra_node_latency_us=0.5)

    def test_local_gpu_ports_serialise_on_the_shared_port(self):
        # 8 MiB at 1600 Gbps = 41.94304 us of service (rounded up to 1 ns).
        service = 41.94304e-6
        times, snapshot = run_transfers(self.topology, [("a", "kv", 0, 1, 8 * MIB, 2),
                                                        ("b", "kv", 0, 1, 8 * MIB, 2)])
        self.assertAlmostEqual(times["a"], service + 0.5e-6, delta=2e-6)
        self.assertAlmostEqual(times["b"], 2 * service + 0.5e-6, delta=2e-6)
        ports = {row["resource_id"]: row for row in snapshot["resources"]
                 if row["resource_id"].startswith("local:")}
        self.assertIn("local:gpu-0:tx", ports)
        self.assertGreaterEqual(ports["local:gpu-0:tx"]["tx_bytes"], 8 * MIB)

    def test_opposite_directions_use_independent_ports(self):
        service = 41.94304e-6
        times, _ = run_transfers(self.topology, [("a", "kv", 0, 1, 8 * MIB, 2),
                                                 ("b", "kv", 1, 0, 8 * MIB, 2)])
        for name in ("a", "b"):
            self.assertAlmostEqual(times[name], service + 0.5e-6, delta=2e-6)

    def test_shared_node_fifo_serialises_whole_host(self):
        topology = replace(network_config().topology, intra_node_model="shared_fifo",
                           intra_node_bandwidth_gbps=1600.0, intra_node_latency_us=0.5)
        service = 41.94304e-6
        times, snapshot = run_transfers(topology, [("a", "kv", 0, 1, 8 * MIB, 2),
                                                   ("b", "kv", 1, 0, 8 * MIB, 2)])
        self.assertAlmostEqual(times["b"] - times["a"], service, delta=2e-6)
        self.assertIn("local:0", {row["resource_id"] for row in snapshot["resources"]})


@unittest.skipUnless(flowsim_available(), "flowSim backend not built")
class ContractTests(unittest.TestCase):
    def setUp(self):
        self.topology = network_config().topology

    def test_priority_is_recorded_but_never_reorders_bandwidth(self):
        for order in ((("a", "collective", 0, 12, MIB, 1), ("b", "kv", 2, 12, MIB, 2)),
                      (("b", "kv", 2, 12, MIB, 2), ("a", "collective", 0, 12, MIB, 1))):
            times, snapshot = run_transfers(self.topology, list(order))
            self.assertAlmostEqual(abs(times["a"] - times["b"]), 0, delta=2e-6,
                                   msg="equal-share model must not favour a priority class")
        priorities = {row["resource_id"]: row["data_priority_bytes"]
                      for row in snapshot["resources"] if row["resource_id"].startswith("link:")}
        self.assertTrue(any(sum(values) == 2 * MIB for values in priorities.values()))

    def test_priority_flag_is_rejected_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(BackendError, "strict priority"):
            FlowSimBackend(self.topology, Path(directory) / "network", network_priority_enabled=True)

    def test_rejects_invalid_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            with FlowSimBackend(self.topology, Path(directory) / "network") as backend:
                backend.submit(NetworkTransfer("a", "kv", 0, 12, MIB), lambda progress: None)
                for transfer in (NetworkTransfer("a", "kv", 0, 12, MIB),
                                 NetworkTransfer("b", "unknown", 0, 12, MIB),
                                 NetworkTransfer("c", "kv", 0, 99, MIB),
                                 NetworkTransfer("d", "kv", 0, 12, 0),
                                 NetworkTransfer("e", "kv", 0, 12, MIB, 9)):
                    with self.assertRaises(BackendError):
                        backend.submit(transfer, lambda progress: None)
                with self.assertRaises(BackendError):
                    backend.call_at(backend.now_seconds() - 1e-6, lambda: None)
                backend.run_until(0.01)

    def test_do_not_require_the_ns3_backend(self):
        from joint_scheduling.backend import build
        original = build.require_backend
        build.require_backend = lambda: (_ for _ in ()).throw(AssertionError("ns-3 must not be required"))
        try:
            times, _ = run_transfers(self.topology, [("a", "kv", 0, 12, MIB, 2)])
        finally:
            build.require_backend = original
        self.assertIn("a", times)

    def test_same_inputs_and_seed_reproduce(self):
        transfers = [("a", "kv", 0, 12, MIB, 2), ("b", "kv", 2, 14, 2 * MIB, 2), ("c", "kv", 4, 12, MIB, 2)]
        first, _ = run_transfers(self.topology, transfers)
        second, _ = run_transfers(self.topology, transfers)
        self.assertEqual(first, second)

    def test_metadata_declares_the_model_and_its_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            working = Path(directory) / "network"
            with FlowSimBackend(self.topology, working) as backend:
                metadata = backend.metadata
            self.assertTrue((working / "flowsim_routes.json").is_file())
        self.assertEqual(metadata["backend"], "simai_flowsim_fluid")
        self.assertEqual(metadata["completion_semantics"], "all_bytes_serialized_at_destination_no_ack")
        self.assertEqual(metadata["queue_model"], "none")
        self.assertEqual(metadata["pfc"], "not_modeled")
        self.assertEqual(metadata["congestion_control"], "not_modeled")
        self.assertEqual(metadata["seed"], 7)


class ConfigurationTests(unittest.TestCase):
    def test_default_pair_is_unchanged(self):
        config = network_config()
        self.assertEqual((config.feedback.time_authority, config.feedback.network_backend),
                         ("ns3", "ns3_alibabacloud"))

    def test_mixed_pairs_are_rejected(self):
        from joint_scheduling.config import load_config
        from joint_scheduling.validation import ConfigError
        raw = json.loads((PACKAGE / "configs" / "baseline_smoke.json").read_text())
        for authority, backend in (("ns3", "flowsim"), ("flowsim", "ns3_alibabacloud")):
            with tempfile.TemporaryDirectory() as directory:
                value = json.loads(json.dumps(raw))
                value["feedback"]["time_authority"] = authority
                value["feedback"]["network_backend"] = backend
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(value))
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_unknown_backend_name_is_rejected(self):
        from joint_scheduling.config import load_config
        from joint_scheduling.validation import ConfigError
        raw = json.loads((PACKAGE / "configs" / "baseline_smoke.json").read_text())
        raw["feedback"]["network_backend"] = "flowsim_magic"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
