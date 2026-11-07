"""T002-r03: one read-only boundary-event capture run.

Same config/input/binding/seed as T002-r02; the only difference is that the
RuntimeHooks callback keeps a bounded copy of boundary event fields (essential
recording omits them from events.jsonl). No simulator state is modified.
"""
import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03"
INPUTS = TASK / "inputs"
RESULTS = TASK / "results"
RUN_DIR = RESULTS / "audit-run"

from joint_scheduling.config import load_config
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.execution import RuntimeHooks
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.experiments.ep_audit import EPFlowAudit

FIELDS = {
    "prefill_started": ("request_id", "instance_id"),
    "kv_ready": ("request_id",),
    "kv_all_received": ("request_id",),
    "decode_started": ("request_id", "instance_id"),
    "prefill_chunk_layer_ready": ("request_id", "layer_id", "chunk_index", "size_bytes", "cached_tokens"),
    "layer_ready": ("request_id", "layer_id", "size_bytes"),
    "prefill_chunk_completed": ("request_id", "chunk_index", "chunk_tokens", "cached_tokens", "batch_id"),
    "prefill_batch_completed": ("instance_id", "batch_id", "total_query_tokens", "batch_size"),
    "decode_batch_started": ("instance_id", "batch_id", "request_ids", "batch_size", "context_tokens"),
    "decode_batch_completed": ("instance_id", "batch_id", "request_ids", "batch_size"),
    "compute_step_completed": ("request_id", "phase", "instance_id", "layer_id", "batch_id"),
    "transfer_started": ("request_id", "transfer_id", "transfer_kind", "block_id", "layer_id",
                         "offset_bytes", "ready_at_seconds", "size_bytes", "source_gpu_id",
                         "destination_gpu_id", "priority"),
}
CAPTURE = set(FIELDS)


class BoundaryCapture:
    def __init__(self):
        self.counts = Counter()
        self.rows = []

    def event(self, name, time_ns, data):
        self.counts[name] += 1
        if name not in CAPTURE:
            return
        if name == "transfer_started" and data.get("transfer_kind") != "kv":
            return
        row = {"event": name, "time_ns": time_ns, "time_seconds": time_ns / 1e9}
        for key in FIELDS[name]:
            if key in data:
                row[key] = data[key]
        self.rows.append(row)

    def document(self):
        return {"event_counts": dict(sorted(self.counts.items())),
                "event_total": sum(self.counts.values()), "captured_rows": self.rows}


def main():
    if RUN_DIR.exists():
        raise SystemExit("refusing to overwrite: {}".format(RUN_DIR))
    config = load_config(str(INPUTS / "config.json"))
    profile = ComputeProfile(config.profile_path())
    binding_plan = json.loads((INPUTS / "binding_plan.json").read_text(encoding="utf-8"))
    ep = EPFlowAudit(config, profile)
    capture = BoundaryCapture()

    def dispatch(name, time_ns, data):
        ep.event(name, time_ns, data)
        capture.event(name, time_ns, data)

    began = time.monotonic()
    run_baseline(config, RUN_DIR, hooks=RuntimeHooks(event=dispatch), binding_plan=binding_plan)
    wall = time.monotonic() - began
    try:
        ep_result = ep.finish(json.loads((RUN_DIR / "expert_parallel_summary.json").read_text(encoding="utf-8")))
    except Exception as error:
        ep_result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}

    document = {
        "schema_version": 1, "task": "T002-r03",
        "evidence_type": "read-only RuntimeHooks boundary capture, essential recording unchanged",
        "inputs_sha256": {name: __import__("hashlib").sha256((INPUTS / name).read_bytes()).hexdigest()
                          for name in ("config.json", "workload.json", "compute_profile.json", "binding_plan.json")},
        "wall_clock_seconds": wall,
        "ep_flow_audit": ep_result,
        "hooks": capture.document(),
        "field_whitelist": {k: list(v) for k, v in FIELDS.items()},
        "note": "token_returned is written directly by TraceRecorder (not via hooks); its times come from audit-run/events.jsonl",
    }
    (RESULTS / "boundary-events.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": "completed", "wall_clock_seconds": round(wall, 3),
                      "ep_audit": ep_result.get("status"),
                      "hook_event_total": document["hooks"]["event_total"],
                      "captured_rows": len(capture.rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
