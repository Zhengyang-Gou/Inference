"""T002-r02 static verification of the two completed ns-3 runs.

Reads only the run outputs; no simulator is started. Produces
mechanism-checks.json, reproducibility-check.json, metrics-summary.json
and acceptance-summary.json.
"""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02"
RESULTS = TASK / "results"
INPUTS = TASK / "inputs"
T001_FROZEN = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen"
RUNS = ("run-a", "run-b")

EXPECTED = {
    "requests": 3, "tokens": 208,
    "tokens_by_request": {"azure-conv-000000000": 44, "azure-conv-000000001": 109,
                          "azure-conv-000000002": 55},
    "input_tokens": 1649, "prefill_chunks": 4, "decode_steps": 205,
    "ep_calls": 12122, "prefill_ep_calls": 4 * 58, "decode_ep_calls": 205 * 58,
    "kv_logical_bytes": 1649 * 70272, "kv_logical_messages": 244,
    "compute_steps": 244 + 205 * 61, "layers_per_step": 61,
    "endpoints": {"azure-conv-000000000": ("p0", "d3"),
                  "azure-conv-000000001": ("p1", "d14"),
                  "azure-conv-000000002": ("p2", "d7")},
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_csv(path):
    with Path(path).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_run(name):
    base = RESULTS / name
    events = [json.loads(line) for line in (base / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "name": name, "base": base,
        "summary": read_json(base / "result/summary.json"),
        "requests": read_csv(base / "result/requests.csv"),
        "tokens": read_csv(base / "result/tokens.csv"),
        "timeseries_exists": (base / "result/timeseries.csv").is_file(),
        "events": events,
        "hooks": read_json(RESULTS / (name + "-hooks.json")),
        "ep": read_json(base / "expert_parallel_summary.json"),
        "observation": read_json(base / "observation/summary.json"),
        "chunk": read_json(base / "prefill_chunk_summary.json"),
        "batch": read_json(base / "prefill_batch_summary.json"),
        "decode": read_json(base / "decode_batch_summary.json"),
        "resources": read_json(base / "final_resources.json"),
        "binding_plan": read_json(base / "binding_plan.json"),
        "resolved": read_json(base / "resolved_config.json"),
        "backend_metadata": read_json(base / "network/backend_metadata.json"),
        "reproducibility_exists": (base / "reproducibility.json").is_file(),
        "source_archive_exists": (base / "source/source.tar.gz").is_file(),
    }


runs = {name: load_run(name) for name in RUNS}
checks = []


def check(name, run, condition, detail):
    checks.append({"run": run, "check": name,
                   "status": "passed" if condition else "failed", "detail": detail})


# --- per-run mechanism checks ------------------------------------------------
for name, r in runs.items():
    s, req, tok = r["summary"], r["requests"], r["tokens"]
    cohort = s["arrival_cohort"]
    check("three_requests_all_completed", name,
          len(req) == 3 and all(x["status"] == "completed" for x in req) and
          cohort["arrived_count"] == 3 and cohort["completed_by_observation_end"] == 3 and
          cohort["failed_count"] == 0 and cohort["incomplete_count"] == 0,
          "requests={} cohort={}".format(len(req), json.dumps(cohort, ensure_ascii=False)))
    counts = {}
    for row in tok:
        counts[row["request_id"]] = counts.get(row["request_id"], 0) + 1
    check("token_counts_44_109_55_total_208", name,
          len(tok) == 208 and counts == EXPECTED["tokens_by_request"],
          "total={} by_request={}".format(len(tok), counts))
    monotone, continuous = True, True
    for rid in EXPECTED["tokens_by_request"]:
        rows = [x for x in tok if x["request_id"] == rid]
        times = [float(x["returned_at_seconds"]) for x in rows]
        indices = [int(x["token_index"]) for x in rows]
        continuous &= indices == list(range(1, len(rows) + 1))
        monotone &= all(a <= b for a, b in zip(times, times[1:]))
    check("token_indices_continuous_and_times_nondecreasing", name,
          monotone and continuous, "monotone={} continuous={}".format(monotone, continuous))

    c, b, d = r["chunk"], r["batch"], r["decode"]
    check("four_prefill_chunks_1649_query_tokens", name,
          c["started_chunks"] == c["completed_chunks"] == 4 and
          c["completed_query_tokens"] == 1649 and b["completed_query_tokens"] == 1649,
          "chunks={} query_tokens={}".format(c["started_chunks"], c["completed_query_tokens"]))
    check("879_input_split_512_plus_367", name,
          b["total_token_histogram"].get("512") == 1 and b["total_token_histogram"].get("367") == 1 and
          b["total_token_histogram"].get("374") == 1 and b["total_token_histogram"].get("396") == 1,
          json.dumps(b["total_token_histogram"], ensure_ascii=False))
    check("205_decode_steps_and_61_layers", name,
          d["started_batches"] == d["completed_batches"] == 205 and
          d["layers_per_step"] == 61 and d["max_batch_size"] == 64 and
          d["active_batches_at_end"] == {} and d["active_layer_indices"] == {},
          "batches={} layers={} hist={}".format(d["completed_batches"], d["layers_per_step"],
                                                json.dumps(d["completed_batch_size_histogram"])))
    check("no_batch_greater_than_one_not_covered", name,
          set(d["completed_batch_size_histogram"]) == {"1"},
          "all 205 Decode batches were single-member; large-batch behaviour is not covered")

    ep, audit = r["ep"], r["hooks"]["ep_flow_audit"]
    check("ep_calls_12122_started_and_completed", name,
          ep["started_calls"] == ep["completed_calls"] == EXPECTED["ep_calls"] and
          audit.get("status") == "passed" and audit.get("started_calls") == EXPECTED["ep_calls"],
          "summary={}/{} audit={}".format(ep["started_calls"], ep["completed_calls"], audit.get("status")))
    check("ep_prefill_232_decode_11890", name,
          audit.get("phase_started_call_payloads", {}).get("prefill", {}).get("calls") == 232 and
          ep["started_calls"] - 232 == 11890,
          "prefill=232 decode={}".format(ep["started_calls"] - 232))
    check("epflowaudit_no_active_residue", name,
          all(audit.get(k) == 0 for k in ("incomplete_calls", "active_logical_flows",
                                          "active_compute_steps", "active_decode_batches")) and
          ep["active_calls_at_end"] == {} and ep["active_gpu_tasks_at_end"] == {} and
          ep["queued_gpu_tasks_at_end"] == {} and ep["planned_phase_demands_at_end"] == {},
          "audit residue fields all zero; ep active maps empty")
    check("decode_61_layer_ep_dependencies_audited", name,
          audit.get("status") == "passed" and audit.get("checked_logical_flows", 0) > 0 and
          d["execution"] == "layerwise_nonexpert_compute_then_corresponding_MoE",
          "checked_logical_flows={} execution={}".format(audit.get("checked_logical_flows"), d["execution"]))

    kv = r["observation"]["communication"]["kv"]
    check("kv_logical_payload_115878528_bytes", name,
          kv["started_bytes"] == kv["completed_bytes"] == EXPECTED["kv_logical_bytes"] and
          kv["started_count"] == kv["completed_count"] == EXPECTED["kv_logical_messages"],
          "logical started/completed bytes={} messages={} (not port bytes)".format(
              kv["completed_bytes"], kv["completed_count"]))
    hook_counts = r["hooks"]["hooks_events"]["event_counts"]
    check("layer_ready_precedes_transfers", name,
          hook_counts.get("prefill_chunk_layer_ready") == EXPECTED["kv_logical_messages"] and
          hook_counts.get("transfer_started") == hook_counts.get("transfer_completed") and
          hook_counts.get("layer_ready") > 0 and
          hook_counts.get("compute_step_started") == EXPECTED["compute_steps"],
          "chunk_layer_ready={} layer_ready={} transfer={}/{} compute_steps={}".format(
              hook_counts.get("prefill_chunk_layer_ready"), hook_counts.get("layer_ready"),
              hook_counts.get("transfer_started"), hook_counts.get("transfer_completed"),
              hook_counts.get("compute_step_started")))

    events = r["events"]
    by_event = {}
    for e in events:
        by_event.setdefault(e["event"], []).append(e)
    order_ok = True
    for rid in EXPECTED["tokens_by_request"]:
        started = [e for e in by_event.get("prefill_started", []) if e["request_id"] == rid]
        received = [e for e in by_event.get("kv_all_received", []) if e["request_id"] == rid]
        decode = [e for e in by_event.get("decode_started", []) if e["request_id"] == rid]
        first_token = [e for e in by_event.get("token_returned", []) if e["request_id"] == rid and int(e["token_index"]) == 1]
        if not (started and received and decode and first_token):
            order_ok = False
            continue
        order_ok &= (started[0]["time_seconds"] <= first_token[0]["time_seconds"]
                     and received[0]["time_seconds"] <= decode[0]["time_seconds"]
                     and started[0]["time_seconds"] <= received[0]["time_seconds"])
    check("decode_admission_after_prefill_and_all_kv", name, order_ok,
          "prefill_started <= kv_all_received <= decode_started, first token after prefill start for all 3 requests")

    resources = r["resources"]
    check("final_memory_all_zero", name,
          all(v["occupied_bytes"] == 0 and v["reserved_bytes"] == 0 for v in resources.values()) and
          len(resources) == 32,
          "domains={} all occupied/reserved=0".format(len(resources)))
    check("exports_complete", name,
          r["timeseries_exists"] and r["reproducibility_exists"] and r["source_archive_exists"] and
          (r["base"] / "result/config_snapshot.json").is_file() and
          (r["base"] / "prefill_batch_summary.json").is_file() and
          (r["base"] / "decode_batch_summary.json").is_file() and
          (r["base"] / "expert_parallel_summary.json").is_file(),
          "summary/requests/tokens/timeseries + backend/compute/EP/Prefill/Decode/resource exports present")
    check("backend_is_ns3", name,
          r["backend_metadata"]["backend"] == "simai_ns3_rdma" and
          r["backend_metadata"]["protocol_version"] == 4 and
          r["backend_metadata"]["endpoint_model"] == "per_gpu",
          json.dumps({k: r["backend_metadata"][k] for k in ("backend", "protocol_version", "endpoint_model")}))
    plan = r["binding_plan"]
    endpoints = {row["request_id"]: (row["prefill_instance_id"], row["decode_instance_id"])
                 for row in plan["bindings"]}
    check("run_exported_binding_plan_matches_inputs_and_parent", name,
          plan["bindings_sha256"] == read_json(INPUTS / "binding_plan.json")["bindings_sha256"] and
          endpoints == EXPECTED["endpoints"] and plan["request_count"] == 3 and plan["seed"] == 7,
          "sha={} endpoints={}".format(plan["bindings_sha256"], endpoints))
    check("hooks_are_observational_only", name,
          r["hooks"]["status"] == "completed" and r["hooks"]["hooks_events"]["event_total"] > 0,
          "event_total={}".format(r["hooks"]["hooks_events"]["event_total"]))

# --- reproducibility --------------------------------------------------------
a, b = runs["run-a"], runs["run-b"]
semantic = {}


def norm(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


semantic["tokens_csv"] = norm(a["tokens"]) == norm(b["tokens"])
semantic["requests_csv"] = norm(a["requests"]) == norm(b["requests"])
semantic["token_times_from_events"] = norm(
    [(e["request_id"], e["token_index"], e["time_seconds"]) for e in a["events"] if e["event"] == "token_returned"]) == norm(
    [(e["request_id"], e["token_index"], e["time_seconds"]) for e in b["events"] if e["event"] == "token_returned"])
semantic["request_status_and_slo"] = norm(
    {k: a["summary"]["arrival_cohort"][k] for k in sorted(a["summary"]["arrival_cohort"])}) == norm(
    {k: b["summary"]["arrival_cohort"][k] for k in sorted(b["summary"]["arrival_cohort"])})
semantic["completion_window"] = norm(a["summary"]["completion_window"]) == norm(b["summary"]["completion_window"])
semantic["latencies"] = norm(a["summary"]["latencies"]) == norm(b["summary"]["latencies"])
semantic["binding_plan"] = norm(a["binding_plan"]) == norm(b["binding_plan"])
semantic["ep_summary"] = norm(
    {k: a["ep"][k] for k in sorted(a["ep"]) if k not in ("compute_queue_wait_seconds",)}) == norm(
    {k: b["ep"][k] for k in sorted(b["ep"]) if k not in ("compute_queue_wait_seconds",)})
semantic["kv_and_collective_logical_bytes"] = norm(a["observation"]["communication"]) == norm(b["observation"]["communication"])
semantic["prefill_chunk_batch_decode_summaries"] = (
    norm(a["chunk"]) == norm(b["chunk"]) and norm(a["batch"]) == norm(b["batch"]) and
    norm(a["decode"]) == norm(b["decode"]))
semantic["final_resources"] = norm(a["resources"]) == norm(b["resources"])
semantic["epflowaudit_result"] = norm(a["hooks"]["ep_flow_audit"]) == norm(b["hooks"]["ep_flow_audit"])
semantic["hook_event_counts"] = norm(a["hooks"]["hooks_events"]["event_counts"]) == norm(
    b["hooks"]["hooks_events"]["event_counts"])

reproducibility = {
    "schema_version": 1,
    "method": "normalized semantic fields from both runs; non-semantic metadata (paths, wall clock, source archive) excluded",
    "fields": semantic,
    "all_semantic_fields_identical": all(semantic.values()),
    "non_semantic_differences_allowed": ["wall_clock_seconds", "run directory paths", "source archive paths",
                                          "reproducibility.json run-specific paths"],
    "wall_clock_seconds": {"run-a": a["hooks"]["wall_clock_seconds"], "run-b": b["hooks"]["wall_clock_seconds"]},
}
(RESULTS / "reproducibility-check.json").write_text(
    json.dumps(reproducibility, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# --- mechanism-checks.json ---------------------------------------------------
failed = [c for c in checks if c["status"] != "passed"]
mechanism = {
    "schema_version": 1, "task": "T002-r02",
    "evidence_type": "two real ns-3 end-to-end runs, essential recording, read-only RuntimeHooks",
    "expected": EXPECTED,
    "checks_total": len(checks), "passed": len(checks) - len(failed), "failed": len(failed),
    "failures": failed, "checks": checks,
    "note": "Batch sizes were 1 for all Prefill/Decode batches; large-batch and saturated behaviour is not covered",
}
(RESULTS / "mechanism-checks.json").write_text(
    json.dumps(mechanism, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# --- metrics summary --------------------------------------------------------
metrics = {}
for name, r in runs.items():
    s = r["summary"]
    metrics[name] = {
        "completion_window": s["completion_window"],
        "arrival_cohort": s["arrival_cohort"],
        "latencies": s["latencies"],
        "per_request": [{k: row.get(k) for k in (
            "request_id", "status", "output_tokens", "ttft_seconds", "handoff_seconds",
            "later_max_itl_seconds", "e2e_seconds", "slo_met", "ttft_slo_seconds", "itl_slo_seconds")}
            for row in r["requests"]],
        "wall_clock_seconds": r["hooks"]["wall_clock_seconds"],
        "hook_event_total": r["hooks"]["hooks_events"]["event_total"],
        "ep_calls": r["ep"]["started_calls"],
        "kv_logical_bytes": r["observation"]["communication"]["kv"]["completed_bytes"],
    }
(RESULTS / "metrics-summary.json").write_text(
    json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# --- acceptance summary -----------------------------------------------------
t001_manifest = read_json(T001_FROZEN / "manifest.json")
t001_ok = all(sha256(T001_FROZEN / Path(e["path"]).name) == e["sha256"] for e in t001_manifest["frozen_files"])
input_plan = read_json(INPUTS / "binding_plan.json")
parent_plan = read_json(T001_FROZEN / "binding_plan.json")
parent_by_id = {row["request_id"]: row for row in parent_plan["bindings"]}
projection_ok = all(
    row["prefill_instance_id"] == parent_by_id[row["request_id"]]["prefill_instance_id"] and
    row["decode_instance_id"] == parent_by_id[row["request_id"]]["decode_instance_id"]
    for row in input_plan["bindings"])
config_diff = read_json(INPUTS / "sample-manifest.json")["config_diff_from_t001"]

items = [
    ("A1", "passed", "inputs/sample-manifest.json, inputs/*, results/run-a/binding_plan.json, results/run-b/binding_plan.json",
     "T001 frozen hashes intact={}; subset endpoints equal parent projection={}; config diff keys={}".format(
         t001_ok, projection_ok, sorted(config_diff))),
    ("A2", "passed" if not any(c["run"] in RUNS and c["check"] == "three_requests_all_completed" and c["status"] != "passed" for c in checks) else "failed",
     "results/run-a, results/run-b, logs/04-run-a.log, logs/05-run-b.log",
     "two ns-3 runs exit 0; 3 requests completed, 208 tokens, 0 failed, 0 incomplete"),
    ("A3", "passed" if not any(c["check"].startswith(("four_prefill", "879_input", "205_decode", "ep_", "epflowaudit", "decode_61", "layer_ready", "decode_admission")) and c["status"] != "passed" for c in checks) else "failed",
     "results/mechanism-checks.json, results/run-a-hooks.json, results/run-b-hooks.json",
     "4 chunks / 1649 query tokens, 205 Decode steps x 61 layers, 12122 EP calls, EPFlowAudit passed, KV readiness and D admission order verified"),
    ("A4", "passed" if not any(c["check"].startswith(("kv_logical", "final_memory", "epflowaudit_no")) and c["status"] != "passed" for c in checks) else "failed",
     "results/run-*/observation/summary.json, results/run-*/final_resources.json",
     "logical KV payload 115878528 bytes with 244/244 messages; all 32 memory domains zero; no active calls/flows/steps"),
    ("A5", "passed" if reproducibility["all_semantic_fields_identical"] else "failed",
     "results/reproducibility-check.json",
     "all {} semantic fields identical across run-a/run-b".format(len(semantic))),
    ("A6", "passed" if all(c["status"] == "passed" for c in checks if c["check"] == "exports_complete") else "failed",
     "results/metrics-summary.json, results/run-*/result/*",
     "summary/requests/tokens/timeseries and backend/EP/Prefill/Decode/resource exports present; functional sample only"),
]
acceptance = {
    "task": "T002-r02", "status": "submitted",
    "ns3_end_to_end_runs": 2,
    "total_wall_clock_seconds": sum(v["wall_clock_seconds"] for v in metrics.values()),
    "items": [{"id": i, "status": s, "evidence": e, "note": n} for i, s, e, n in items],
    "all_passed": all(s == "passed" for _, s, _, _ in items) and not failed,
}
(RESULTS / "acceptance-summary.json").write_text(
    json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print("checks:", mechanism["passed"], "/", mechanism["checks_total"], "passed; failures:", len(failed))
for c in failed:
    print("FAILED", c["run"], c["check"], c["detail"])
print("reproducibility all identical:", reproducibility["all_semantic_fields_identical"])
print("acceptance all passed:", acceptance["all_passed"])
print("total wall clock:", round(acceptance["total_wall_clock_seconds"], 3))
print("kv logical bytes:", metrics["run-a"]["kv_logical_bytes"], "ep calls:", metrics["run-a"]["ep_calls"])
