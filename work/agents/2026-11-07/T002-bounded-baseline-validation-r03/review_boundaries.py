"""Commander review: stronger static matching of existing r03 artifacts. No simulation."""
import csv, hashlib, json, tarfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path.cwd()
BASE=ROOT/"outputs/agents/2026-11-07"
TASK=BASE/"T002-bounded-baseline-validation-r03"
R02=BASE/"T002-bounded-baseline-validation-r02"
FROZEN=BASE/"T001-baseline-freeze/results/frozen"
RUN=TASK/"results/audit-run"
checks=[]
def load(p): return json.loads(p.read_text(encoding="utf-8"))
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def check(name, condition, detail=None): checks.append(dict(check=name,passed=bool(condition),detail=detail))
def ns(seconds): return round(float(seconds)*1e9)
def csvrows(p):
    with p.open(encoding="utf-8",newline="") as f: return list(csv.DictReader(f))
def eventrows(p): return [json.loads(s) for s in p.read_text().splitlines() if s.strip()]
def inventory(p): return {str(f.relative_to(p)):digest(f) for f in sorted(p.rglob("*")) if f.is_file()}

protected_before=inventory(R02)
frozen_hashes={
"config.json":"565572ab4d2dde71bd265bfc8a240f7b433bf5987886505c231ea9a8f049a8f1",
"compute_profile.json":"374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554",
"workload.json":"0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346",
"binding_plan.json":"9a48e35f53a4e8df2c9c09312a1fbf402bf8e79c2d47f083ba12e9416d37b091",
"manifest.json":"811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78"}
check("T001_frozen_unchanged",all(digest(FROZEN/f)==h for f,h in frozen_hashes.items()))
check("r03_inputs_equal_r02",len(list((TASK/"inputs").glob("*.json")))==5 and all(digest(f)==digest(R02/"inputs"/f.name) for f in (TASK/"inputs").glob("*.json")))
check("parent_task_preserved",digest(TASK/"TASK-parent.md")==digest(R02/"TASK.md"))
check("r02_historical_state_preserved",load(R02/"STATUS.json")["status"]=="changes_requested" and (R02/"REVIEW.md").is_file())
check("r02_report_and_acceptance_hashes",digest(R02/"REPORT.md")=="6409a54f839ed25be4831ad6287f1591f0ef45a0f8233023b7f481f2dcab87f7" and digest(R02/"results/acceptance-summary.json")=="12d28faef8c8b2b2f373440a9a9b3f2d6e358429ff1403e4889610f1e25f6f2d")
check("execution_git_status_unchanged",(TASK/"logs/baseline-status.txt").read_bytes()==(TASK/"logs/final-status.txt").read_bytes())
capture=load(TASK/"results/boundary-events.json")
rows=capture["hooks"]["captured_rows"]
def select(event): return [(i,r) for i,r in enumerate(rows) if r["event"]==event]
requests=load(TASK/"inputs/workload.json")["requests"]
byrid={r["request_id"]:r for r in requests}
plan=load(TASK/"inputs/binding_plan.json")
bindings={r["request_id"]:r for r in plan["bindings"]}
placements={p["instance_id"]:p for p in load(TASK/"inputs/config.json")["topology"]["placements"]}
check("capture_input_hashes",all(capture["inputs_sha256"][f]==digest(TASK/"inputs"/f) for f in capture["inputs_sha256"]))
check("capture_clock_exact_ns",all(r["time_ns"]==ns(r["time_seconds"]) for r in rows) and all(a["time_ns"]<=b["time_ns"] for a,b in zip(rows,rows[1:])))
observed=Counter(r["event"] for r in rows)
check("captured_counts",len(rows)==13844 and observed["prefill_chunk_layer_ready"]==244 and observed["compute_step_completed"]==12749 and observed["transfer_started"]==244 and observed["decode_batch_started"]==observed["decode_batch_completed"]==205)
check("EP_audit",capture["ep_flow_audit"]["status"]=="passed" and capture["ep_flow_audit"]["started_calls"]==capture["ep_flow_audit"]["completed_calls"]==12122 and capture["ep_flow_audit"]["checked_logical_flows"]==141968 and all(capture["ep_flow_audit"][k]==0 for k in ("incomplete_calls","active_logical_flows","active_compute_steps","active_decode_batches")))

