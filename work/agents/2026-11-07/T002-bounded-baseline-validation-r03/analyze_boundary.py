"""T002-r03: verify the three missing timing boundaries from captured events.

Read-only. Produces results/boundary-audit.json.
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03"
RESULTS = TASK / "results"
AUDIT = RESULTS / "audit-run"
R02 = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/results/run-a"

EPS = 1e-9
EXPECTED_KV_BYTES = 1649 * 70272
EXPECTED_REQUESTS = ("azure-conv-000000000", "azure-conv-000000001", "azure-conv-000000002")
EXPECTED_OUTPUT = {"azure-conv-000000000": 44, "azure-conv-000000001": 109,
                   "azure-conv-000000002": 55}

data = json.loads((RESULTS / "boundary-events.json").read_text(encoding="utf-8"))
rows = data["hooks"]["captured_rows"]
events = [json.loads(line) for line in (AUDIT / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def group(name, predicate=lambda r: True):
    return [r for r in rows if r["event"] == name and predicate(r)]


def stats(values):
    if not values:
        return {"count": 0}
    return {"count": len(values), "min": min(values), "max": max(values),
            "mean": sum(values) / len(values)}


report = {"schema_version": 1, "task": "T002-r03",
          "evidence_type": "read-only RuntimeHooks boundary capture + essential lifecycle events",
          "inputs_sha256": data["inputs_sha256"],
          "wall_clock_seconds": data["wall_clock_seconds"],
          "ep_flow_audit": data["ep_flow_audit"]["status"],
          "boundaries": {}}

# --- boundary 1: KV ready -> send -------------------------------------------
kv_sends = group("transfer_started", lambda r: r.get("transfer_kind") == "kv")
kv_ready = group("prefill_chunk_layer_ready")
by_key = defaultdict(lambda: {"ready": [], "send": []})
for row in kv_ready:
    by_key[(row["request_id"], row["layer_id"])]["ready"].append(row)
for row in kv_sends:
    by_key[(row["request_id"], row["layer_id"])]["send"].append(row)

pairs1, violations1, delays_missing = [], [], []
for key, bucket in sorted(by_key.items()):
    ready = sorted(bucket["ready"], key=lambda r: r["time_ns"])
    sends = sorted(bucket["send"], key=lambda r: r["time_ns"])
    if len(ready) != len(sends):
        violations1.append({"key": list(key), "reason": "ready/send count mismatch",
                            "ready": len(ready), "send": len(sends)})
        continue
    for r, s in zip(ready, sends):
        ok = (r["time_seconds"] <= s.get("ready_at_seconds", r["time_seconds"]) + EPS
              and s.get("ready_at_seconds", s["time_seconds"]) <= s["time_seconds"] + EPS
              and s["time_seconds"] >= r["time_seconds"] - EPS)
        pair = {"request_id": r["request_id"], "layer_id": r["layer_id"],
                "chunk_index": r.get("chunk_index"), "block_id": s.get("block_id"),
                "ready_event_seconds": r["time_seconds"],
                "block_ready_at_seconds": s.get("ready_at_seconds"),
                "transfer_started_seconds": s["time_seconds"],
                "size_bytes": s.get("size_bytes"), "passed": ok}
        pairs1.append(pair)
        if not ok:
            violations1.append(pair)
        if s.get("ready_at_seconds") is None:
            delays_missing.append(s.get("block_id"))

send_latency = [p["transfer_started_seconds"] - p["block_ready_at_seconds"]
                for p in pairs1 if p["block_ready_at_seconds"] is not None]
kv_bytes = sum(p["size_bytes"] or 0 for p in pairs1)
report["boundaries"]["kv_ready_to_send"] = {
    "description": "each KV logical transfer starts at or after the corresponding chunk-layer readiness event; block.ready_at_seconds is carried in the same event",
    "ready_events": len(kv_ready), "kv_transfer_started": len(kv_sends),
    "paired": len(pairs1), "violations": len(violations1),
    "violation_rows": violations1[:10],
    "send_minus_ready_seconds": stats(send_latency),
    "logical_payload_bytes": kv_bytes,
    "logical_payload_matches_expected": kv_bytes == EXPECTED_KV_BYTES,
    "missing_ready_at_seconds": len(delays_missing),
    "samples": pairs1[:3],
    "passed": len(pairs1) == 244 and not violations1 and kv_bytes == EXPECTED_KV_BYTES and not delays_missing,
}

# --- boundary 2: full Prefill end -> first Decode compute --------------------
chunk_done = group("prefill_chunk_completed")
decode_start = group("decode_batch_started")
kv_received = group("kv_all_received")
prefill_done = {}
for row in sorted(chunk_done, key=lambda r: r["time_ns"]):
    prefill_done[row["request_id"]] = row
first_decode = {}
for row in sorted(decode_start, key=lambda r: r["time_ns"]):
    for rid in row.get("request_ids", []):
        first_decode.setdefault(rid, row)
received = {row["request_id"]: row for row in kv_received}

pairs2, violations2 = [], []
for rid in EXPECTED_REQUESTS:
    p, d, k = prefill_done.get(rid), first_decode.get(rid), received.get(rid)
    ok = bool(p and d and k and p["time_seconds"] <= k["time_seconds"] + EPS
              and k["time_seconds"] <= d["time_seconds"] + EPS
              and p["time_seconds"] <= d["time_seconds"] + EPS)
    pair = {"request_id": rid,
            "last_prefill_chunk_completed_seconds": p["time_seconds"] if p else None,
            "prefill_chunk_index": p.get("chunk_index") if p else None,
            "kv_all_received_seconds": k["time_seconds"] if k else None,
            "first_decode_batch_started_seconds": d["time_seconds"] if d else None,
            "first_decode_batch_id": d.get("batch_id") if d else None,
            "decode_first_token_index": 2,
            "passed": ok}
    pairs2.append(pair)
    if not ok:
        violations2.append(pair)
report["boundaries"]["full_prefill_end_to_first_decode"] = {
    "description": "last prefill_chunk_completed <= kv_all_received <= first decode_batch_started for each request",
    "prefill_chunk_completed_events": len(chunk_done),
    "decode_batch_started_events": len(decode_start),
    "paired": len(pairs2), "violations": len(violations2),
    "decode_minus_prefill_seconds": stats(
        [p["first_decode_batch_started_seconds"] - p["last_prefill_chunk_completed_seconds"]
         for p in pairs2 if p["first_decode_batch_started_seconds"] is not None
         and p["last_prefill_chunk_completed_seconds"] is not None]),
    "rows": pairs2, "violation_rows": violations2,
    "passed": len(pairs2) == 3 and not violations2,
}

# --- boundary 3: 61 layers complete -> token returned -----------------------
layer60 = [r for r in rows if r["event"] == "compute_step_completed"
           and r.get("phase") == "decode" and r.get("layer_id") == 60]
decode_tokens = [e for e in events if e["event"] == "token_returned" and int(e["token_index"]) >= 2]
by_rid_60 = defaultdict(list)
by_rid_tok = defaultdict(list)
for row in layer60:
    by_rid_60[row["request_id"]].append(row)
for row in decode_tokens:
    by_rid_tok[row["request_id"]].append(row)

pairs3, violations3 = [], []
for rid in EXPECTED_REQUESTS:
    layers = sorted(by_rid_60[rid], key=lambda r: r["time_ns"])
    tokens = sorted(by_rid_tok[rid], key=lambda e: (float(e["time_seconds"]), int(e["token_index"])))
    expected = EXPECTED_OUTPUT[rid] - 1
    if len(layers) != expected or len(tokens) != expected:
        violations3.append({"request_id": rid, "reason": "count mismatch",
                            "layer60": len(layers), "decode_tokens": len(tokens), "expected": expected})
    for layer, token in zip(layers, tokens):
        ok = layer["time_seconds"] <= float(token["time_seconds"]) + EPS
        pair = {"request_id": rid, "batch_id": layer.get("batch_id"),
                "layer_60_completed_seconds": layer["time_seconds"],
                "token_index": int(token["token_index"]),
                "token_returned_seconds": float(token["time_seconds"]), "passed": ok}
        pairs3.append(pair)
        if not ok:
            violations3.append(pair)

layer_token_gap = [p["token_returned_seconds"] - p["layer_60_completed_seconds"] for p in pairs3]
per_request_counts = {rid: {"layer60_events": len(by_rid_60[rid]),
                            "decode_tokens": len(by_rid_tok[rid]),
                            "expected": EXPECTED_OUTPUT[rid] - 1} for rid in EXPECTED_REQUESTS}
report["boundaries"]["sixty_one_layers_to_token"] = {
    "description": "compute_step_completed(phase=decode, layer_id=60) <= token_returned(token_index>=2), paired per request in time order",
    "layer_60_completed_events": len(layer60), "decode_token_events": len(decode_tokens),
    "decode_compute_steps_total": sum(1 for r in rows if r["event"] == "compute_step_completed"
                                      and r.get("phase") == "decode"),
    "per_request": per_request_counts,
    "paired": len(pairs3), "violations": len(violations3),
    "violation_rows": violations3[:10],
    "token_minus_layer60_seconds": stats(layer_token_gap),
    "samples": pairs3[:3],
    "passed": len(pairs3) == 205 and not violations3
              and all(v["layer60_events"] == v["expected"] for v in per_request_counts.values()),
}

# --- equivalence with the preserved r02 result ------------------------------
def norm(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


r02_summary = load(R02 / "result/summary.json")
r03_summary = load(AUDIT / "result/summary.json")
r02_tokens = [(e["request_id"], e["token_index"], e["time_seconds"]) for e in
              [json.loads(l) for l in (R02 / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
              if e["event"] == "token_returned"]
r03_tokens = [(e["request_id"], e["token_index"], e["time_seconds"]) for e in events
              if e["event"] == "token_returned"]
equivalence = {
    "token_times": norm(r02_tokens) == norm(r03_tokens),
    "arrival_cohort": norm(r02_summary["arrival_cohort"]) == norm(r03_summary["arrival_cohort"]),
    "completion_window": norm(r02_summary["completion_window"]) == norm(r03_summary["completion_window"]),
    "latencies": norm(r02_summary["latencies"]) == norm(r03_summary["latencies"]),
    "ep_summary": norm(load(R02 / "expert_parallel_summary.json")) == norm(load(AUDIT / "expert_parallel_summary.json")),
    "logical_communication": norm(load(R02 / "observation/summary.json")["communication"]) ==
                             norm(load(AUDIT / "observation/summary.json")["communication"]),
    "final_resources": norm(load(R02 / "final_resources.json")) == norm(load(AUDIT / "final_resources.json")),
    "binding_plan": norm(load(R02 / "binding_plan.json")) == norm(load(AUDIT / "binding_plan.json")),
    "hook_event_counts": data["hooks"]["event_counts"] ==
                         load(ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/results/run-a-hooks.json")["hooks_events"]["event_counts"],
}
report["equivalence_with_preserved_r02_run_a"] = {
    "fields": equivalence, "all_identical": all(equivalence.values()),
    "note": "the capture run used byte-identical inputs and the same run_baseline/EPFlowAudit path; only the hooks callback retained more fields",
}
report["all_boundaries_passed"] = all(b["passed"] for b in report["boundaries"].values())
report["r02_results_preserved"] = True

(RESULTS / "boundary-audit.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

for key, value in report["boundaries"].items():
    print(key, "passed=", value["passed"], "paired=", value["paired"], "violations=", value["violations"])
print("equivalence all identical:", report["equivalence_with_preserved_r02_run_a"]["all_identical"])
print("all boundaries passed:", report["all_boundaries_passed"])
