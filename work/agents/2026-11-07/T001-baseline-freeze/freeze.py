"""T001 static-only baseline freeze. Run from the repository root.

No formal source/config/input is modified. Everything is written into the
T001 task directory (outputs/agents/2026-11-07/T001-baseline-freeze/).
"""
import copy
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from joint_scheduling.config import load_config
from joint_scheduling.workload.inputs import load_workload
from joint_scheduling.binding.static import binding_plan_document, plan_summary
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import validate_profile

TASK = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze"
FROZEN = TASK / "results/frozen"
LOGS = TASK / "logs"
RESULTS = TASK / "results"
BASE = ROOT / "joint_scheduling/configs/research/base.json"
WORKLOAD_SRC = ROOT / "data/azure/conv_original.json"
PROFILE_SRC = ROOT / "joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json"
AZURE_MANIFEST = ROOT / "data/azure/manifest.json"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload, indent=2):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")


def check(label, fn):
    try:
        value = fn()
        return {"check": label, "status": "passed", "detail": str(value)[:180]}
    except Exception as error:
        return {"check": label, "status": "failed", "detail": "{}: {}".format(type(error).__name__, error)}


# --- 1. frozen config: deep copy of base.json, only task-specified changes ---
base_raw = BASE.read_text(encoding="utf-8")
base = json.loads(base_raw)
cfg = copy.deepcopy(base)
changes = {}


def set_field(section, key, value):
    old = cfg[section].get(key, None)
    cfg[section][key] = value
    if old != value or key not in base[section]:
        changes["{}.{}".format(section, key)] = {
            "base": old if key in base[section] else None,
            "frozen": value,
            "base_field_present": key in base[section],
        }


verified = {}

def verify_field(section, key, expected):
    """Explicitly assert an already-correct base value, without changing it."""
    actual = cfg[section].get(key, None)
    if actual != expected:
        raise ValueError("{} .{} is {!r}, expected {!r}".format(section, key, actual, expected))
    verified["{}.{}".format(section, key)] = actual


# Foundation acceptance switches required by TASK.md.
verify_field("binding", "window_enabled", False)
verify_field("binding", "prediction_enabled", False)
verify_field("binding", "joint_selection_enabled", False)
verify_field("binding", "decision_overhead_us", 0)
verify_field("binding", "policy", "balanced_random")
verify_field("binding", "seed", 7)
verify_field("prefix_cache", "enabled", False)
verify_field("runtime", "network_priority_enabled", False)
verify_field("runtime", "seed", 7)
verify_field("runtime", "prefill_chunk_tokens", 512)
verify_field("runtime", "prefill_batch_token_budget", 2048)
verify_field("runtime", "prefill_batch_size", 16)
verify_field("runtime", "decode_batch_size", 64)
verify_field("feedback", "network_observation_enabled", True)
verify_field("feedback", "network_observation_interval_us", 1000)
verify_field("feedback", "observation_delay_us", 100)
verify_field("feedback", "scheduling_overhead_us", 10.0)
verify_field("feedback", "time_authority", "ns3")
verify_field("feedback", "network_backend", "ns3_alibabacloud")
verify_field("recording", "mode", "essential")
verify_field("evaluation", "default_ttft_slo_seconds", 1.0)
verify_field("evaluation", "default_itl_slo_seconds", 0.1)

# Only these fields actually differ from base.json.
set_field("kv_scheduler", "policy", "ready_immediate")
set_field("kv_scheduler", "slo_priority_enabled", False)
set_field("kv_scheduler", "collective_protection_enabled", False)
set_field("kv_scheduler", "decode_admission_limit", 0)
set_field("runtime", "kv_transfer_mode", "layer_ready_immediate")
set_field("feedback", "state_observation_enabled", True)
set_field("feedback", "compute_profile_path", "compute_profile.json")
set_field("runtime", "workload_path", "workload.json")
set_field("evaluation", "output_dir", "evaluation")

write_json(FROZEN / "config.json", cfg)