# Match actual KV byte intervals, never zip unrelated events by time.
ready={}
duplicate_ready=[]
for i,r in select("prefill_chunk_layer_ready"):
    key=(r["request_id"],r["layer_id"],r["cached_tokens"]*1152)
    if key in ready: duplicate_ready.append(key)
    ready[key]=(i,r)
sent=set(); kv_pairs=[]; kv_bad=[]
for i,s in select("transfer_started"):
    key=(s["request_id"],s["layer_id"],s["offset_bytes"])
    match=ready.get(key)
    if match is None or key in sent:
        kv_bad.append(dict(key=key,reason="missing/duplicate interval")); continue
    j,r=match
    b=bindings[s["request_id"]]
    p,d=placements[b["prefill_instance_id"]],placements[b["decode_instance_id"]]
    ok=(s["transfer_kind"]=="kv" and j<i and r["time_ns"]<=s["time_ns"]
        and r["time_ns"]==ns(s["ready_at_seconds"]) and s["size_bytes"]==r["size_bytes"]
        and s["source_gpu_id"]==p["gpu_ids"][0] and s["destination_gpu_id"]==d["gpu_ids"][0])
    pair=dict(request_id=s["request_id"],layer_id=s["layer_id"],chunk_index=r["chunk_index"],offset_bytes=s["offset_bytes"],
              size_bytes=s["size_bytes"],ready_ns=r["time_ns"],sent_ns=s["time_ns"],capture_order_valid=j<i,passed=ok)
    kv_pairs.append(pair)
    if not ok: kv_bad.append(pair)
    sent.add(key)
check("B1_exact_interval_bijection_and_ready_order",not duplicate_ready and not kv_bad and set(ready)==sent and len(kv_pairs)==244,kv_bad)
check("B1_KV_payload",sum(x["size_bytes"] for x in kv_pairs)==115878528)

# Use batch IDs, membership and token context to identify every Decode step.
events=eventrows(RUN/"events.jsonl")
tokens={(r["request_id"],int(r["token_index"])):ns(r["returned_at_seconds"]) for r in csvrows(RUN/"result/tokens.csv")}
starts={r["batch_id"]:(i,r) for i,r in select("decode_batch_started")}
ends={r["batch_id"]:(i,r) for i,r in select("decode_batch_completed")}
completed=defaultdict(list)
for i,r in select("compute_step_completed"):
    if r["phase"]=="decode": completed[r["batch_id"]].append((i,r))
check("B3_batch_ID_sets",len(starts)==len(ends)==len(completed)==205 and set(starts)==set(ends)==set(completed))
batch_pairs=[]; batch_bad=[]; used_tokens=set()
for bid,(start_i,start) in starts.items():
    end_i,end=ends[bid]
    steps=completed[bid]
    members=start["request_ids"]
    ok=(len(members)==start["batch_size"]==1 and end["request_ids"]==members and end["instance_id"]==start["instance_id"]
        and [r["layer_id"] for i,r in steps]==list(range(61)) and start_i<steps[0][0] and steps[-1][0]<end_i
        and start["time_ns"]<=steps[0][1]["time_ns"]<=steps[-1][1]["time_ns"]<=end["time_ns"]
        and all(a[1]["time_ns"]<=b[1]["time_ns"] for a,b in zip(steps,steps[1:])))
    rid=members[0]; context=start["context_tokens"][0]
    token_index=context-byrid[rid]["input_tokens"]+2
    key=(rid,token_index)
    last=steps[-1][1]
    ok=(ok and all(r["request_id"]==rid and r["instance_id"]==start["instance_id"] for i,r in steps)
        and bindings[rid]["decode_instance_id"]==start["instance_id"]
        and key in tokens and key not in used_tokens and 2<=token_index<=byrid[rid]["output_tokens"]
        and last["time_ns"]<=tokens[key]<=end["time_ns"])
    pair=dict(batch_id=bid,request_id=rid,context_tokens=context,token_index=token_index,
              layer_count=len(steps),started_ns=start["time_ns"],layer60_completed_ns=last["time_ns"],
              token_ns=tokens.get(key),batch_completed_ns=end["time_ns"],passed=ok)
    batch_pairs.append(pair)
    if not ok: batch_bad.append(pair)
    used_tokens.add(key)
check("B3_context_to_token_bijection_full_61_layers",not batch_bad and used_tokens=={k for k in tokens if k[1]>=2} and len(used_tokens)==205,batch_bad)

