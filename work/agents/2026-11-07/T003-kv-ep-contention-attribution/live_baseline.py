"""T003 phase 1 (live-baseline): ONE real ns-3 end-to-end run.

Read-only RuntimeHooks compose:
  * contention.Probe        -> flows.csv / stages.json / native_schedule.jsonl
  * EPFlowAudit             -> EP logical-flow audit
  * T3 capture              -> event-links.jsonl / port-intervals.csv

No official function is patched. The script itself enforces a hard wall-clock
limit, refuses to overwrite an existing run directory, and stops on the
TASK.md resource guards (1e6 cross-node logical flows / 2e6 native entries).
"""
import csv
import hashlib
import json
import signal
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
DIAG = TASK / "results/diagnostic"
RUN = DIAG / "run"
RESULTS = TASK / "results"
HARD_WALL_SECONDS = 25 * 60
MAX_FLOWS = 1_000_000
MAX_NATIVE = 2_000_000


class HardStop(Exception):
    pass


def _alarm(_signum, _frame):
    raise HardStop("hard wall-clock limit reached")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
                          encoding="utf-8")


from joint_scheduling.config import load_config
from joint_scheduling.backend.compute_profile import ComputeProfile, profile_kind
from joint_scheduling.backend.execution import RuntimeHooks
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.experiments.ep_audit import EPFlowAudit
from joint_scheduling.experiments import contention

KV_FIELDS = ("request_id", "transfer_id", "transfer_kind", "block_id", "layer_id",
             "offset_bytes", "ready_at_seconds", "size_bytes", "source_gpu_id",
             "destination_gpu_id", "priority")
EP_FIELDS = KV_FIELDS + ("collective_type", "ep_call_id")
STEP_FIELDS = ("request_id", "phase", "instance_id", "tokens", "layer_id", "batch_id",
               "context_tokens", "chunk_index", "cached_tokens", "total_query_tokens",
               "scheduled_tokens")
BATCH_EVENTS = ("prefill_batch_started", "prefill_batch_completed",
                "decode_batch_started", "decode_batch_completed")
BOUNDARY_EVENTS = ("prefill_started", "decode_started", "kv_ready", "kv_all_received",
                   "prefill_chunk_started", "prefill_chunk_completed",
                   "prefill_chunk_layer_ready", "layer_ready")


