"""T001 static identity / consistency verification. Run from the repository root."""
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from joint_scheduling.config import load_config
from joint_scheduling.workload.inputs import load_workload
from joint_scheduling.binding.static import binding_plan_document
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import validate_profile

TASK = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze"
FROZEN = TASK / "results/frozen"
BASE = ROOT / "joint_scheduling/configs/research/base.json"
WORKLOAD_SRC = ROOT / "data/azure/conv_original.json"
PROFILE_SRC = ROOT / "joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def diff(a, b, prefix=""):
    out = {}
    for key in sorted(set(a) | set(b)):
        path = "{}.{}".format(prefix, key) if prefix else key
        if key not in a:
            out[path] = {"base": None, "frozen": b[key]}
        elif key not in b:
            out[path] = {"base": a[key], "frozen": None}
        elif isinstance(a[key], dict) and isinstance(b[key], dict):
            out.update(diff(a[key], b[key], path))
        elif a[key] != b[key]:
            out[path] = {"base": a[key], "frozen": b[key]}
    return out


manifest = json.loads((FROZEN / "manifest.json").read_text(encoding="utf-8"))
audit = json.loads((TASK / "results/input-audit.json").read_text(encoding="utf-8"))
base_cfg = json.loads(BASE.read_text(encoding="utf-8"))
frozen_raw = json.loads((FROZEN / "config.json").read_text(encoding="utf-8"))
plan_file = json.loads((FROZEN / "binding_plan.json").read_text(encoding="utf-8"))

checks = []


def record(name, ok, detail):
    checks.append({"check": name, "status": "passed" if ok else "failed", "detail": detail})
    return ok


# 1. source / frozen file identities
for entry in manifest["sources"]:
    record("source_sha256:" + entry["role"], sha256(ROOT / entry["path"]) == entry["sha256"],
           entry["sha256"])
for entry in manifest["frozen_files"]:
    record("frozen_sha256:" + entry["path"], sha256(TASK / entry["path"]) == entry["sha256"],
           entry["sha256"])
record("byte_copy:compute_profile", (FROZEN / "compute_profile.json").read_bytes() == PROFILE_SRC.read_bytes(),
       "identical bytes")
record("byte_copy:workload", (FROZEN / "workload.json").read_bytes() == WORKLOAD_SRC.read_bytes(),
       "identical bytes")

# 2. config diff is exactly the declared set
actual_diff = diff(base_cfg, frozen_raw)
declared = set(manifest["config_field_diff_from_base"])
record("config_diff_keys_match_manifest", set(actual_diff) == declared,
       "actual={} declared={}".format(sorted(actual_diff), sorted(declared)))
record("config_diff_is_task_scope_only", set(actual_diff) == {
    "kv_scheduler.policy", "kv_scheduler.slo_priority_enabled",
    "kv_scheduler.collective_protection_enabled", "kv_scheduler.decode_admission_limit",
    "runtime.kv_transfer_mode", "feedback.state_observation_enabled",
    "feedback.compute_profile_path", "runtime.workload_path", "evaluation.output_dir"},
       sorted(actual_diff))

# 3. project entry loads frozen config; paths resolve inside frozen/
cfg = load_config(str(FROZEN / "config.json"))
record("frozen_profile_path_resolves", cfg.profile_path().is_file(), str(cfg.profile_path()))
record("frozen_evaluation_path_relative", not Path(cfg.evaluation.output_dir).is_absolute(),
       cfg.evaluation.output_dir)
record("frozen_workload_path_relative", not Path(cfg.runtime.workload_path).is_absolute(),
       cfg.runtime.workload_path)
record("backend_is_ns3", (cfg.feedback.time_authority, cfg.feedback.network_backend) == ("ns3", "ns3_alibabacloud"),
       "{} + {}".format(cfg.feedback.time_authority, cfg.feedback.network_backend))