# Verify every Prefill chunk, all layers, first token and initial Decode boundary.
p_pairs=[]; p_bad=[]
for rid,request in byrid.items():
    chunks=[(i,r) for i,r in select("prefill_chunk_completed") if r["request_id"]==rid]
    expected=[min(512,request["input_tokens"]-offset) for offset in range(0,request["input_tokens"],512)]
    ok=([r["chunk_index"] for i,r in chunks]==list(range(len(expected))) and [r["chunk_tokens"] for i,r in chunks]==expected
        and [r["cached_tokens"] for i,r in chunks]==list(range(0,request["input_tokens"],512)))
    for done_i,c in chunks:
        layers=[(i,r) for i,r in select("prefill_chunk_layer_ready") if r["request_id"]==rid and r["chunk_index"]==c["chunk_index"]]
        comp=[(i,r) for i,r in select("compute_step_completed") if r.get("phase")=="prefill" and r.get("batch_id")==c["batch_id"]]
        ok=(ok and [r["layer_id"] for i,r in layers]==list(range(61)) and [r["layer_id"] for i,r in comp]==list(range(61))
            and all(r["size_bytes"]==c["chunk_tokens"]*1152 and r["cached_tokens"]==c["cached_tokens"] for i,r in layers)
            and all(ci<li and cr["time_ns"]==lr["time_ns"] for (ci,cr),(li,lr) in zip(comp,layers))
            and layers[-1][0]<done_i and layers[-1][1]["time_ns"]==c["time_ns"])
    last_i,last=chunks[-1]
    received=[(i,r) for i,r in select("kv_all_received") if r["request_id"]==rid]
    first_i,first=min(((i,r) for i,r in starts.values() if rid in r["request_ids"]),key=lambda p:p[0])
    kv_i,kv=received[0]
    ok=(ok and len(received)==1 and last["time_ns"]==tokens[(rid,1)] and last_i<kv_i<first_i
        and last["time_ns"]<=kv["time_ns"]<=first["time_ns"]
        and first["context_tokens"]==[request["input_tokens"]])
    pair=dict(request_id=rid,chunk_tokens=expected,prefill_done_ns=last["time_ns"],first_token_ns=tokens[(rid,1)],
              all_KV_received_ns=kv["time_ns"],first_decode_batch_id=first["batch_id"],first_decode_batch_started_ns=first["time_ns"],passed=ok)
    p_pairs.append(pair)
    if not ok: p_bad.append(pair)
check("B2_all_chunks_first_token_KV_and_initial_Decode",not p_bad and len(p_pairs)==3,p_bad)

# All run inputs, backend and source identities; strict semantic comparisons to BOTH r02 runs.
repro=load(RUN/"reproducibility.json")
for f,k in (("config.json","config"),("compute_profile.json","profile"),("workload.json","workload"),("binding_plan.json","binding_plan")):
    check("provenance."+f,digest(RUN/f)==repro["input_sha256"][k])
check("run_inputs_match",all(load(RUN/f)==load(R02/"results/run-a"/f) for f in ("config.json","compute_profile.json","workload.json","binding_plan.json")))
metadata=load(RUN/"network/backend_metadata.json")
check("backend_ACK_contract",metadata["completion_semantics"]=="all_bytes_acknowledged_at_sender"
      and metadata["build_fingerprint"]=="23ec96146135d0706e39"
      and repro["backend"]["executable_sha256"]==load(R02/"results/run-a/reproducibility.json")["backend"]["executable_sha256"])
check("backend_binary_matches_identity",digest(Path(repro["backend"]["executable"]))==repro["backend"]["executable_sha256"])
source=load(RUN/"source/manifest.json")
check("source_tree_matches_r02",source["source_tree_sha256"]==load(R02/"results/run-a/source/manifest.json")["source_tree_sha256"])
check("source_manifest_hash",hashlib.sha256(json.dumps(source["files"],sort_keys=True,separators=(",",":")).encode()).hexdigest()==source["source_tree_sha256"]==repro["source"]["source_tree_sha256"])
check("archive_hash",digest(RUN/"source/source.tar.gz")==source["archive_sha256"]==repro["source"]["archive_sha256"])
core=[f for f in source["files"] if f["path"].startswith("joint_scheduling/") and not f.get("missing")]
check("current_core_matches_frozen_source",all(digest(ROOT/f["path"])==f["sha256"] for f in core))
with tarfile.open(RUN/"source/source.tar.gz") as tar:
    members={m.name:m for m in tar.getmembers()}
    bad=[]
    for f in source["files"]:
        name=f["path"]
        if f.get("missing"):
            if name in members: bad.append(name)
        elif name not in members: bad.append(name)
        else:
            m=members[name]
            data=m.linkname.encode() if f.get("symlink") else tar.extractfile(m).read()
            if hashlib.sha256(data).hexdigest()!=f["sha256"]: bad.append(name)
    check("archive_files_match_manifest",not bad,bad)

