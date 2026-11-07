"""T002 static-only input preparation and identity check.

No ns-3 run is started. This script only prepares inputs/ and records evidence,
because the WSL entry point is blocked by the tool security policy (see
logs/wsl-tool-policy-block.txt). All checks here are static and must not be
reported as end-to-end validation.
"""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from joint_scheduling.config import load_config
from joint_scheduling.workload.inputs import load_workload
from joint_scheduling.binding.static import binding_plan_document
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import validate_profile

T001 = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze"
T001_FROZEN = T001 / "results/frozen"
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation"
INPUTS = TASK / "inputs"
RESULTS = TASK / "results"
LOGS = TASK / "logs"

EXPECTED_SAMPLE = [
    {"request_id": "azure-conv-000000000", "arrival_seconds": 0.0,
     "input_tokens": 374, "output_tokens": 44, "p": "p0", "d": "d3"},
    {"request_id": "azure-conv-000000001", "arrival_seconds": 4.314579,
     "input_tokens": 396, "output_tokens": 109, "p": "p1", "d": "d14"},
    {"request_id": "azure-conv-000000002", "arrival_seconds": 4.541877,
     "input_tokens": 879, "output_tokens": 55, "p": "p2", "d": "d7"},
]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload, indent=2):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n",
                          encoding="utf-8")


INPUTS.mkdir(parents=True, exist_ok=True)

# --- 1. T001 frozen identity -------------------------------------------------
t001_manifest = json.loads((T001_FROZEN / "manifest.json").read_text(encoding="utf-8"))
frozen_hashes = {}
for entry in t001_manifest["frozen_files"]:
    path = T001 / entry["path"]
    actual = sha256(path)
    frozen_hashes[entry["path"]] = {
        "expected_sha256": entry["sha256"], "actual_sha256": actual,
        "match": actual == entry["sha256"], "bytes": path.stat().st_size}

source_config = json.loads((T001_FROZEN / "config.json").read_text(encoding="utf-8"))
source_workload = json.loads((T001_FROZEN / "workload.json").read_text(encoding="utf-8"))
parent_plan = json.loads((T001_FROZEN / "binding_plan.json").read_text(encoding="utf-8"))
parent_by_id = {b["request_id"]: b for b in parent_plan["bindings"]}

# --- 2. sample identity against TASK.md table --------------------------------
first3 = source_workload["requests"][:3]
sample_rows = []
for index, (row, expected) in enumerate(zip(first3, EXPECTED_SAMPLE)):
    parent = parent_by_id[row["request_id"]]
    actual = {
        "request_id": row["request_id"], "arrival_seconds": row["arrival_seconds"],
        "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
        "p": parent["prefill_instance_id"], "d": parent["decode_instance_id"],
    }
    sample_rows.append({"index": index, "actual": actual, "expected": expected,
                        "match": actual == expected})
sample_match = all(r["match"] for r in sample_rows)
sum_input = sum(r["actual"]["input_tokens"] for r in sample_rows)
sum_output = sum(r["actual"]["output_tokens"] for r in sample_rows)

# --- 3. inputs/workload.json : extraction only, request fields unchanged -----
sample_workload = {
    "schema_version": source_workload["schema_version"],
    "provenance": source_workload["provenance"],
    "requests": copy.deepcopy(first3),
}
write_json(INPUTS / "workload.json", sample_workload)

# --- 4. inputs/compute_profile.json : byte-identical copy -------------------
shutil.copyfile(T001_FROZEN / "compute_profile.json", INPUTS / "compute_profile.json")
assert sha256(T001_FROZEN / "compute_profile.json") == sha256(INPUTS / "compute_profile.json")

# --- 5. inputs/config.json : only the three permitted window/limit diffs ----
cfg = copy.deepcopy(source_config)
diff = {}


def set_field(section, key, value):
    old = cfg[section].get(key, None)
    cfg[section][key] = value
    if old != value:
        diff["{}.{}".format(section, key)] = {"t001": old, "t002": value}


set_field("evaluation", "window_start_seconds", 0.0)
set_field("evaluation", "window_end_seconds", 30.0)
set_field("runtime", "time_limit_seconds", 30.0)
write_json(INPUTS / "config.json", cfg)

# --- 6. inputs/binding_plan.json : project plan for the real subset ---------
loaded_cfg = load_config(str(INPUTS / "config.json"))
loaded_requests, _ = load_workload(str(INPUTS / "workload.json"))
sub_plan = binding_plan_document(loaded_requests, loaded_cfg.topology, loaded_cfg.binding)
projection = []
for row in sub_plan["bindings"]:
    parent = parent_by_id[row["request_id"]]
    projection.append({
        "request_id": row["request_id"],
        "prefill_instance_id": row["prefill_instance_id"],
        "decode_instance_id": row["decode_instance_id"],
        "parent_prefill_instance_id": parent["prefill_instance_id"],
        "parent_decode_instance_id": parent["decode_instance_id"],
        "match": (row["prefill_instance_id"] == parent["prefill_instance_id"]
                  and row["decode_instance_id"] == parent["decode_instance_id"]),
    })
projection_match = all(r["match"] for r in projection) and len(projection) == 3
write_json(INPUTS / "binding_plan.json", sub_plan)