# --- 2. byte-identical copies of the two source inputs ---
for src, name in ((PROFILE_SRC, "compute_profile.json"), (WORKLOAD_SRC, "workload.json")):
    shutil.copyfile(src, FROZEN / name)
    assert sha256(src) == sha256(FROZEN / name), "byte copy mismatch: " + name

# --- 3. deterministic binding plan from the project's static implementation ---
frozen_cfg = load_config(str(FROZEN / "config.json"))
requests, workload_provenance = load_workload(str(FROZEN / "workload.json"))
plan_a = binding_plan_document(requests, frozen_cfg.topology, frozen_cfg.binding)
plan_b = binding_plan_document(requests, frozen_cfg.topology, frozen_cfg.binding)
assert plan_a == plan_b, "binding plan is not deterministic"
assert plan_a["bindings_sha256"] == plan_b["bindings_sha256"]
assert plan_a["request_count"] == len(requests) == 19366
write_json(FROZEN / "binding_plan.json", plan_a)

# --- 4. input audit ---
raw_workload = json.loads(WORKLOAD_SRC.read_text(encoding="utf-8"))
rows = raw_workload["requests"]
arrivals = [r["arrival_seconds"] for r in rows]
inputs = [r["input_tokens"] for r in rows]
outputs = [r["output_tokens"] for r in rows]
max_context = max(i + o - 1 for i, o in zip(inputs, outputs))
field_counts = dict(Counter(k for r in rows for k in r))
ids = [r["request_id"] for r in rows]
azure_manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
expected_sha = azure_manifest["prepared"][0]["sha256"]
actual_sha = sha256(WORKLOAD_SRC)

profile = ComputeProfile(str(FROZEN / "compute_profile.json"))
coverage = [
    check("validate_prefill_batch_coverage(max_input={}, query_cap=512, budget=2048, tp=1)".format(max(inputs)),
          lambda: profile.validate_prefill_batch_coverage(max(inputs), 512, 2048, 1)),
    check("lookup_decode_batch(64 x {} tokens)".format(max_context),
          lambda: profile.lookup_decode_batch([max_context] * 64, 1)),
    check("lookup_decode_batch(1 x {} tokens)".format(max_context),
          lambda: profile.lookup_decode_batch([max_context], 1)),
    check("expert_profile.validate_profile(frozen_config, frozen_profile)",
          lambda: validate_profile(frozen_cfg, profile)),
]
gaps = [c for c in coverage if c["status"] != "passed"]
if actual_sha != expected_sha:
    gaps.append({"check": "workload_sha256_vs_manifest", "status": "failed",
                 "detail": "{} != {}".format(actual_sha, expected_sha)})