class T3Probe(contention.Probe):
    def __init__(self, config, links_handle, port_writer):
        super().__init__(config)
        self.links = links_handle
        self.port_writer = port_writer
        self.current_step = {}
        self.kv_links = {}
        # NOTE: Probe.__init__ owns self.counts (per-endpoint in-flight counters).
        # Never overwrite it; T3's own event tally uses a separate attribute.
        self.hook_counts = Counter()
        self.sample_count = 0
        self.interval_samples = 0
        self.interval_rows_written = 0
        self.interval_rows_omitted = 0
        self.nonlocal_ports = 0
        self.native_count = 0
        self.deadline = time.monotonic() + HARD_WALL_SECONDS
        self.guard = None

    # -- hard limits -------------------------------------------------------
    def _check_limits(self):
        if self.guard is not None:
            raise HardStop(self.guard)
        if len(self.flows) > MAX_FLOWS:
            self.guard = "cross-node logical flows exceeded 1,000,000"
            raise HardStop(self.guard)
        if self.native_count > MAX_NATIVE:
            self.guard = "native registrations exceeded 2,000,000"
            raise HardStop(self.guard)
        if (self.native_count % 20000 == 0) and time.monotonic() > self.deadline:
            self.guard = "live-baseline exceeded 25 minute hard limit"
            raise HardStop(self.guard)

    def native_event(self, registration):
        self.native_count += 1
        self._check_limits()
        super().native_event(registration)

    # -- 1 ms port intervals (cumulative counter differences) --------------
    def sample(self, state):
        self.sample_count += 1
        previous = self.previous
        if previous is not None:
            dt = state["time_seconds"] - previous["time_seconds"]
            if dt > 0:
                self.interval_samples += 1
                keys = [k for k in state["resources"] if not k.startswith("local:")]
                self.nonlocal_ports = len(keys)
                for key in keys:
                    row = state["resources"][key]
                    old = previous["resources"][key]
                    tx = row["tx_bytes"] - old["tx_bytes"]
                    kv = row["kv_bytes"] - old["kv_bytes"]
                    ep = row["collective_bytes"] - old["collective_bytes"]
                    queue = row["queue_bytes"]
                    pfc = row["pfc_pause_count"] - old["pfc_pause_count"]
                    if tx or kv or ep or queue or pfc:
                        self.port_writer.writerow([
                            "%.9f" % previous["time_seconds"], "%.9f" % state["time_seconds"],
                            key, row["node_id"], row["peer_id"], row["capacity_bps"],
                            tx, kv, ep, queue, pfc,
                            "%.9g" % (tx * 8 / (dt * row["capacity_bps"]))])
                        self.interval_rows_written += 1
                    else:
                        self.interval_rows_omitted += 1
        super().sample(state)

    # -- read-only event copies --------------------------------------------
    def _write(self, link_type, time_ns, **fields):
        row = dict(link_type=link_type, time_ns=time_ns)
        row.update(fields)
        self.links.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    def capture(self, name, time_ns, data):
        self.hook_counts[name] += 1
        if name == "transfer_started":
            index = self.last_remote_index
            row = self.flows[index] if index is not None else None
            is_kv = data.get("transfer_kind") == "kv"
            fields = {k: data[k] for k in (KV_FIELDS if is_kv else EP_FIELDS) if k in data}
            fields.update(flow_index=index, stage_index=(row[7] if row is not None else None),
                          source_endpoint=(row[2] if row is not None else None),
                          destination_endpoint=(row[3] if row is not None else None))
            self._write("transfer", time_ns, **fields)
            if is_kv:
                link = dict(fields)
                link.update(start_ns=time_ns, ack_ns=None)
                self.kv_links[data["transfer_id"]] = link
        elif name == "transfer_completed" and data.get("transfer_kind") == "kv":
            self._write("kv_transfer_completed", time_ns,
                        request_id=data.get("request_id"), transfer_id=data["transfer_id"],
                        block_id=data.get("block_id"), layer_id=data.get("layer_id"))
            link = self.kv_links.get(data["transfer_id"])
            if link is not None:
                link["ack_ns"] = time_ns
        elif name == "compute_step_started":
            step = {k: data[k] for k in STEP_FIELDS if k in data}
            self.current_step[(data["phase"], data["instance_id"])] = step
            if data.get("layer_id") == 60:
                self._write("compute_step_started", time_ns, **step)
        elif name == "compute_step_completed":
            if data.get("layer_id") == 60:
                self._write("compute_step_completed", time_ns,
                            **{k: data[k] for k in STEP_FIELDS if k in data})
        elif name == "ep_call_started":
            step = dict(self.current_step.get((data["phase"], data["source_instance_id"]), {}))
            self._write("ep_call", time_ns,
                        ep_call_id=data["ep_call_id"], request_id=data["request_id"],
                        source_instance_id=data["source_instance_id"], phase=data["phase"],
                        layer_id=data["layer_id"], batch_id=step.get("batch_id"),
                        context_tokens=step.get("context_tokens"), chunk_index=step.get("chunk_index"),
                        cached_tokens=step.get("cached_tokens"),
                        total_query_tokens=step.get("total_query_tokens"),
                        scheduled_tokens=step.get("scheduled_tokens"),
                        tokens=step.get("tokens"), instance_id=step.get("instance_id"))
        elif name == "collective_started" and data.get("collective_type") in ("ep_dispatch", "ep_combine"):
            key = (data["ep_call_id"], data["collective_type"])
            stage_index = self.stage_ids.get(key)
            self._write("ep_stage", time_ns, stage_index=stage_index,
                        ep_call_id=data["ep_call_id"], request_id=data.get("request_id"),
                        collective_id=data["collective_id"], collective_type=data["collective_type"],
                        size_bytes=data["size_bytes"], flow_count=data["flow_count"])
        elif name in BATCH_EVENTS:
            self._write("batch", time_ns, event=name,
                        **{k: data[k] for k in ("instance_id", "batch_id", "request_ids",
                                                "batch_size", "context_tokens",
                                                "total_query_tokens", "scheduled_tokens",
                                                "cached_tokens") if k in data})
        elif name in BOUNDARY_EVENTS:
            self._write("boundary", time_ns, event=name,
                        **{k: data[k] for k in ("request_id", "instance_id", "chunk_index",
                                                "chunk_tokens", "cached_tokens", "layer_id",
                                                "size_bytes", "batch_id") if k in data})
        self._check_limits()


