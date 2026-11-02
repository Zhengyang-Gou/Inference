"""Prefix identity, physical accounting, eviction and real ns-3 reuse."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError, require_backend
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.runtime import ComputeProfile, KV_TRANSFER_MODES, validate_requests
from joint_scheduling.config import load_config
from joint_scheduling.prefix_cache import PrefixCache, PrefixCacheConfig, prefix_chain
from joint_scheduling.reservations import PhysicalObjectReservation as Object
from joint_scheduling.reservations.ledger import CapacityLedger


def available():
    try:
        require_backend()
        return True
    except (BackendError, OSError, ValueError):
        return False


class PrefixUnitTests(unittest.TestCase):
    def test_identity_includes_namespace_and_entire_ordered_chain(self):
        a, b = dict(id="a", tokens=3), dict(id="b", tokens=2)
        self.assertNotEqual(prefix_chain([a, b], "model"), prefix_chain([b, a], "model"))
        self.assertNotEqual(prefix_chain([a], "model"), prefix_chain([a], "other-model"))
        self.assertNotEqual(prefix_chain([a, b], "model")[-1], prefix_chain([b], "model")[-1])

    def test_ready_only_query_is_pure_and_references_prevent_eviction(self):
        ledger = CapacityLedger({"gpu": 100})
        cache = PrefixCache(PrefixCacheConfig(enabled=True, max_bytes_per_instance=80), ledger, "m")
        blocks = [dict(id="a", tokens=3)]
        key = prefix_chain(blocks, "m")[0]
        objects = [Object("a", "gpu", 60, ("r0",))]
        ledger.try_commit("r0", objects)
        ledger.materialize("gpu", "a", 30)
        with self.assertRaises(ValueError):
            cache.publish("p", key, objects, 1)
        self.assertEqual(cache.lookup("p", blocks, 10), ())
        ledger.materialize("gpu", "a", 30)
        cache.publish("p", key, objects, 2)
        self.assertEqual(cache.lookup("p", blocks, 1), ())
        before = copy.deepcopy(cache.snapshot())
        hit = cache.lookup("p", blocks, 2)
        self.assertEqual(cache.snapshot(), before, "prediction queries must not mutate LRU/stats")
        ledger.try_commit("r1", [Object("a", "gpu", 60, ("r1",))])
        self.assertEqual(ledger.snapshot()["gpu"]["occupied_bytes"], 60)
        ledger.release_request("r0")
        demand = [Object("new", "gpu", 70, ("r2",))]
        self.assertFalse(cache.evict_for(demand))
        ledger.pin("gpu", "a")
        ledger.release_request("r1")
        self.assertFalse(cache.evict_for(demand), "read pins must survive request release")
        ledger.unpin("gpu", "a")
        self.assertTrue(cache.evict_for(demand))
        self.assertEqual(cache.lookup("p", blocks, 3), ())
        self.assertEqual(cache.stats["evictions"], 1)
        self.assertTrue(hit)

    def test_budget_and_reinsertion_after_eviction(self):
        ledger = CapacityLedger({"gpu": 200})
        cache = PrefixCache(PrefixCacheConfig(enabled=True, max_bytes_per_instance=60), ledger, "m")
        for i, name in enumerate(("a", "b", "a")):
            key = prefix_chain([dict(id=name, tokens=1)], "m")[0]
            objects = [Object(str(i), "gpu", 60, (str(i),))]
            ledger.try_commit(str(i), objects)
            ledger.materialize("gpu", str(i), 60)
            cache.publish("p", key, objects, i)
            ledger.release_request(str(i))
            self.assertEqual(cache.snapshot()["resident_bytes"], 60)
        self.assertEqual(cache.stats["evictions"], 2)
        self.assertEqual(cache.stats["insertions"], 3)

    def test_prefix_metadata_validation_and_cached_profile(self):
        request = dict(request_id="r", arrival_seconds=0, input_tokens=128, output_tokens=2,
                       prefix_blocks=[dict(id="a", tokens=64)])
        raw = dict(schema_version=1, provenance={"synthetic": True}, requests=[request])
        validate_requests(raw)
        for blocks in ([dict(id="a", tokens=129)], [dict(id="", tokens=1)], "bad"):
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                validate_requests(dict(raw, requests=[dict(request, prefix_blocks=blocks)]))
        with self.assertRaises(ValueError):
            validate_requests(dict(raw, requests=[request, dict(request, request_id="s",
                prefix_blocks=[dict(id="a", tokens=32)])]))
        profile = ComputeProfile(PACKAGE / "backend/fixtures/prefix_compute_profile.json")
        cold = sum(s["compute_seconds"] for s in profile.prefill_steps(128, 4))
        hit = sum(s["compute_seconds"] for s in profile.prefill_steps(128, 4, 64))
        self.assertLess(hit, cold)
        self.assertGreater(hit, 0)
        profile.prefill_steps(128, 4, 127)  # full-KV hit recomputes the last query
        with self.assertRaises(ValueError):
            profile.prefill_steps(128, 4, 128)
        with self.assertRaises(ValueError):
            ComputeProfile(PACKAGE / "backend/fixtures/layered_compute_profile.json").prefill_steps(128, 4, 64)


@unittest.skipUnless(available(), "build ns-3 for prefix integration checks")
class PrefixNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(PACKAGE / "configs/prefix_cache_smoke.json")
        self.config = replace(self.config, recording=replace(self.config.recording, mode="debug"))

    def run_case(self, name, config=None, requests=None, mode=None):
        workload = None
        if requests is not None:
            workload = self.root / (name+"-workload.json")
            workload.write_text(json.dumps(dict(schema_version=1, provenance={"synthetic": True}, requests=requests)))
        output = self.root / name
        result = run_baseline(config or self.config, output, workload_path=workload, kv_mode=mode)
        events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        return result, events, output

    def kv_bytes(self, events, rid):
        return sum(e["size_bytes"] for e in events if e["event"] == "transfer_started"
                   and e["transfer_kind"] == "kv" and e["request_id"] == rid)

    def test_p_and_d_hits_byte_conservation_and_all_five_kv_modes(self):
        for mode in KV_TRANSFER_MODES:
            with self.subTest(mode=mode):
                result, events, output = self.run_case(mode, mode=mode)
                self.assertEqual(result.summary["arrival_cohort"]["completed_by_observation_end"], 5)
                lookup = {e["request_id"]: e for e in events if e["event"] == "prefix_cache_lookup"}
                self.assertEqual((lookup["prefix-0"]["prefill_hit_tokens"], lookup["prefix-0"]["decode_hit_tokens"]), (0, 0))
                self.assertEqual((lookup["prefix-1"]["prefill_hit_tokens"], lookup["prefix-1"]["decode_hit_tokens"]), (64, 0))
                self.assertEqual((lookup["prefix-2"]["prefill_hit_tokens"], lookup["prefix-2"]["decode_hit_tokens"]), (64, 64))
                self.assertEqual((lookup["prefix-4"]["prefill_hit_tokens"], lookup["prefix-4"]["decode_hit_tokens"]), (128, 64))
                self.assertEqual(self.kv_bytes(events, "prefix-1"), 128*32768, "P hit alone must not reduce D's missing KV")
                self.assertEqual(self.kv_bytes(events, "prefix-2"), (192-64)*32768)
                self.assertEqual(self.kv_bytes(events, "prefix-4"), (192-64)*32768)
                for e in events:
                    if e["event"] == "resources":
                        for domain in e["domains"].values():
                            self.assertLessEqual(domain["occupied_bytes"]+domain["reserved_bytes"], domain["capacity_bytes"])
                summary = json.loads((output / "observation/prefix_cache_summary.json").read_text())
                final = json.loads((output / "final_resources.json").read_text())
                self.assertEqual(sum(x["reserved_bytes"] for x in final.values()), 0)
                self.assertEqual(sum(x["occupied_bytes"] for x in final.values()), summary["resident_bytes"])

    def test_disabled_restores_full_compute_and_kv_without_cache_residue(self):
        config = replace(self.config, prefix_cache=replace(self.config.prefix_cache, enabled=False))
        result, events, output = self.run_case("off", config)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 5)
        self.assertFalse(any(e["event"].startswith("prefix_cache") for e in events))
        self.assertEqual(self.kv_bytes(events, "prefix-2"), 192*32768)
        self.assertFalse((output / "observation/prefix_cache_summary.json").exists())
        self.assertTrue(all(x["occupied_bytes"] == x["reserved_bytes"] == 0
                            for x in json.loads((output / "final_resources.json").read_text()).values()))

    def test_full_prompt_hit_and_one_token_requests(self):
        config = replace(self.config, topology=replace(self.config.topology,
            placements=tuple(p for p in self.config.topology.placements if p.instance_id != "d1")))
        requests = [dict(request_id=str(i), arrival_seconds=i*.02, input_tokens=128,
                         output_tokens=3 if i < 2 else 1, prefix_blocks=[dict(id="whole", tokens=128)]) for i in range(3)]
        result, events, output = self.run_case("full", config, requests)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 3)
        self.assertEqual(self.kv_bytes(events, "1"), 0)
        self.assertEqual(self.kv_bytes(events, "2"), 0)
        lookup = [e for e in events if e["event"] == "prefix_cache_lookup"]
        self.assertEqual(lookup[1]["compute_cached_tokens"], 127)
        self.assertTrue(all(e["modeled_compute_seconds"] > 0 for e in events if e["event"] == "compute_step_started"))

    def test_independent_reuse_switches_and_missing_profile_rejected_before_output(self):
        for role in ("prefill", "decode"):
            config = replace(self.config, prefix_cache=replace(self.config.prefix_cache, **{role+"_reuse_enabled": False}))
            _, events, _ = self.run_case(role+"-off", config)
            self.assertTrue(all(e[role+"_hit_tokens"] == 0 for e in events if e["event"] == "prefix_cache_lookup"))
        config = replace(self.config, feedback=replace(self.config.feedback,
            compute_profile_path=str(PACKAGE / "backend/fixtures/layered_compute_profile.json")))
        with self.assertRaises(ValueError):
            run_baseline(config, self.root / "bad-profile")
        self.assertFalse((self.root / "bad-profile").exists())

    def test_no_future_or_in_progress_hits_and_deterministic_replay(self):
        requests = [dict(request_id=str(i), arrival_seconds=0, input_tokens=128, output_tokens=3,
                         prefix_blocks=[dict(id="a", tokens=64)]) for i in range(3)]
        _, events, a = self.run_case("replay-a", requests=requests)
        _, _, b = self.run_case("replay-b", requests=requests)
        self.assertEqual((a / "events.jsonl").read_bytes(), (b / "events.jsonl").read_bytes())
        lookups = [e for e in events if e["event"] == "prefix_cache_lookup"]
        self.assertEqual(lookups[0]["prefill_hit_tokens"], 0)
        self.assertTrue(all(e["decode_hit_tokens"] == 0 for e in lookups if e["time_seconds"] == 0))

    def test_memory_pressure_evicts_idle_cache_and_disabled_eviction_waits(self):
        per_gpu = 2**20 + 65536
        config = replace(self.config,
            topology=replace(self.config.topology, gpu_memory_gib=1+per_gpu/2**30,
                placements=tuple(p for p in self.config.topology.placements if p.instance_id != "d1")),
            reservations=replace(self.config.reservations, capacity_safety_fraction=0))
        requests = [dict(request_id=str(i), arrival_seconds=i*.02, input_tokens=128, output_tokens=3,
                         prefix_blocks=[dict(id=name, tokens=64)]) for i, name in enumerate(("a", "b", "a"))]
        result, events, output = self.run_case("pressure", config, requests)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 3)
        self.assertGreater(result.summary["prefix_cache"]["evictions"], 0)
        for e in events:
            if e["event"] == "resources":
                self.assertTrue(all(x["occupied_bytes"]+x["reserved_bytes"] <= per_gpu for x in e["domains"].values()))
        disabled = replace(config, prefix_cache=replace(config.prefix_cache, eviction_enabled=False))
        result, _, _ = self.run_case("no-eviction", disabled, requests)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 1)
        self.assertEqual(result.summary["arrival_cohort"]["incomplete_count"], 2)
        self.assertEqual(result.summary["prefix_cache"]["evictions"], 0)

    def test_partial_run_does_not_publish_unfinished_prefix(self):
        config = replace(self.config, runtime=replace(self.config.runtime, time_limit_seconds=.0001),
                         evaluation=replace(self.config.evaluation, window_end_seconds=.0001))
        result, events, output = self.run_case("cutoff", config)
        self.assertEqual(result.summary["arrival_cohort"]["incomplete_count"], 1)
        self.assertFalse(any(e["event"] == "prefix_cache_inserted" for e in events))
        resources = json.loads((output / "final_resources.json").read_text())
        self.assertGreater(sum(x["occupied_bytes"]+x["reserved_bytes"] for x in resources.values()), 0)
        self.assertEqual(result.summary["prefix_cache"]["resident_bytes"], 0)

    def test_optional_features_off_and_state_observation_is_passive(self):
        enabled_result, _, _ = self.run_case("observed")
        state_off = replace(self.config, feedback=replace(self.config.feedback, state_observation_enabled=False))
        result, _, output = self.run_case("state-off", state_off)
        self.assertEqual(enabled_result.tokens, result.tokens)
        self.assertFalse((output / "observation/state.jsonl").exists())
        all_off = replace(state_off, feedback=replace(state_off.feedback, network_observation_enabled=False),
            kv_scheduler=replace(state_off.kv_scheduler, slo_priority_enabled=False,
                                 collective_protection_enabled=False, inflight_budget_mode="fixed"))
        result, events, output = self.run_case("features-off", all_off)
        self.assertEqual(result.summary["completion_window"]["completed_count"], 5)
        self.assertFalse((output / "observation/network_samples.jsonl").exists())
        summary = json.loads((output / "observation/kv_scheduler_summary.json").read_text())
        self.assertEqual(summary["priority"], "fifo")
        self.assertFalse(summary["collective_protection_enabled"])
        for e in events:
            if e["event"] == "kv_scheduling_decision":
                self.assertTrue(all(not r["collective_reserved"] and r["collective_limit_bytes"] is None
                                    for r in e["resources"].values()))


if __name__ == "__main__":
    unittest.main()