input_audit = {
    "schema_version": 1,
    "task": "T001-baseline-freeze",
    "evidence_type": "static_input_audit_only_no_simulation",
    "workload": {
        "path": str(WORKLOAD_SRC.relative_to(ROOT)),
        "sha256": actual_sha,
        "manifest_expected_sha256": expected_sha,
        "identity_match": actual_sha == expected_sha,
        "request_count": len(rows),
        "manifest_request_count": azure_manifest["prepared"][0]["request_count"],
        "arrival_min_seconds": min(arrivals),
        "arrival_max_seconds": max(arrivals),
        "manifest_duration_seconds": azure_manifest["prepared"][0]["duration_seconds"],
        "input_tokens_min": min(inputs),
        "input_tokens_max": max(inputs),
        "output_tokens_min": min(outputs),
        "output_tokens_max": max(outputs),
        "max_decode_context_tokens": max_context,
        "duplicate_request_ids": len(ids) - len(set(ids)),
        "request_field_counts": field_counts,
        "request_level_slo_overrides": {
            "ttft_slo_seconds": sum("ttft_slo_seconds" in r for r in rows),
            "itl_slo_seconds": sum("itl_slo_seconds" in r for r in rows),
        },
        "prefix_blocks_requests": sum("prefix_blocks" in r for r in rows),
        "max_output_tokens_requests": sum("max_output_tokens" in r for r in rows),
        "provenance_selected": {k: raw_workload["provenance"].get(k) for k in (
            "source_file", "source_sha256", "source_request_count", "source_synthetic",
            "synthetic", "source_lengths", "clipped_requests", "dropped_requests",
            "arrival_time_scaled", "time_scale", "injection_duration_seconds",
            "selected_request_count", "slo_origin")},
    },
    "profile": {
        "path": str(PROFILE_SRC.relative_to(ROOT)),
        "sha256": sha256(PROFILE_SRC),
        "schema_version": profile.raw.get("schema_version"),
        "provenance_profile_kind": profile.raw["provenance"].get("profile_kind"),
        "provenance_synthetic": profile.raw["provenance"].get("synthetic"),
        "prefill_layers": len(profile.phases["prefill"]["layers"]),
        "decode_layers": len(profile.phases["decode"]["layers"]),
        "decode_dense_layers": profile.phases["decode"]["layers"].count("dense"),
        "decode_moe_layers": profile.phases["decode"]["layers"].count("moe"),
        "decode_batch_sizes": [b["batch_size"] for b in profile.phases["decode"]["batches"]],
        "decode_point_max_tokens": max(x["tokens"] for x in profile.phases["decode"]["points"]),
        "prefill_chunk_groups": [g["chunk_tokens"] for g in profile.phases["prefill"]["chunked"]["groups"]],
        "prefill_linear_max_tokens": max(x["tokens"] for x in profile.phases["prefill"]["chunked"]["linear_points"]),
        "prefill_layer0_point_max_tokens": max(x["tokens"] for x in profile.phases["prefill"]["layers"][0]["points"]),
        "kv_bytes_per_token": profile.raw.get("kv_bytes_per_token"),
        "weights_bytes_per_gpu": profile.raw.get("weights_bytes_per_gpu"),
        "network_communication_excluded": profile.raw.get("network_communication_excluded"),
        "coverage_checks": coverage,
    },
    "binding": {
        "request_count": plan_a["request_count"],
        "seed": plan_a["seed"],
        "policy": plan_a["policy"],
        "bindings_sha256": plan_a["bindings_sha256"],
        "prefill_counts": plan_a["prefill_counts"],
        "decode_counts": plan_a["decode_counts"],
        "cross_leaf_requests": plan_a["cross_leaf_requests"],
        "cross_leaf_fraction": plan_a["cross_leaf_fraction"],
        "regenerated_identical": True,
    },
    "slo": {
        "default_ttft_slo_seconds": cfg["evaluation"]["default_ttft_slo_seconds"],
        "default_itl_slo_seconds": cfg["evaluation"]["default_itl_slo_seconds"],
        "origin": "manual_experiment_assumption_not_from_trace",
        "all_requests_use_defaults": not any(
            "ttft_slo_seconds" in r or "itl_slo_seconds" in r for r in rows),
    },
    "gaps": gaps,
}
write_json(RESULTS / "input-audit.json", input_audit)

# --- 5. manifest ---
baseline_status = (LOGS / "baseline-status.txt").read_text(encoding="utf-8")
head = (LOGS / "git-head.txt").read_text(encoding="utf-8").strip()
sources = []
for role, path in (("base_config", BASE), ("workload", WORKLOAD_SRC),
                   ("compute_profile", PROFILE_SRC), ("azure_manifest", AZURE_MANIFEST)):
    sources.append({"role": role, "path": str(path.relative_to(ROOT)),
                    "sha256": sha256(path), "bytes": path.stat().st_size})
frozen_files = []
for name in ("config.json", "compute_profile.json", "workload.json", "binding_plan.json"):
    path = FROZEN / name
    frozen_files.append({"path": "results/frozen/" + name, "sha256": sha256(path),
                         "bytes": path.stat().st_size})