summary=load(RUN/"result/summary.json")
for oldrun in ("run-a","run-b"):
    old=R02/"results"/oldrun
    for f in ("result/requests.csv","result/tokens.csv","result/timeseries.csv"):
        check(oldrun+".semantic_equal."+f,csvrows(RUN/f)==csvrows(old/f))
    for f in ("expert_parallel_summary.json","prefill_chunk_summary.json","prefill_batch_summary.json","decode_batch_summary.json","final_resources.json","binding_plan.json"):
        check(oldrun+".semantic_equal."+f,load(RUN/f)==load(old/f))
    for field in ("arrival_cohort","completion_window","latencies"):
        check(oldrun+".summary_equal."+field,summary[field]==load(old/"result/summary.json")[field])
    check(oldrun+".all_lifecycle_events_equal",events==eventrows(old/"events.jsonl"))
    check(oldrun+".communication_equal",load(RUN/"observation/summary.json")["communication"]==load(old/"observation/summary.json")["communication"])
    check(oldrun+".EP_audit_equal",capture["ep_flow_audit"]==load(R02/"results"/(oldrun+"-hooks.json"))["ep_flow_audit"])
    check(oldrun+".all_hook_counts_equal",capture["hooks"]["event_counts"]==load(R02/"results"/(oldrun+"-hooks.json"))["hooks_events"]["event_counts"])

check("r02_unchanged_during_review",protected_before==inventory(R02))
report=dict(task="T002-r03",reviewer="commander",reviewed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    method="Read-only artifact review; match KV intervals and Decode batch/token context with exact integer-ns comparisons. No simulation.",
    checks_total=len(checks),passed=sum(c["passed"] for c in checks),failed=[c for c in checks if not c["passed"]],checks=checks,
    boundaries=dict(KV=kv_pairs,prefill_decode=p_pairs,decode_token=batch_pairs),
    source_based_inferences=[
        dict(claim="decode_batch_started and compute_step_started(layer0) share the same simulated timestamp; physical GPU service may begin later.",
             reason="Frozen decode.kick_decode calls layer(0) synchronously; execute_step dispatches execute_ep_step, which emits compute_step_started before any timer or GPU queue submission.",
             files=["joint_scheduling/backend/execution/decode.py:43","joint_scheduling/backend/execution/compute.py:11","joint_scheduling/backend/expert_parallel.py:59"]),
        dict(claim="KV completion callback has source_reads_complete=True and destination_ready=True under the same ACK-completion backend contract.",
             reason="Frozen rpc.submit.done constructs TransferProgress(...,True,True) for every completed logical transfer. client waits for all parts; completion metadata and executable identity match r02. Individual KV completion flags/timestamps were not captured in r03.",
             files=["joint_scheduling/backend/rpc.py:142","joint_scheduling/backend/client.py:65","joint_scheduling/backend/execution/network.py:23"])
    ],
    r02_inventory=protected_before,
    notes=["Executor matching/checker omissions were covered by this independent review, including first-token equality, exact KV identity/size/endpoints, all per-batch 61 layers and context-derived token identity.",
           "Executor analysis/capture scripts always return zero even on a false summary; this reviewer exits nonzero when any check fails. Old executor summaries are preserved as historical evidence."])
report["all_checked_conditions_passed"]=not report["failed"]
(TASK/"results/review-verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps(dict(checks_total=report["checks_total"],passed=report["passed"],failed=report["failed"],
      KV_pairs=len(kv_pairs),request_pairs=len(p_pairs),decode_token_pairs=len(batch_pairs),
      all_checked_conditions_passed=report["all_checked_conditions_passed"]),ensure_ascii=False,indent=2))
raise SystemExit(0 if report["all_checked_conditions_passed"] else 1)
