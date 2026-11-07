"""T003 step 7 gate: verify replay-both fidelity before any removal replay.

Every condition is recomputed from the frozen calendar and the both replay;
any failure returns non-zero so the caller stops before replay-collective/kv.
"""
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
DIAG = TASK / "results/diagnostic"
RESULTS = TASK / "results"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
                          encoding="utf-8")


def main():
    flows = []
    with (DIAG / "flows.csv").open(encoding="utf-8", newline="") as handle:
        for r in list(csv.reader(handle))[1:]:
            flows.append([int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]),
                          r[5], int(r[6]), int(r[7]), r[8]])
    stages = json.loads((DIAG / "stages.json").read_text(encoding="utf-8"))
    baseline = json.loads((DIAG / "baseline_summary.json").read_text(encoding="utf-8"))
    calendar = [json.loads(l) for l in (DIAG / "native_schedule.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    both_dir = DIAG / "replay-both"
    both_summary = json.loads((both_dir / "summary.json").read_text(encoding="utf-8"))
    completions = list(csv.DictReader((both_dir / "completions.csv").open(encoding="utf-8")))
    stage_evidence = json.loads((RESULTS / "replay-stage-both.json").read_text(encoding="utf-8"))
    rpc = json.loads((both_dir / "network/backend_rpc_summary.json").read_text(encoding="utf-8"))

    ack = {}
    for row in completions:
        ack[int(row["flow_index"])] = int(row["ack_ns"])
    errors = [ack.get(i, -1) - row[1] for i, row in enumerate(flows)]
    max_error = max(abs(v) for v in errors)

    # Reconstruct stage completion from frozen local ACK + remote flow ACKs.
    stage_full = [s["local_ack_ns"] for s in stages]
    for i, row in enumerate(flows):
        if row[7] >= 0:
            stage_full[row[7]] = max(stage_full[row[7]], ack.get(i, -1))
    stage_errors = [stage_full[i] - s["ack_ns"] for i, s in enumerate(stages)]
    max_stage_error = max(abs(v) for v in stage_errors)

    pairs = defaultdict(set)
    for row in flows:
        pairs[(row[2], row[3])].add(row[5])
    separate = all(len(v) == 1 for v in pairs.values())

    kinds = defaultdict(int)
    for row in calendar:
        kinds[row["command"].split()[0]] += 1
    send_with_flow = sum(1 for row in calendar if row["flow_index"] is not None)
    expected_submissions = len(calendar)

    live_identity = json.loads((DIAG / "run/reproducibility.json").read_text(encoding="utf-8"))
    both_identity = json.loads((both_dir / "reproducibility.json").read_text(encoding="utf-8"))
    shared_source = (DIAG / "inputs/source/manifest.json")
    replay_source_manifest = both_dir / both_identity["source"]["manifest"]
    shared_hash_ok = (replay_source_manifest.is_file()
                      and sha(shared_source) == sha(replay_source_manifest))

    checks = []

    def check(name, expected, actual):
        checks.append(dict(name=name, expected=expected, actual=actual, passed=expected == actual))

    check("replay_both_completed", "completed", stage_evidence.get("status"))
    check("selected_flows_equal_all_cross_node_flows", len(flows), both_summary["selected_flows"])
    check("completed_flows_equal_selected", both_summary["selected_flows"], both_summary["completed_flows"])
    check("completion_rows", len(flows), len(completions))
    check("completion_indices_no_loss_or_duplicate", list(range(len(flows))),
          sorted(ack.keys()))
    check("all_selected_flows_acked", True, all(v >= 0 for v in ack.values()))
    check("max_abs_flow_ack_error_ns_le_1", True, max_error <= 1)
    check("max_abs_ep_stage_error_ns_le_1", True, max_stage_error <= 1)
    check("endpoint_pairs_separate_by_traffic_class", True, separate and baseline["endpoint_pairs_separate_by_traffic_class"])
    check("all_native_registrations_submitted", expected_submissions, rpc["submission_commands"])
    check("native_timer_local_and_local_ports_skeleton_preserved", True,
          kinds.get("TIMER", 0) > 0 and kinds.get("LOCAL_PORTS", 0) > 0
          and send_with_flow > 0 and all(k in ("SEND", "TIMER", "LOCAL", "LOCAL_PORTS")
                                         for k in kinds))
    check("backend_identity_unchanged",
          (live_identity["backend"]["fingerprint"], live_identity["backend"]["executable_sha256"]),
          (both_identity["backend"]["fingerprint"], both_identity["backend"]["executable_sha256"]))
    check("shared_source_snapshot_used", True, shared_hash_ok)
    check("calendar_commands_have_no_flow_index_loss", True,
          all(row["flow_index"] is not None for row in calendar if row["command"].startswith("SEND ")))
    check("port_cooldown_checks_recorded", True,
          isinstance(both_summary["port_cooldown_checks"], int) and both_summary["port_cooldown_checks"] >= 0)

    report = dict(
        schema_version=1, phase="replay-both-fidelity",
        all_passed=all(c["passed"] for c in checks),
        checks=checks,
        flow_count=len(flows), stage_count=len(stages),
        max_abs_flow_ack_error_ns=max_error,
        flow_ack_error_distribution=dict(
            zero=sum(v == 0 for v in errors), plus_1=sum(v == 1 for v in errors),
            minus_1=sum(v == -1 for v in errors),
            other=sum(abs(v) > 1 for v in errors)),
        max_abs_ep_stage_error_ns=max_stage_error,
        stage_error_distribution=dict(
            zero=sum(v == 0 for v in stage_errors), plus_1=sum(v == 1 for v in stage_errors),
            minus_1=sum(v == -1 for v in stage_errors),
            other=sum(abs(v) > 1 for v in stage_errors)),
        native_calendar=dict(total=len(calendar), commands=dict(kinds),
                             send_with_flow_index=send_with_flow),
        separate_by_class_endpoint_pairs=len(pairs),
        port_cooldown_checks=both_summary["port_cooldown_checks"],
        backend_fingerprint=both_identity["backend"]["fingerprint"],
        evidence=dict(flows_csv_sha256=sha(DIAG / "flows.csv"),
                      stages_json_sha256=sha(DIAG / "stages.json"),
                      native_schedule_sha256=sha(DIAG / "native_schedule.jsonl"),
                      completions_csv_sha256=sha(both_dir / "completions.csv")),
    )
    dump(RESULTS / "replay-fidelity.json", report)
    print(json.dumps(dict(all_passed=report["all_passed"],
                          checks_passed=sum(c["passed"] for c in checks), checks_total=len(checks),
                          max_abs_flow_ack_error_ns=max_error,
                          max_abs_ep_stage_error_ns=max_stage_error,
                          failures=[c["name"] for c in checks if not c["passed"]]), ensure_ascii=False))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