manifest = {
    "schema_version": 1,
    "task": "T001-baseline-freeze",
    "purpose": "static freeze of stage-one baseline conditions; no simulation executed",
    "sources": sources,
    "frozen_files": frozen_files,
    "byte_identical_copies": {
        "compute_profile.json": sha256(PROFILE_SRC) == sha256(FROZEN / "compute_profile.json"),
        "workload.json": sha256(WORKLOAD_SRC) == sha256(FROZEN / "workload.json"),
    },
    "config_field_diff_from_base": changes,
    "explicitly_verified_unchanged_switches": verified,
    "git": {
        "head": head,
        "baseline_status_file": "logs/baseline-status.txt",
        "baseline_status_sha256": hashlib.sha256(baseline_status.encode("utf-8")).hexdigest(),
        "dirty_entry_count": len([line for line in baseline_status.splitlines() if line.strip()]),
        "note": "pre-existing dirty state preserved; not reset, not cleaned, not committed",
    },
    "model_assumptions": {
        "profile_kind": "estimated_simulation_profile",
        "device": "NVIDIA H800",
        "model": "deepseek-ai/DeepSeek-V3",
        "tp_size": 1,
        "ep_groups": 2,
        "ep_size_per_group": 16,
        "layers": 61,
        "dense_layers": 3,
        "moe_layers": 58,
        "routing": "deterministic synthetic layerwise routing; no real expert activation trace",
        "slo": "manual experiment assumption; Azure trace supplies no business SLO",
        "local_expert_combine_extra_cost": "explicitly zero in the profile",
        "no_double_microbatch_or_deepep_node_aggregation": True,
    },
    "parameter_scope": {
        "active_in_layer_ready_immediate": [
            "runtime.kv_transfer_mode", "runtime.prefill_chunk_tokens",
            "runtime.prefill_batch_token_budget", "runtime.prefill_batch_size",
            "runtime.decode_batch_size", "kv_scheduler.max_block_bytes",
            "feedback.state_observation_enabled", "feedback.network_observation_enabled",
            "feedback.network_observation_interval_us", "feedback.observation_delay_us",
            "expert_parallel.*", "reservations.*", "evaluation.*", "recording.mode",
        ],
        "inactive_when_no_gated_kv": [
            "kv_scheduler.policy (ready_immediate selects no FIFO/MLU gate)",
            "kv_scheduler.slo_priority_enabled", "kv_scheduler.collective_protection_enabled",
            "kv_scheduler.decode_admission_limit", "kv_scheduler.max_concurrent_transfers",
            "kv_scheduler.max_inflight_bytes", "kv_scheduler.max_resource_inflight_bytes",
            "kv_scheduler.safety_margin_fraction", "kv_scheduler.control_interval_us",
            "kv_scheduler.collective_guard_us", "kv_scheduler.collective_reserve_fraction",
            "kv_scheduler.pause_on_collective", "kv_scheduler.queue_target_bytes",
            "kv_scheduler.inflight_budget_mode", "kv_scheduler.collective_max_blocking_us",
            "kv_scheduler.completion_feedback_alpha", "kv_scheduler.mlu_*",
            "feedback.scheduling_overhead_us (configured 10 us, no controlled KV decision)",
        ],
        "observation_is_not_optimization": True,
    },
    "claims": {
        "source_archive": False,
        "backend_binary_archive": False,
        "end_to_end_simulation": False,
        "algorithm_comparison_or_scaling_experiment": False,
        "note": "TASK.md forbids these; only static validation is claimed",
    },
}
write_json(FROZEN / "manifest.json", manifest)

print("frozen files:", ", ".join(f["path"] for f in frozen_files))
print("config field diff keys:", ", ".join(sorted(changes)))
print("verified unchanged switches:", len(verified))
print("binding plan:", plan_a["request_count"], "requests, seed", plan_a["seed"],
      "sha256", plan_a["bindings_sha256"])
print("input identity match:", input_audit["workload"]["identity_match"])
print("coverage checks:", [(c["check"].split("(")[0], c["status"]) for c in coverage])
print("gaps:", gaps)
print("git head:", head, "dirty entries:", manifest["git"]["dirty_entry_count"])
