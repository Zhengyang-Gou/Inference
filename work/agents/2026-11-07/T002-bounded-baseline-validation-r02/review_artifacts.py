"""Commander read-only artifact review. Never starts a simulator."""
import csv
import hashlib
import json
import math
import tarfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from joint_scheduling.config import load_config

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02"
FROZEN = ROOT / "outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen"
OLD = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation"
checks, runs = [], {}
def read(p):
    return json.loads(p.read_text(encoding="utf-8"))
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
def csvrows(p):
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
def check(name, condition, detail=None):
    checks.append(dict(check=name, passed=bool(condition), detail=detail))
def diffs(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = {}
        for key in sorted(a.keys() | b.keys()):
            path = prefix + ("." if prefix else "") + key
            if key not in a or key not in b:
                out[path] = [a.get(key), b.get(key)]
            else:
                out.update(diffs(a[key], b[key], path))
        return out
    return {} if a == b else {prefix: [a, b]}

expected_hashes = {
    "config.json": "565572ab4d2dde71bd265bfc8a240f7b433bf5987886505c231ea9a8f049a8f1",
    "compute_profile.json": "374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554",
    "workload.json": "0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346",
    "binding_plan.json": "9a48e35f53a4e8df2c9c09312a1fbf402bf8e79c2d47f083ba12e9416d37b091",
    "manifest.json": "811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78",
}
check("T001_frozen_hashes", all(sha(FROZEN / f) == h for f,h in expected_hashes.items()))
inputs = TASK / "inputs"
check("inputs_equal_blocked_round", all(sha(p) == sha(OLD / "inputs" / p.name) for p in inputs.glob("*.json")))
workload = read(inputs / "workload.json")
check("requests_exact_parent_prefix", workload["requests"] == read(FROZEN / "workload.json")["requests"][:3])
check("profile_exact_T001", sha(inputs / "compute_profile.json") == expected_hashes["compute_profile.json"])
config_diff = diffs(read(FROZEN / "config.json"), read(inputs / "config.json"))
check("only_three_permitted_config_changes", config_diff == {
    "evaluation.window_start_seconds": [60.0, 0.0],
    "evaluation.window_end_seconds": [3501.721937001, 30.0],
    "runtime.time_limit_seconds": [4101.721937001, 30.0]}, config_diff)
plan = read(inputs / "binding_plan.json")
parent = {r["request_id"]: r for r in read(FROZEN / "binding_plan.json")["bindings"]}
check("bindings_exact_parent_projection", all(row == parent[row["request_id"]] for row in plan["bindings"]) and len(plan["bindings"]) == 3)
check("execution_git_status_unchanged", (TASK / "logs/baseline-status.txt").read_bytes() == (TASK / "logs/final-status.txt").read_bytes())
check("original_T002_still_blocked", read(OLD / "STATUS.json")["status"] == "blocked")
normalized = json.loads(json.dumps(asdict(load_config(str(inputs / "config.json"))), default=str))
normalized.pop("config_path")
normalized["feedback"]["compute_profile_path"] = "compute_profile.json"
normalized["runtime"]["workload_path"] = "workload.json"
normalized["evaluation"]["output_dir"] = "result"
expect_tokens = {r["request_id"]: r["output_tokens"] for r in workload["requests"]}
for name in ("run-a", "run-b"):
    base = TASK / "results" / name
    req = csvrows(base / "result/requests.csv")
    tok = csvrows(base / "result/tokens.csv")
    events = [json.loads(x) for x in (base / "events.jsonl").read_text().splitlines() if x.strip()]
    hook = read(TASK / "results" / (name + "-hooks.json"))
    summary = read(base / "result/summary.json")
    ep = read(base / "expert_parallel_summary.json")
    obs = read(base / "observation/summary.json")
    resources = read(base / "final_resources.json")
    repro = read(base / "reproducibility.json")
    source = read(base / "source/manifest.json")
    backend = read(base / "network/backend_metadata.json")
    check(name + ".run_config_matches_normalized_input", read(base / "config.json") == normalized, diffs(normalized, read(base / "config.json")))
    check(name + ".input_exports_unchanged", all(sha(base / f) == sha(inputs / f) for f in ("compute_profile.json","workload.json")) and read(base / "binding_plan.json") == plan)
    check(name + ".repro_input_hashes", all(repro["input_sha256"][key] == sha(base / filename) for key,filename in {
        "config":"config.json","profile":"compute_profile.json","workload":"workload.json","binding_plan":"binding_plan.json"}.items()))
    check(name + ".all_requests_completed", len(req)==3 and set(x["request_id"] for x in req)==set(expect_tokens) and all(x["status"]=="completed" for x in req)
          and summary["arrival_cohort"]["failed_count"]==summary["arrival_cohort"]["incomplete_count"]==0)
    check(name + ".all_token_counts", len(tok)==208 and dict(Counter(x["request_id"] for x in tok))==expect_tokens)
    per_request = []
    order_checks = []
    for row in req:
        rid = row["request_id"]
        tokens = [x for x in tok if x["request_id"]==rid]
        times = [float(x["returned_at_seconds"]) for x in tokens]
        check(name + "." + rid + ".token_order", [int(x["token_index"]) for x in tokens]==list(range(1,expect_tokens[rid]+1))
            and all(a<=b for a,b in zip(times,times[1:])))
        arrival = float(row["arrived_at_seconds"])
        computed = {
            "ttft_seconds":times[0]-arrival,
            "handoff_seconds":times[1]-times[0],
            "later_max_itl_seconds":max(b-a for a,b in zip(times[1:],times[2:])),
            "e2e_seconds":times[-1]-arrival}
        check(name + "." + rid + ".CSV_metrics_recomputed", all(math.isclose(v,float(row[k]),abs_tol=1e-12) for k,v in computed.items())
              and math.isclose(times[-1],float(row["completed_at_seconds"]),abs_tol=1e-12))
        slo = computed["ttft_seconds"]<=float(row["ttft_slo_seconds"]) and max(b-a for a,b in zip(times,times[1:]))<=float(row["itl_slo_seconds"])
        check(name + "." + rid + ".SLO_recomputed", slo == (row["slo_met"]=="True"))
        byname = {key: [e for e in events if e["event"]==key and e.get("request_id")==rid] for key in ("prefill_started","kv_all_received","decode_started")}
        p,k,d = (byname[x][0]["time_seconds"] for x in ("prefill_started","kv_all_received","decode_started"))
        # These inequalities do NOT independently identify Prefill completion or first Decode compute.
        order_checks.append(p <= times[0] <= k <= d <= times[1])
        per_request.append(dict(request_id=rid,**computed))
    check(name + ".observable_coarse_boundary_order", all(order_checks))
    check(name + ".goodput_recomputed", summary["completion_window"]["completed_count"]==3 and summary["completion_window"]["slo_met_count"]==3
          and summary["completion_window"]["slo_goodput_rps"]==3/30)
    check(name + ".EP_route_ACK_audit", hook["ep_flow_audit"]["status"]=="passed" and hook["ep_flow_audit"]["checked_logical_flows"]==141968
          and ep["started_calls"]==ep["completed_calls"]==12122
          and all(hook["ep_flow_audit"][k]==0 for k in ("incomplete_calls","active_logical_flows","active_compute_steps","active_decode_batches")))
    check(name + ".EP_active_tasks_empty", all(ep[k]=={} for k in ("active_calls_at_end","active_gpu_tasks_at_end","queued_gpu_tasks_at_end","planned_phase_demands_at_end")))
    check(name + ".KV_logical_payload_and_messages", obs["communication"]["kv"]["started_bytes"]==obs["communication"]["kv"]["completed_bytes"]==1649*70272
          and obs["communication"]["kv"]["started_count"]==obs["communication"]["kv"]["completed_count"]==244)
    check(name + ".resources_all_zero",len(resources)==32 and all(x["occupied_bytes"]==x["reserved_bytes"]==0 for x in resources.values()))
    check(name + ".ns3_identity_and_end_time", backend["backend"]=="simai_ns3_rdma" and backend["protocol_version"]==4
          and backend["build_fingerprint"]=="23ec96146135d0706e39" and read(base / "network/backend_rpc_summary.json")["time_ns"]==30000000000)
    archive = base / "source/source.tar.gz"
    check(name + ".source_archive_hash", sha(archive)==source["archive_sha256"]==repro["source"]["archive_sha256"])
    check(name + ".source_tree_hash", hashlib.sha256(json.dumps(source["files"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
          ==source["source_tree_sha256"]==repro["source"]["source_tree_sha256"])
    with tarfile.open(archive) as tar:
        entries = {m.name:m for m in tar.getmembers()}
        bad = []
        for f in source["files"]:
            path = f["path"]
            if f.get("missing"):
                if path in entries: bad.append(path)
                continue
            member = entries.get(path)
            if member is None: bad.append(path); continue
            data = member.linkname.encode() if f.get("symlink") else tar.extractfile(member).read()
            if hashlib.sha256(data).hexdigest()!=f["sha256"]: bad.append(path)
        check(name + ".source_archive_file_inventory", not bad,dict(files=len(source["files"]),mismatches=bad))
    corefiles = [f for f in source["files"] if f["path"].startswith("joint_scheduling/") and not f.get("missing")]
    check(name + ".current_core_source_matches_snapshot", all(sha(ROOT/f["path"])==f["sha256"] for f in corefiles),len(corefiles))
    check(name + ".prefill_counts",read(base/"prefill_chunk_summary.json")["completed_chunks"]==4
          and read(base/"prefill_batch_summary.json")["total_token_histogram"]=={"374":1,"396":1,"512":1,"367":1})
    check(name + ".decode_counts",read(base/"decode_batch_summary.json")["completed_batches"]==205 and read(base/"decode_batch_summary.json")["layers_per_step"]==61)
    runs[name] = dict(events=events,tokens=tok,requests=req,summary=summary,ep=ep,observation=obs,resources=resources,
                     binding=read(base/"binding_plan.json"),prefill=read(base/"prefill_batch_summary.json"),decode=read(base/"decode_batch_summary.json"),
                     hook_audit=hook["ep_flow_audit"],hook_counts=hook["hooks_events"]["event_counts"])
    runs[name]["review_detail"] = dict(per_request=per_request,essential_event_counts=dict(Counter(x["event"] for x in events)),
        wall_clock_seconds=hook["wall_clock_seconds"],source_tree_sha256=source["source_tree_sha256"],
        excluded_by_executor_compute_queue_wait_seconds=ep["compute_queue_wait_seconds"],
        omitted_event_counts=summary["recording"]["omitted_event_counts"])

a,b=runs["run-a"],runs["run-b"]
for field in ("events","tokens","requests","ep","observation","resources","binding","prefill","decode","hook_audit","hook_counts"):
    check("reproducibility."+field,a[field]==b[field])
for field in ("completion_window","arrival_cohort","latencies"):
    check("reproducibility.summary."+field,a["summary"][field]==b["summary"][field])

gaps = [
    dict(id="A3-KV",requirement="Every KV transfer starts after corresponding chunk/layer data is ready, and ACK completion releases dependencies.",
         evidence="Only counts of prefill_chunk_layer_ready/transfer events retained; EPFlowAudit ignores KV transfers. No per-KV boundary checks or records."),
    dict(id="A3-PD",requirement="First token follows complete Prefill; first Decode compute follows Prefill completion and ALL KV.",
         evidence="Checker compares prefill_started, not prefill completion; event counter retains no completed-chunk/first-compute timestamps."),
    dict(id="A3-token",requirement="Each returned Decode token follows all 61 corresponding layer dependencies.",
         evidence="EPFlowAudit verifies layers before decode_batch_completed but sees no recorder.token_returned event. No batch-completion/layer-60 timestamps retained for CSV cross-check.")
]
report = dict(task="T002-r02",reviewer="commander",reviewed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    method="Read-only artifact, snapshot and checker review; no new simulation.",checks_total=len(checks),passed=sum(c["passed"] for c in checks),
    failed=[c for c in checks if not c["passed"]],checks=checks,
    run_details={name:r["review_detail"] for name,r in runs.items()},uncovered_required_evidence=gaps,
    checker_findings=[
        "A1 is unconditionally passed; t001_ok/projection_ok are only formatted into text and config_diff is trusted from manifest.",
        "layer_ready_precedes_transfers compares counts, not causality or timestamps.",
        "decode_admission_after_prefill_and_all_kv uses prefill_started; it does not check Prefill completion or first Decode compute.",
        "hooks_are_observational_only merely checks completed status/event_total; source review supplies observational confirmation.",
        "A5 excludes compute_queue_wait_seconds, which is simulated timing; independent comparison includes it and found exact equality."
    ])
out = TASK/"results/review-verification.json"
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps(dict(checks_total=report["checks_total"],passed=report["passed"],failed=report["failed"],uncovered_required_evidence=len(gaps),
  source_tree_sha256=runs["run-a"]["review_detail"]["source_tree_sha256"],essential_event_counts=runs["run-a"]["review_detail"]["essential_event_counts"]),ensure_ascii=False,indent=2))