def main():
    if RUN.exists():
        print("REFUSE: run directory already exists: %s" % RUN, file=sys.stderr)
        return 2
    evidence = dict(phase="live-baseline", started_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(HARD_WALL_SECONDS)
    links_path = RESULTS / "event-links.jsonl"
    ports_path = RESULTS / "port-intervals.csv"
    if links_path.exists() or ports_path.exists():
        print("REFUSE: evidence file already exists", file=sys.stderr)
        return 2
    config = load_config(str(DIAG / "config.json"))
    profile = ComputeProfile(config.profile_path())
    binding_plan = json.loads((DIAG / "inputs/binding_plan.json").read_text(encoding="utf-8"))
    ep = EPFlowAudit(config, profile)

    source_snapshot = (DIAG / "inputs/source").resolve()
    if not (source_snapshot / "manifest.json").is_file():
        print("STOP: shared source snapshot missing", file=sys.stderr)
        return 3

    began = time.monotonic()
    with links_path.open("w", encoding="utf-8") as links, \
            ports_path.open("w", newline="", encoding="utf-8") as ports:
        port_writer = csv.writer(ports)
        port_writer.writerow(["start_seconds", "end_seconds", "resource_id", "node_id",
                              "peer_id", "capacity_bps", "delta_tx_bytes", "delta_kv_bytes",
                              "delta_collective_bytes", "queue_bytes", "delta_pfc_pause_count",
                              "interval_utilization"])
        probe = T3Probe(config, links, port_writer)

        def dispatch(name, time_ns, data):
            probe.event(name, time_ns, data)     # official Probe first (owns stage ids / flow index)
            ep.event(name, time_ns, data)         # read-only EP audit
            probe.capture(name, time_ns, data)    # T3 read-only copies

        hooks = RuntimeHooks(event=dispatch, network_sample=probe.sample,
                            native_schedule=probe.native_event)
        try:
            result = run_baseline(config, RUN, hooks=hooks,
                                  source_snapshot=source_snapshot, binding_plan=binding_plan)
        except Exception as error:
            evidence.update(status="failed", error_type=type(error).__name__, error=str(error),
                            traceback=traceback.format_exc(),
                            wall_clock_seconds=time.monotonic() - began,
                            native_registrations=probe.native_count,
                            cross_node_flows=len(probe.flows),
                            hook_events=sum(probe.hook_counts.values()))
            dump(RESULTS / "live-stage.json", evidence)
            print("LIVE FAILED: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
    wall = time.monotonic() - began
    signal.alarm(0)
    evidence["wall_clock_seconds"] = wall

    # ---- official contention export format -------------------------------
    with (DIAG / "flows.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["start_ns", "ack_ns", "source_endpoint", "destination_endpoint",
                         "payload_bytes", "kind", "priority", "stage_index", "request_id"])
        writer.writerows(probe.flows)
    (DIAG / "stages.json").write_text(json.dumps(probe.stages, separators=(",", ":")) + "\n",
                                      encoding="utf-8")
    with (DIAG / "native_schedule.jsonl").open("w", encoding="utf-8") as handle:
        for row in probe.native_schedule:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    pairs = defaultdict(set)
    for row in probe.flows:
        pairs[(row[2], row[3])].add(row[5])
    separate_by_class = all(len(v) == 1 for v in pairs.values())
    active_end = max(row[1] for row in probe.flows) / 1e9
    for p in probe.ports.values():
        p["mean_utilization_during_flow_span"] = p["tx_bytes"] * 8 / (active_end * p["capacity_bps"])
    profile_path = config.profile_path()
    summary = dict(
        wall_seconds=wall, flow_count=len(probe.flows), stage_count=len(probe.stages),
        active_span_seconds=active_end,
        endpoint_outstanding_overlap_seconds={k: v / 1e9 for k, v in probe.endpoint_overlap.items()},
        any_endpoint_outstanding_overlap_seconds=probe.global_overlap_ns / 1e9,
        ports=probe.ports, arrival_cohort=result.summary["arrival_cohort"],
        pressure=result.summary["pressure"],
        workload_provenance=json.loads((DIAG / "inputs/workload.json").read_text(encoding="utf-8"))["provenance"],
        profile_sha256=sha(profile_path),
        model_status=profile_kind(json.loads(profile_path.read_text())["provenance"]),
        experiment_type="end_to_end", endpoint_pairs_separate_by_traffic_class=separate_by_class,
        native_registration_count=probe.native_count,
        t3=dict(hook_event_total=sum(probe.hook_counts.values()),
                hook_event_counts=dict(sorted(probe.hook_counts.items())),
                network_samples=probe.sample_count, interval_samples=probe.interval_samples,
                nonlocal_ports=probe.nonlocal_ports,
                port_interval_rows_written=probe.interval_rows_written,
                port_interval_rows_omitted=probe.interval_rows_omitted,
                port_interval_omission_rule=("omit a (1 ms sample, port) row when delta_tx=delta_kv="
                                             "delta_collective=0, queue_bytes=0 and delta_pfc=0"),
                kv_transfer_links=len(probe.kv_links),
                unfinished_kv_transfers=sum(v["ack_ns"] is None for v in probe.kv_links.values())))
    dump(DIAG / "baseline_summary.json", summary)
    dump(RESULTS / "kv-transfer-links.json", probe.kv_links)

    # ---- EP audit and mechanism checks ------------------------------------
    try:
        ep_result = ep.finish(json.loads((RUN / "expert_parallel_summary.json").read_text(encoding="utf-8")))
    except Exception as error:
        ep_result = dict(status="failed", error_type=type(error).__name__, error=str(error))
    evidence["ep_flow_audit"] = ep_result

    s = json.loads((RUN / "result/summary.json").read_text(encoding="utf-8"))
    requests = list(csv.DictReader((RUN / "result/requests.csv").open(encoding="utf-8")))
    tokens = list(csv.DictReader((RUN / "result/tokens.csv").open(encoding="utf-8")))
    chunk = json.loads((RUN / "prefill_chunk_summary.json").read_text(encoding="utf-8"))
    batch = json.loads((RUN / "decode_batch_summary.json").read_text(encoding="utf-8"))
    ep_summary = json.loads((RUN / "expert_parallel_summary.json").read_text(encoding="utf-8"))
    comm = json.loads((RUN / "observation/summary.json").read_text(encoding="utf-8"))["communication"]
    resources = json.loads((RUN / "final_resources.json").read_text(encoding="utf-8"))
    workload = json.loads((RUN / "workload.json").read_text(encoding="utf-8"))

    checks = []

    def check(name, expected, actual):
        checks.append(dict(name=name, expected=expected, actual=actual, passed=expected == actual))

    check("requests_completed", 8, sum(r["status"] == "completed" for r in requests))
    check("output_tokens", 550, len(tokens))
    check("failed_requests", 0, s["arrival_cohort"]["failed_count"])
    check("incomplete_requests", 0, s["arrival_cohort"]["incomplete_count"])
    check("prefill_chunks", 11, chunk["started_chunks"])
    check("prefill_chunks_completed", 11, chunk["completed_chunks"])
    check("prefill_query_tokens", 3913, chunk["completed_query_tokens"])
    check("decode_steps", 542, batch["started_batches"])
    check("decode_steps_completed", 542, batch["completed_batches"])
    check("layers_per_decode_step", 61, batch["layers_per_step"])
    check("ep_calls", 32074, ep_summary["started_calls"])
    check("ep_calls_completed", 32074, ep_summary["completed_calls"])
    check("kv_logical_payload_bytes", 3913 * 70272, comm["kv"]["started_bytes"])
    check("kv_logical_payload_completed_bytes", 3913 * 70272, comm["kv"]["completed_bytes"])
    check("kv_messages", 671, comm["kv"]["started_count"])
    check("kv_messages_completed", 671, comm["kv"]["completed_count"])
    check("memory_domains", 32, len(resources))
    check("memory_occupied_zero", 0, sum(v["occupied_bytes"] for v in resources.values()))
    check("memory_reserved_zero", 0, sum(v["reserved_bytes"] for v in resources.values()))
    check("active_ep_calls_at_end", 0, len(ep_summary["active_calls_at_end"]))
    check("active_decode_batches_at_end", 0, len(batch["active_batches_at_end"]))
    check("active_logical_flows", 0, len(probe.active))
    check("active_local_collective", 0, len(probe.local_active))
    check("all_stage_acks_after_start", True, all(x["ack_ns"] >= x["start_ns"] for x in probe.stages))
    check("ep_flow_audit_passed", "passed", ep_result.get("status"))
    check("endpoint_pairs_separate_by_class", True, separate_by_class)
    check("unfinished_kv_transfers", 0,
          sum(v["ack_ns"] is None for v in probe.kv_links.values()))
    check("workload_request_count", 8, len(workload["requests"]))
    check("exported_binding_plan_identical", binding_plan["bindings_sha256"],
          json.loads((RUN / "binding_plan.json").read_text(encoding="utf-8"))["bindings_sha256"])
    check("all_cross_node_flows_acked", True,
          all(row[1] >= 0 for row in probe.flows))
    checks.append(dict(name="resource_guards", expected="<=1e6 flows and <=2e6 native",
                       actual=dict(flows=len(probe.flows), native=probe.native_count),
                       passed=len(probe.flows) <= MAX_FLOWS and probe.native_count <= MAX_NATIVE))

    mechanism = dict(schema_version=1, phase="live-baseline",
                     all_passed=all(c["passed"] for c in checks),
                     checks=checks,
                     counts=dict(flows=len(probe.flows), stages=len(probe.stages),
                                 native_registrations=probe.native_count,
                                 hook_events=sum(probe.hook_counts.values())))
    dump(RESULTS / "mechanism-checks.json", mechanism)
    evidence.update(status="completed" if mechanism["all_passed"] else "mechanism_failed",
                    mechanism_all_passed=mechanism["all_passed"],
                    checks_passed=sum(c["passed"] for c in checks), checks_total=len(checks),
                    flows=len(probe.flows), stages=len(probe.stages),
                    native_registrations=probe.native_count,
                    wall_clock_seconds=wall,
                    simulation_time_limit_seconds=config.runtime.time_limit_seconds)
    dump(RESULTS / "live-stage.json", evidence)
    print(json.dumps({k: evidence[k] for k in
                      ("status", "wall_clock_seconds", "mechanism_all_passed", "checks_passed",
                       "checks_total", "flows", "stages", "native_registrations")},
                     ensure_ascii=False))
    return 0 if mechanism["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