# --- 7. static checks (NOT a simulation result) ----------------------------
profile = ComputeProfile(str(INPUTS / "compute_profile.json"))
static_checks = []
for label, fn in (
    ("load_config(inputs/config.json)", lambda: load_config(str(INPUTS / "config.json"))),
    ("load_workload(inputs/workload.json) has 3 requests",
     lambda: len(load_workload(str(INPUTS / "workload.json"))[0]) == 3),
    ("expert_profile.validate_profile", lambda: validate_profile(loaded_cfg, profile)),
    ("profile.validate_prefill_batch_coverage(879,512,2048,1)",
     lambda: profile.validate_prefill_batch_coverage(879, 512, 2048, 1)),
    ("profile.lookup_decode_batch(1 x 933)", lambda: profile.lookup_decode_batch([933], 1)),
):
    try:
        value = fn()
        if value is False:
            raise ValueError("returned False")
        static_checks.append({"check": label, "status": "passed", "detail": str(value)[:120]})
    except Exception as error:
        static_checks.append({"check": label, "status": "failed",
                              "detail": "{}: {}".format(type(error).__name__, error)})

# --- 8. sample-manifest.json -------------------------------------------------
sample_manifest = {
    "schema_version": 1,
    "task": "T002-bounded-baseline-validation",
    "evidence_type": "static_input_preparation_only_no_ns3_run",
    "source": {
        "t001_frozen_dir": str(T001_FROZEN.relative_to(ROOT)),
        "t001_frozen_files": frozen_hashes,
        "t001_parent_binding_plan_sha256": sha256(T001_FROZEN / "binding_plan.json"),
        "t001_parent_bindings_sha256": parent_plan["bindings_sha256"],
        "t001_parent_plan_request_count": parent_plan["request_count"],
        "t001_parent_plan_seed": parent_plan["seed"],
    },
    "extraction": {
        "rule": "first 3 requests in original order, request fields unchanged",
        "indices": [0, 1, 2],
        "request_ids": [r["actual"]["request_id"] for r in sample_rows],
        "arrival_times_preserved": True,
        "input_token_sum": sum_input,
        "output_token_sum": sum_output,
        "rows": sample_rows,
        "matches_task_table": sample_match,
    },
    "binding_projection": {
        "method": "binding_plan_document(first3, topology, binding) compared row-by-row with parent plan projection",
        "rows": projection,
        "all_endpoints_match_parent": projection_match,
        "subset_plan_bindings_sha256": sub_plan["bindings_sha256"],
        "subset_plan_request_count": sub_plan["request_count"],
        "subset_plan_seed": sub_plan["seed"],
        "subset_prefill_counts": sub_plan["prefill_counts"],
        "subset_decode_counts": sub_plan["decode_counts"],
        "subset_cross_leaf_requests": sub_plan["cross_leaf_requests"],
        "note": "parent plan hash kept here only; binding_plan.json has no custom fields so runner equality check applies",
    },
    "config_diff_from_t001": diff,
    "unchanged_conditions": [
        "topology / placements / EP groups / network / ns-3 backend",
        "kv_transfer_mode=layer_ready_immediate, kv_scheduler.policy=ready_immediate",
        "no FIFO/MLU gating, no collective protection, no decode admission, no network priority",
        "no binding window/prediction/joint selection, no prefix cache",
        "prefill chunk 512 / token budget 2048 / batch 16, decode batch 64",
        "runtime.seed=7, binding.seed=7, policy=balanced_random",
        "SLO TTFT 1.0s / ITL 0.1s, evaluation defaults, time_bin 1.0, percentiles",
        "network observation 1000us + visibility 100us, state observation on, recording essential",
        "compute profile byte-identical to T001; workload provenance preserved verbatim",
    ],
    "inputs_files": {},
}
for name in ("config.json", "workload.json", "compute_profile.json", "binding_plan.json"):
    path = INPUTS / name
    sample_manifest["inputs_files"][name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
write_json(INPUTS / "sample-manifest.json", sample_manifest)

# --- 9. machine-readable static identity result -----------------------------
identity = {
    "schema_version": 1,
    "task": "T002",
    "evidence_type": "static_only_not_end_to_end_validation",
    "t001_frozen_identity_verified": all(v["match"] for v in frozen_hashes.values()),
    "sample_matches_task_table": sample_match,
    "binding_endpoints_match_parent_projection": projection_match,
    "config_diff_keys": sorted(diff),
    "static_checks": static_checks,
    "ns3_end_to_end_runs": 0,
    "blocked_reason": "wsl.exe is on the tool Program Blacklist; see logs/wsl-tool-policy-block.txt",
}
write_json(RESULTS / "input-identity-check.json", identity)

print("T001 frozen hashes verified:", identity["t001_frozen_identity_verified"])
print("sample matches TASK table:", sample_match, "input sum", sum_input, "output sum", sum_output)
print("binding endpoints match parent projection:", projection_match)
print("subset bindings_sha256:", sub_plan["bindings_sha256"])
print("config diff keys:", sorted(diff))
print("static checks:", [(c["check"].split("(")[0], c["status"]) for c in static_checks])
print("inputs files:", list(sample_manifest["inputs_files"]))
print("ns-3 end-to-end runs: 0 (blocked)")
