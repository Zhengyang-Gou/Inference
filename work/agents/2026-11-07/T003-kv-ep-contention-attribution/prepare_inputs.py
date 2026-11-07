"""T003 step 3: prepare the ONLY allowed inputs (8 frozen requests).

Runs inside the WSL simai environment. Does not touch formal source/config/data;
writes only the task inputs/ and the diagnostic root skeleton.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
FROZEN = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen"
INPUTS = TASK / "inputs"
DIAG = TASK / "results/diagnostic"
EXPECTED_MANIFEST = "811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78"
N = 8


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def diff(a, b, path="", out=None):
    """Recursive difference from b (frozen) to a (new)."""
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in b:
                out.append(dict(path=path + key, change="added", value=a[key]))
            elif key not in a:
                out.append(dict(path=path + key, change="removed", value=b[key]))
            else:
                diff(a[key], b[key], path + key + ".", out)
    elif a != b:
        out.append(dict(path=path[:-1], from_value=b, to_value=a))
    return out


def main():
    if INPUTS.exists() or DIAG.exists():
        print("REFUSE: inputs/ or results/diagnostic/ already exists", file=sys.stderr)
        return 2
    INPUTS.mkdir(parents=True)
    DIAG.mkdir(parents=True)
    (DIAG / "inputs").mkdir()

    # ---- A1: frozen identity -------------------------------------------------
    manifest_hash = sha(FROZEN / "manifest.json")
    frozen = {}
    for name in ("config.json", "compute_profile.json", "workload.json", "binding_plan.json"):
        frozen[name] = sha(FROZEN / name)
    frozen_manifest = json.loads((FROZEN / "manifest.json").read_text(encoding="utf-8"))
    checks = {"expected_frozen_manifest_sha256": EXPECTED_MANIFEST,
              "actual_frozen_manifest_sha256": manifest_hash,
              "manifest_sha256_match": manifest_hash == EXPECTED_MANIFEST,
              "frozen_files": {}}
    for entry in frozen_manifest["frozen_files"]:
        rel = entry["path"].replace("\\", "/")
        path = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze" / rel
        checks["frozen_files"][rel] = dict(expected=entry["sha256"], actual=sha(path),
                                           match=sha(path) == entry["sha256"])
    if not checks["manifest_sha256_match"] or not all(v["match"] for v in checks["frozen_files"].values()):
        write(TASK / "results" / "identity-failure.json", checks)
        print("STOP: frozen identity mismatch", file=sys.stderr)
        return 3

    raw_config = json.loads((FROZEN / "config.json").read_text(encoding="utf-8"))
    original_workload = json.loads((FROZEN / "workload.json").read_text(encoding="utf-8"))
    parent_plan = json.loads((FROZEN / "binding_plan.json").read_text(encoding="utf-8"))
    requests = [dict(r) for r in original_workload["requests"][:N]]
    expected = [
        ("azure-conv-000000000", 0.0, 374, 44, "p0", "d3"),
        ("azure-conv-000000001", 4.314579, 396, 109, "p1", "d14"),
        ("azure-conv-000000002", 4.541877, 879, 55, "p2", "d7"),
        ("azure-conv-000000003", 4.710427, 91, 16, "p3", "d9"),
        ("azure-conv-000000004", 5.892655, 91, 16, "p4", "d13"),
        ("azure-conv-000000005", 6.311529, 381, 84, "p5", "d11"),
        ("azure-conv-000000006", 7.745497, 1313, 142, "p6", "d4"),
        ("azure-conv-000000007", 8.251431, 388, 84, "p7", "d5"),
    ]
    assert len(requests) == N
    for row, (rid, arrival, tin, tout, p, d) in zip(requests, expected):
        assert row["request_id"] == rid and row["arrival_seconds"] == arrival
        assert row["input_tokens"] == tin and row["output_tokens"] == tout

    # ---- A1: config with ONLY the three permitted scalar changes -------------
    config = json.loads(json.dumps(raw_config))
    config["evaluation"]["window_start_seconds"] = 0.0
    config["evaluation"]["window_end_seconds"] = 30.0
    config["runtime"]["time_limit_seconds"] = 30.0
    scalar_diff = diff(config, raw_config)
    assert len(scalar_diff) == 3, scalar_diff
    write(INPUTS / "config.json", config)
    # diagnostic-root config: additionally relocate the two portable input paths
    diag_config = json.loads(json.dumps(config))
    diag_config["runtime"]["workload_path"] = "inputs/workload.json"
    diag_config["feedback"]["compute_profile_path"] = "inputs/compute_profile.json"
    diag_diff = diff(diag_config, raw_config)
    assert len(diag_diff) == 5, diag_diff
    write(DIAG / "config.json", diag_config)

    # ---- byte-identical frozen profile; exact prefix workload ----------------
    shutil.copyfile(FROZEN / "compute_profile.json", INPUTS / "compute_profile.json")
    workload = dict(original_workload)
    workload["requests"] = requests
    write(INPUTS / "workload.json", workload)

    # ---- A1: binding plan = projection of the T001 parent plan ---------------
    from joint_scheduling.config import load_config
    from joint_scheduling.binding.static import binding_plan_document
    cfg = load_config(str(INPUTS / "config.json"))
    plan = binding_plan_document(requests, cfg.topology, cfg.binding)
    parent_by_id = {row["request_id"]: row for row in parent_plan["bindings"]}
    projection = []
    for row in plan["bindings"]:
        parent = parent_by_id[row["request_id"]]
        projection.append(dict(request_id=row["request_id"],
                               parent=dict(parent), child=dict(row),
                               endpoints_equal=(row["prefill_instance_id"] == parent["prefill_instance_id"]
                                                and row["decode_instance_id"] == parent["decode_instance_id"]),
                               full_row_equal=(row == parent)))
    assert all(p["endpoints_equal"] and p["full_row_equal"] for p in projection)
    assert all(row["prefill_instance_id"] == p and row["decode_instance_id"] == d
               for row, (_, _, _, _, p, d) in zip(plan["bindings"], expected))
    assert "custom" not in plan and set(plan.keys()) == {
        "schema_version", "policy", "seed", "request_order", "selection_time",
        "prefill_selection", "decode_selection", "bindings_sha256", "request_count",
        "prefill_counts", "decode_counts", "cross_leaf_requests", "cross_leaf_fraction", "bindings"}
    write(INPUTS / "binding_plan.json", plan)

    # copy the canonical inputs into the diagnostic root (replay() layout)
    for name in ("compute_profile.json", "workload.json", "binding_plan.json"):
        shutil.copyfile(INPUTS / name, DIAG / "inputs" / name)

    input_hashes = {name: sha(INPUTS / name) for name in
                    ("config.json", "workload.json", "compute_profile.json", "binding_plan.json")}
    sample_manifest = dict(
        schema_version=1, task_id="T003",
        baseline="T001-baseline-freeze/results/frozen",
        frozen_manifest_sha256=manifest_hash,
        frozen_file_hashes=frozen,
        parent_binding_plan_sha256=frozen["binding_plan.json"],
        parent_bindings_content_sha256=parent_plan["bindings_sha256"],
        input_selection=dict(method="first N requests of the frozen workload in original order",
                             n=N, time_shift_seconds=0.0, duplication=False,
                             provenance_note=("workload.provenance is copied verbatim from the frozen full "
                                              "19366-request source; it describes the original full trace, "
                                              "NOT the 8 requests used in this task")),
        requests=[dict(request_id=r["request_id"], arrival_seconds=r["arrival_seconds"],
                       input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                       parent_prefill=parent_by_id[r["request_id"]]["prefill_instance_id"],
                       parent_decode=parent_by_id[r["request_id"]]["decode_instance_id"])
                  for r in requests],
        actual_statistics=dict(request_count=len(requests),
                               input_tokens=sum(r["input_tokens"] for r in requests),
                               output_tokens=sum(r["output_tokens"] for r in requests),
                               decode_token_steps=sum(r["output_tokens"] - 1 for r in requests),
                               unique_decode_instances=sorted({row["decode_instance_id"] for row in plan["bindings"]}),
                               unique_prefill_instances=sorted({row["prefill_instance_id"] for row in plan["bindings"]})),
        expected_by_task=dict(prefill_chunks=11, query_tokens=3913, decode_steps=542,
                              layers_per_step=61, ep_calls=(11 + 542) * 58,
                              kv_logical_payload_bytes=3913 * 70272, kv_messages=11 * 61),
        binding_projection=projection,
        config_diff_vs_frozen=scalar_diff,
        diagnostic_config_diff_vs_frozen=diag_diff,
        input_hashes=input_hashes,
        diagnostic_root="results/diagnostic",
        diagnostic_config_hash=sha(DIAG / "config.json"),
    )
    write(INPUTS / "sample-manifest.json", sample_manifest)
    write(TASK / "results" / "identity-check.json", checks)

    print(json.dumps(dict(status="inputs_ready", request_count=len(requests),
                          input_tokens=sample_manifest["actual_statistics"]["input_tokens"],
                          output_tokens=sample_manifest["actual_statistics"]["output_tokens"],
                          decode_steps=sample_manifest["actual_statistics"]["decode_token_steps"],
                          config_diff=[d["path"] for d in scalar_diff],
                          diag_config_diff=[d["path"] for d in diag_diff],
                          plan_sha256=plan["bindings_sha256"],
                          frozen_manifest_match=True), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