# 4. switch values from the loaded config
switches = {
    "binding.window_enabled": cfg.binding.window_enabled,
    "binding.prediction_enabled": cfg.binding.prediction_enabled,
    "binding.joint_selection_enabled": cfg.binding.joint_selection_enabled,
    "binding.decision_overhead_us": cfg.binding.decision_overhead_us,
    "binding.policy": cfg.binding.policy,
    "binding.seed": cfg.binding.seed,
    "prefix_cache.enabled": cfg.prefix_cache.enabled,
    "runtime.kv_transfer_mode": cfg.runtime.kv_transfer_mode,
    "runtime.network_priority_enabled": cfg.runtime.network_priority_enabled,
    "runtime.seed": cfg.runtime.seed,
    "kv_scheduler.policy": cfg.kv_scheduler.policy,
    "kv_scheduler.slo_priority_enabled": cfg.kv_scheduler.slo_priority_enabled,
    "kv_scheduler.collective_protection_enabled": cfg.kv_scheduler.collective_protection_enabled,
    "kv_scheduler.decode_admission_limit": cfg.kv_scheduler.decode_admission_limit,
    "feedback.state_observation_enabled": cfg.feedback.state_observation_enabled,
    "feedback.network_observation_enabled": cfg.feedback.network_observation_enabled,
    "feedback.network_observation_interval_us": cfg.feedback.network_observation_interval_us,
    "feedback.observation_delay_us": cfg.feedback.observation_delay_us,
    "feedback.scheduling_overhead_us": cfg.feedback.scheduling_overhead_us,
    "recording.mode": cfg.recording.mode,
}
expected = {
    "binding.window_enabled": False, "binding.prediction_enabled": False,
    "binding.joint_selection_enabled": False, "binding.decision_overhead_us": 0,
    "binding.policy": "balanced_random", "binding.seed": 7,
    "prefix_cache.enabled": False, "runtime.kv_transfer_mode": "layer_ready_immediate",
    "runtime.network_priority_enabled": False, "runtime.seed": 7,
    "kv_scheduler.policy": "ready_immediate", "kv_scheduler.slo_priority_enabled": False,
    "kv_scheduler.collective_protection_enabled": False, "kv_scheduler.decode_admission_limit": 0,
    "feedback.state_observation_enabled": True, "feedback.network_observation_enabled": True,
    "feedback.network_observation_interval_us": 1000, "feedback.observation_delay_us": 100,
    "feedback.scheduling_overhead_us": 10.0, "recording.mode": "essential",
}
for key, value in expected.items():
    record("switch:" + key, switches[key] == value, "{!r} == {!r}".format(switches[key], value))

# 5. binding plan regeneration and independence from KV policy / network seed
requests, _ = load_workload(str(FROZEN / "workload.json"))
plan_new = binding_plan_document(requests, cfg.topology, cfg.binding)
record("binding_plan_regeneration_identical", plan_new == plan_file,
       "sha256={}".format(plan_new["bindings_sha256"]))
alt = replace(cfg,
              kv_scheduler=replace(cfg.kv_scheduler, policy="slo_mlu", slo_priority_enabled=True),
              runtime=replace(cfg.runtime, kv_transfer_mode="layer_ready_slo", seed=999))
plan_alt = binding_plan_document(requests, alt.topology, alt.binding)
record("binding_independent_of_kv_policy_and_network_seed",
       plan_alt["bindings_sha256"] == plan_file["bindings_sha256"],
       "alt sha256={}".format(plan_alt["bindings_sha256"]))
ids = {r["request_id"] for r in requests}
plan_ids = {b["request_id"] for b in plan_file["bindings"]}
record("binding_covers_all_requests", ids == plan_ids and len(plan_file["bindings"]) == len(requests),
       "requests={} bindings={}".format(len(requests), len(plan_file["bindings"])))
record("binding_counts_sum_to_requests",
       sum(plan_file["prefill_counts"].values()) == len(requests)
       and sum(plan_file["decode_counts"].values()) == len(requests),
       "p={} d={}".format(sum(plan_file["prefill_counts"].values()),
                          sum(plan_file["decode_counts"].values())))

# 6. profile coverage, re-run against the frozen copies
profile = ComputeProfile(str(FROZEN / "compute_profile.json"))
max_input = audit["workload"]["input_tokens_max"]
max_context = audit["workload"]["max_decode_context_tokens"]
for label, fn in (
    ("prefill_batch_coverage", lambda: profile.validate_prefill_batch_coverage(max_input, 512, 2048, 1)),
    ("decode_batch_64", lambda: profile.lookup_decode_batch([max_context] * 64, 1)),
    ("decode_batch_1", lambda: profile.lookup_decode_batch([max_context], 1)),
    ("expert_validate", lambda: validate_profile(cfg, profile)),
):
    try:
        fn()
        record("coverage:" + label, True, "no extrapolation error")
    except Exception as error:
        record("coverage:" + label, False, "{}: {}".format(type(error).__name__, error))

# 7. workload identity and SLO overrides against the frozen copy
record("workload_identity_matches_manifest", audit["workload"]["identity_match"],
       audit["workload"]["sha256"])
record("no_request_level_slo_overrides", audit["slo"]["all_requests_use_defaults"],
       str(audit["workload"]["request_level_slo_overrides"]))
record("audit_gaps_empty", audit["gaps"] == [], str(audit["gaps"]))

failed = [c for c in checks if c["status"] != "passed"]
result = {"checks_total": len(checks), "passed": len(checks) - len(failed),
          "failed": len(failed), "failures": failed, "checks": checks}
(TASK / "results/verification.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("checks:", result["passed"], "/", result["checks_total"], "passed; failures:", len(failed))
for item in failed:
    print("FAILED", item["check"], item["detail"])
print("binding sha256:", plan_new["bindings_sha256"])
print("config diff keys:", sorted(actual_diff))
raise SystemExit(1 if failed else 0)
