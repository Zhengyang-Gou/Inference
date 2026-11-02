"""Conservation, priority, delayed telemetry and real-network control checks."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.client import Ns3Backend
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config
from joint_scheduling.feedback import NetworkTransfer
from joint_scheduling.kv_scheduler import KVBlock, KVConfig
from joint_scheduling.kv_scheduler.adaptive import AdaptiveKVPolicy, NetworkResources, RequestBudget
from joint_scheduling.observation.network import NetworkObserver
from joint_scheduling.topology import InstancePlacement


def available():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


def budget(arrival, slack):
    return RequestBudget(arrival, arrival+1, arrival+0.1, arrival+0.5, 100, 0.1, slack, slack, True)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        base = load_config(PACKAGE / "configs/layered_smoke.json")
        base = replace(base, recording=replace(base.recording, mode="debug"))
        self.topology = replace(base.topology, num_leaves=1, nodes_per_leaf=2, gpus_per_node=1)
        self.ports = {}
        for src, dst in ((0, 2), (2, 0), (1, 2), (2, 1)):
            key = "{}:{}".format(src, dst)
            self.ports[key] = dict(node_id=src, peer_id=dst, capacity_bps=8000, queue_bytes=0,
                utilization=0, collective_bytes_per_second=0, other_bytes_per_second=0, kv_bytes_per_second=1000)
        self.resources = NetworkResources(self.topology, self.ports)
        self.config = KVConfig(max_block_bytes=100, max_concurrent_transfers=4, max_inflight_bytes=400,
            max_resource_inflight_bytes=200, safety_margin_fraction=0, collective_guard_us=0,
            pause_on_collective=True, inflight_budget_mode="fixed")
        self.network = dict(sequence=0, time_seconds=0, visible_at_seconds=0, resources=self.ports, flows=[])

    def policy(self, priority="slo"):
        result = AdaptiveKVPolicy(self.config, self.resources, priority)
        result.refresh(0, self.network, [])
        return result

    def block(self, name, request):
        return KVBlock(name, request, 0, 0, 1, 100, 0, None)

    def test_later_urgent_request_precedes_old_loose_request(self):
        policy = self.policy()
        policy.on_layer_ready([self.block("old", "old"), self.block("urgent", "urgent")])
        state = dict(network=self.network, collectives=[], requests={"old": budget(0, 0.8), "urgent": budget(0.01, 0.1)})
        self.assertEqual([p.block_id for p in policy.admit(0.1, state)], ["urgent"])
        policy.completed("urgent")
        self.assertEqual(policy.admit(0.1, state), (), "completion must not mint bandwidth credits")
        self.assertEqual([p.block_id for p in policy.admit(0.2, state)], ["old"])
        fifo = self.policy("fifo")
        fifo.on_layer_ready([self.block("old", "old"), self.block("urgent", "urgent")])
        self.assertEqual([p.block_id for p in fifo.admit(0.1, state)], ["old"])

    def test_collective_guard_discards_burst_and_wakes_after_guard(self):
        policy = self.policy()
        policy.on_layer_ready([self.block("b", "r")])
        state = dict(network=self.network, collectives=[dict(resources=self.resources.for_gpus(0, 1), starts_at_seconds=0.1)],
                     requests={"r": budget(0, 0.5)})
        self.assertEqual(policy.admit(0.1, state), ())
        self.assertIn("collective_protection", policy.last_decision["blocked"])
        self.assertGreater(policy.estimate_rate(self.resources.for_gpus(0, 1)[0]), 0)
        state["collectives"] = []
        self.assertEqual(policy.admit(0.2, state), (), "no retroactive credits during a collective guard")
        self.assertEqual(len(policy.admit(0.3, state)), 1)

    def test_shared_path_limits_and_dynamic_queue_feedback(self):
        policy = self.policy()
        policy.on_layer_ready([self.block(str(i), "r") for i in range(3)])
        state = dict(network=self.network, collectives=[], requests={"r": budget(0, 0.5)})
        self.assertEqual(len(policy.admit(0.1, state)), 1)
        self.assertEqual(len(policy.admit(0.2, state)), 1)
        self.assertEqual(policy.admit(0.3, state), ())
        self.assertIn("resource_inflight_limit", policy.last_decision["blocked"])
        policy.completed("0")
        self.assertEqual(len(policy.admit(0.3, state)), 1)
        for port in self.ports.values():
            port["queue_bytes"] = self.config.queue_target_bytes * 9
            port["collective_bytes_per_second"] = 200
        policy.refresh(0.3, self.network, [])
        key = self.resources.for_gpus(0, 1)[0]
        self.assertAlmostEqual(policy.states[key]["rate"], 80)
        with self.assertRaises(ValueError):
            policy.refresh(0.2, self.network, [])
        future = dict(self.network, visible_at_seconds=1)
        with self.assertRaises(ValueError):
            policy.refresh(0.3, future, [])

    def test_soft_collective_reservation_retains_residual_service(self):
        policy = AdaptiveKVPolicy(replace(self.config, pause_on_collective=False), self.resources)
        policy.refresh(0, self.network, [])
        policy.on_layer_ready([self.block("a", "r"), self.block("b", "r")])
        state = dict(network=self.network, collectives=[dict(resources=self.resources.for_gpus(0, 1), starts_at_seconds=0)],
                     requests={"r": budget(0, 0.5)})
        self.assertEqual(len(policy.admit(0.1, state)), 1)
        key = self.resources.for_gpus(0, 1)[0]
        self.assertAlmostEqual(policy.states[key]["rate"], 300)
        self.assertFalse(policy.states[key]["protected"])
        self.assertTrue(policy.states[key]["collective_reserved"])
        self.assertEqual(policy.admit(0.2, state), ())
        policy.completed("a")
        self.assertEqual(policy.admit(0.3, state), ())
        self.assertEqual(len(policy.admit(0.434, state)), 1)

    def adaptive_policy(self, capacity=1000, **settings):
        ports = {key: dict(port, capacity_bps=capacity*8) for key, port in self.ports.items()}
        resources = NetworkResources(self.topology, ports)
        config = replace(self.config, inflight_budget_mode="adaptive", pause_on_collective=False,
            max_resource_inflight_bytes=10000, max_inflight_bytes=10000, max_concurrent_transfers=100,
            control_interval_us=100000, collective_max_blocking_us=250000)
        config = replace(config, **settings)
        policy = AdaptiveKVPolicy(config, resources)
        network = dict(self.network, resources=ports)
        policy.refresh(0, network, [])
        policy.on_layer_ready([self.block(str(i), "r") for i in range(30)])
        state = dict(network=network, collectives=[], requests={"r": budget(0, 0.5)})
        return policy, state

    def test_adaptive_window_scales_with_bandwidth_during_collectives(self):
        counts = []
        for capacity in (1000, 10000):
            policy, state = self.adaptive_policy(capacity)
            state["collectives"] = [dict(resources=policy.resources.for_gpus(0, 1), starts_at_seconds=0)]
            counts.append(len(policy.admit(0.1, state)))
            for key in policy.resources.for_gpus(0, 1):
                s = policy.states[key]
                self.assertLessEqual(s["inflight_bytes"], s["collective_limit_bytes"])
        self.assertEqual(counts, [1, 3], "protection must allow multiple blocks at higher bandwidth")

    def test_completion_feedback_changes_window_without_refunding_tokens(self):
        policy, state = self.adaptive_policy()
        self.assertEqual(len(policy.admit(0.1, state)), 1)
        key = policy.resources.for_gpus(0, 1)[0]
        credits = policy.states[key]["tokens"]
        policy.completed("0", 0.5)
        self.assertEqual(policy.states[key]["tokens"], credits)
        self.assertAlmostEqual(policy.states[key]["completion_latency_seconds"], 0.4)
        self.assertEqual(len(policy.admit(0.5, state)), 4)
        self.assertEqual(policy.states[key]["inflight_limit_bytes"], 400)
        self.assertEqual(policy.summary["completion_samples"], 1)
        with self.assertRaises(ValueError):
            policy.completed("1", 0.4)

    def test_collective_tolerance_can_be_smaller_than_one_block_and_shrink(self):
        policy, state = self.adaptive_policy(10000, collective_max_blocking_us=1000)
        self.assertEqual(len(policy.admit(0.1, state)), 10)
        state["collectives"] = [dict(resources=policy.resources.for_gpus(0, 1), starts_at_seconds=0.1)]
        self.assertEqual(policy.admit(0.1, state), ())
        key = policy.resources.for_gpus(0, 1)[0]
        self.assertEqual(policy.states[key]["collective_limit_bytes"], 10)
        self.assertEqual(policy.states[key]["inflight_bytes"], 1000, "shrinking a window cannot cancel started transfers")
        for block_id in list(policy.inflight):
            policy.completed(block_id, 0.1)
        self.assertEqual(policy.admit(0.2, state), (), "never force one full block through a smaller budget")
        state["collectives"] = []
        self.assertGreater(len(policy.admit(0.3, state)), 0)

    def test_adaptive_window_honors_hard_cap_queue_and_missing_port(self):
        policy, state = self.adaptive_policy(100000)
        policy.config = replace(policy.config, max_resource_inflight_bytes=250)
        self.assertEqual(len(policy.admit(0.1, state)), 2)
        key = policy.resources.for_gpus(0, 1)[0]
        self.assertEqual(policy.states[key]["inflight_limit_bytes"], 250)
        state["network"]["resources"][key]["queue_bytes"] = policy.config.queue_target_bytes*9
        policy.refresh(0.1, state["network"], [])
        self.assertEqual(policy.states[key]["rate"], 10000)
        del state["network"]["resources"][key]
        policy.refresh(0.2, state["network"], [])
        self.assertEqual(policy.states[key]["inflight_limit_bytes"], 0)
        self.assertEqual(policy.admit(0.2, state), ())


@unittest.skipUnless(available(), "build ns-3 to run adaptive integration tests")
class AdaptiveNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = load_config(PACKAGE / "configs/layered_smoke.json")
        base = replace(base, recording=replace(base.recording, mode="debug"))
        self.base = base
        self.config = replace(base,
            topology=replace(base.topology, num_leaves=1, num_spines=1, nodes_per_leaf=2, gpus_per_node=1,
                nic_bandwidth_gbps=1, placements=(InstancePlacement("p0", "prefill", (0,)), InstancePlacement("d0", "decode", (1,)))),
            kv_scheduler=replace(base.kv_scheduler, max_block_bytes=16384, max_resource_inflight_bytes=16384,
                max_concurrent_transfers=4, collective_guard_us=0),
            runtime=replace(base.runtime, kv_transfer_mode="layer_ready_slo", time_limit_seconds=0.03),
            evaluation=replace(base.evaluation, window_end_seconds=0.03))
        profile = json.loads((PACKAGE / "backend/fixtures/layered_compute_profile.json").read_text())
        profile["kv_bytes_per_token"] = 262144
        profile["phases"]["prefill"] = {"tp_size": 1, "layers": [dict(kv_bytes_per_token=131072,
            collectives_per_step=0, points=[dict(tokens=1, compute_seconds=0.00001, collective_bytes=0)]) for _ in range(2)]}
        profile["phases"]["decode"] = dict(tp_size=1, collectives_per_step=0,
            points=[dict(tokens=i, compute_seconds=0.00001, collective_bytes=0) for i in (1, 4)])
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps(profile))

    def workload(self):
        path = self.root / "workload.json"
        requests = [dict(request_id="old", arrival_seconds=0, input_tokens=1, output_tokens=2, itl_slo_seconds=0.1),
                    dict(request_id="urgent", arrival_seconds=0.0006, input_tokens=1, output_tokens=2, itl_slo_seconds=0.006),
                    dict(request_id="future", arrival_seconds=0.02, input_tokens=1, output_tokens=2, itl_slo_seconds=0.1)]
        path.write_text(json.dumps(dict(schema_version=1, provenance={"synthetic": True}, requests=requests)))
        return path

    def test_packet_counters_classification_ack_and_delay(self):
        with Ns3Backend(self.base.topology, self.root / "network") as backend:
            before = backend.network_snapshot()
            backend.submit(NetworkTransfer("data", "kv", 0, 12, 2**20), lambda p: None)
            backend.submit(NetworkTransfer("cc", "collective", 2, 14, 2**20), lambda p: None)
            backend.run_until(0.00001)
            after = backend.network_snapshot()
            self.assertEqual(after["time_ns"], 10000)
            self.assertEqual(after, backend.network_snapshot(), "reading counters must not advance simulation")
            self.assertGreater(sum(p["kv_bytes"] for p in after["resources"]), 0)
            self.assertGreater(sum(p["collective_bytes"] for p in after["resources"]), 0)
            for port in after["resources"]:
                self.assertEqual(port["tx_bytes"], port["kv_bytes"]+port["collective_bytes"]+port["other_bytes"])
            self.assertTrue(any(f["logical_id"] == "data" for f in after["flows"]))
            feedback = replace(self.base.feedback, observation_delay_us=20)
            observer = NetworkObserver(backend, self.base.topology, feedback, self.root)
            try:
                self.assertIsNone(observer.latest_observed())
                backend.run_until(0.00003)
                self.assertEqual(observer.latest_observed()["time_seconds"], 0.00001)
            finally:
                observer.close()

    def test_staggered_deadlines_priority_conservation_and_replay(self):
        workload = self.workload()
        for name in ("a", "b"):
            output = self.root / name
            result = run_baseline(self.config, output, workload, self.profile)
            self.assertEqual(result.summary["completion_window"]["completed_count"], 3)
            events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            decisions = [e for e in events if e["event"] == "kv_scheduling_decision"]
            self.assertTrue(any("old" in e["request_budgets"] and "urgent" in e["request_budgets"]
                and any(b.startswith("urgent:") for b in e["admitted_blocks"]) for e in decisions))
            for event in decisions:
                if event["time_seconds"] < 0.02:
                    self.assertNotIn("future", event["request_budgets"])
                self.assertLessEqual(event["observed_at_seconds"], event["time_seconds"])
                for rid, b in event["request_budgets"].items():
                    arrival = {"old": 0, "urgent": 0.0006, "future": 0.02}[rid]
                    self.assertAlmostEqual(b["arrived_at_seconds"], arrival)
                    self.assertAlmostEqual(b["ttft_deadline_seconds"], arrival+self.config.evaluation.default_ttft_slo_seconds)
                for state in event["resources"].values():
                    self.assertLessEqual(state["inflight_bytes"], self.config.kv_scheduler.max_resource_inflight_bytes)
                    self.assertGreaterEqual(state["tokens"], -1e-7)
            kv = [e for e in events if e["event"] == "transfer_started" and e["transfer_kind"] == "kv"]
            self.assertEqual(sum(e["size_bytes"] for e in kv), 3*262144)
            final = json.loads((output / "final_resources.json").read_text())
            self.assertTrue(all(v["occupied_bytes"] == v["reserved_bytes"] == 0 for v in final.values()))
        self.assertEqual((self.root / "a/events.jsonl").read_bytes(), (self.root / "b/events.jsonl").read_bytes())

    def test_unsent_kv_stays_allocated_after_prefill(self):
        config = replace(self.config, runtime=replace(self.config.runtime, time_limit_seconds=0.00005),
                         evaluation=replace(self.config.evaluation, window_end_seconds=0.00005))
        output = self.root / "cutoff"
        result = run_baseline(config, output, self.workload(), self.profile)
        self.assertEqual(result.summary["arrival_cohort"]["incomplete_count"], 1)
        self.assertEqual(len(result.tokens), 1)
        resources = json.loads((output / "final_resources.json").read_text())
        self.assertEqual(resources["gpu-0"]["occupied_bytes"], 262144)

    def test_real_collective_guards_are_recorded(self):
        config = replace(self.base, topology=replace(self.base.topology, nic_bandwidth_gbps=10),
            kv_scheduler=replace(self.base.kv_scheduler, max_block_bytes=65536, max_resource_inflight_bytes=131072),
            runtime=replace(self.base.runtime, kv_transfer_mode="layer_ready_slo"))
        output = self.root / "protected"
        result = run_baseline(config, output)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 12)
        guards, groups = 0, 0
        with (output / "events.jsonl").open() as handle:
            for line in handle:
                e = json.loads(line)
                if e["event"] == "kv_scheduling_decision":
                    guards += e["blocked"].get("collective_protection", 0)
                if e["event"] == "collective_completed":
                    groups += 1
                    self.assertGreater(e["duration_seconds"], 0)
        self.assertGreater(guards, 0)
        self.assertGreater(groups, 0)

    def test_real_adaptive_windows_allow_multiple_reserved_blocks_and_obey_envelopes(self):
        config = replace(self.base,
            feedback=replace(self.base.feedback, network_observation_enabled=True),
            kv_scheduler=replace(self.base.kv_scheduler, max_block_bytes=65536,
                max_resource_inflight_bytes=1048576, max_inflight_bytes=8388608, max_concurrent_transfers=128),
            runtime=replace(self.base.runtime, kv_transfer_mode="layer_ready_slo"))
        output = self.root / "windows"
        result = run_baseline(config, output)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 12)
        events = [json.loads(line) for line in (output/"events.jsonl").read_text().splitlines()]
        blocks = {e["block_id"]: e for e in events if e["event"] == "transfer_started" and e["transfer_kind"] == "kv"}
        with (output/"observation/network_samples.jsonl").open() as handle:
            initial = json.loads(handle.readline())
        resources = NetworkResources(config.topology, initial["resources"])
        multi, samples = False, False
        for e in events:
            if e["event"] != "kv_scheduling_decision":
                continue
            for block_id in e["admitted_blocks"]:
                b = blocks[block_id]
                for key in resources.for_gpus(b["source_gpu_id"], b["destination_gpu_id"]):
                    state = e["resources"][key]
                    self.assertLessEqual(state["inflight_bytes"], state["inflight_limit_bytes"])
                    self.assertFalse(state["protected"])
                    if state["collective_limit_bytes"] is not None:
                        self.assertLessEqual(state["inflight_bytes"], state["collective_limit_bytes"])
                    multi |= state["collective_reserved"] and state["inflight_bytes"] > config.kv_scheduler.max_block_bytes
                    samples |= state["completion_samples"] > 0
        self.assertTrue(multi, "a protected high-bandwidth port must admit multiple blocks")
        self.assertTrue(samples)


if __name__ == "__main__":
    unittest.main()
