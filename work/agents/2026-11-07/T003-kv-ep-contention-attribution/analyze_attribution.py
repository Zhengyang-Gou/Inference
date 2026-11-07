"""T003 step 8: read-only attribution analysis (no ns-3 execution).

Builds flow/stage signed comparisons, port sharing evidence, request-boundary
exposure and the actual end-to-end metrics from the frozen files.
"""
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
DIAG = TASK / "results/diagnostic"
RESULTS = TASK / "results"
RUN = DIAG / "run"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
                          encoding="utf-8")


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    i = int(pos)
    return values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (pos - i)


def stats(values):
    return dict(count=len(values), mean=statistics.mean(values) if values else None,
                p50=quantile(values, .5), p95=quantile(values, .95),
                p99=quantile(values, .99), minimum=min(values, default=None),
                maximum=max(values, default=None))


def main():
    # ------------------------------------------------------------------ load
    with (DIAG / "flows.csv").open(encoding="utf-8", newline="") as handle:
        raw = list(csv.reader(handle))[1:]
    flows = [[int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), r[5],
              int(r[6]), int(r[7]), r[8]] for r in raw]
    stages = json.loads((DIAG / "stages.json").read_text(encoding="utf-8"))
    baseline = json.loads((DIAG / "baseline_summary.json").read_text(encoding="utf-8"))
    fidelity = json.loads((RESULTS / "replay-fidelity.json").read_text(encoding="utf-8"))
    mechanism = json.loads((RESULTS / "mechanism-checks.json").read_text(encoding="utf-8"))
    live_stage = json.loads((RESULTS / "live-stage.json").read_text(encoding="utf-8"))
    sample_manifest = json.loads((TASK / "inputs/sample-manifest.json").read_text(encoding="utf-8"))
    identity = json.loads((RESULTS / "identity-check.json").read_text(encoding="utf-8"))
    environment = json.loads((RESULTS / "environment-check.json").read_text(encoding="utf-8"))

    acks = {}
    replay_evidence = {}
    for name in ("both", "collective", "kv"):
        directory = DIAG / ("replay-" + name)
        arr = [-1] * len(flows)
        for row in csv.DictReader((directory / "completions.csv").open(encoding="utf-8")):
            arr[int(row["flow_index"])] = int(row["ack_ns"])
        acks[name] = arr
        replay_evidence[name] = json.loads((RESULTS / ("replay-stage-" + name + ".json")).read_text(encoding="utf-8"))

    # Event links (streamed during live). Only the small link types are loaded;
    # the full transfer_id<->flow_index rows remain on disk for audit.
    ep_calls, ep_stages, batches, boundaries, layer60 = {}, {}, [], [], []
    with (RESULTS / "event-links.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            r = json.loads(line)
            kind = r["link_type"]
            if kind == "ep_call":
                ep_calls[r["ep_call_id"]] = r
            elif kind == "ep_stage":
                ep_stages[r["stage_index"]] = r
            elif kind == "batch":
                batches.append(r)
            elif kind == "boundary":
                boundaries.append(r)
            elif kind in ("compute_step_started", "compute_step_completed"):
                layer60.append(r)

    workload = json.loads((DIAG / "inputs/workload.json").read_text(encoding="utf-8"))
    input_tokens = {r["request_id"]: r["input_tokens"] for r in workload["requests"]}
    requests_csv = list(csv.DictReader((RUN / "result/requests.csv").open(encoding="utf-8")))
    tokens_csv = list(csv.DictReader((RUN / "result/tokens.csv").open(encoding="utf-8")))
    token_ns = {(r["request_id"], int(r["token_index"])): round(float(r["returned_at_seconds"]) * 1e9)
                for r in tokens_csv}
    live_summary = json.loads((RUN / "result/summary.json").read_text(encoding="utf-8"))

    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    # ------------------------------------------------------- flow comparison
    full = acks["both"]
    isolated = {"collective": acks["collective"], "kv": acks["kv"]}
    require(all(v >= 0 for v in full), "replay-both has unacked flow")
    for kind in ("collective", "kv"):
        ids = [i for i, r in enumerate(flows) if r[5] == kind]
        require(all(isolated[kind][i] >= 0 for i in ids), "isolated replay missing " + kind + " flow ACK")
        require(all(isolated[kind][i] == -1 for i, r in enumerate(flows) if r[5] != kind),
                "isolated replay unexpectedly completed removed-class flow")

    with (RESULTS / "flow-comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["flow_index", "kind", "request_id", "stage_index", "source_endpoint",
                    "destination_endpoint", "source_node", "destination_node",
                    "payload_bytes", "priority", "start_ns", "ack_live_ns", "ack_both_ns",
                    "ack_isolated_ns", "isolated_replay", "delta_ack_ns",
                    "full_latency_us", "isolated_latency_us", "added_latency_us"])
        for i, r in enumerate(flows):
            iso = isolated[r[5]][i]
            delta = full[i] - iso
            w.writerow([i, r[5], r[8], r[7], r[2], r[3], r[2] // 8, r[3] // 8,
                        r[4], r[6], r[0], r[1], full[i], iso,
                        "replay-collective" if r[5] == "collective" else "replay-kv",
                        delta, (full[i] - r[0]) / 1000, (iso - r[0]) / 1000, delta / 1000])

    classes = {}
    for kind in ("collective", "kv"):
        ids = [i for i, r in enumerate(flows) if r[5] == kind]
        added = [(full[i] - isolated[kind][i]) / 1000 for i in ids]
        classes[kind] = dict(
            flow_count=len(ids), logical_payload_bytes=sum(flows[i][4] for i in ids),
            full_flow_latency_us=stats([(full[i] - flows[i][0]) / 1000 for i in ids]),
            isolated_flow_latency_us=stats([(isolated[kind][i] - flows[i][0]) / 1000 for i in ids]),
            added_latency_us=stats(added),
            delayed_by_over_1us=dict(count=sum(v > 1 for v in added),
                                     fraction=sum(v > 1 for v in added) / len(added) if added else None),
            improved_by_over_1us=dict(count=sum(v < -1 for v in added),
                                      fraction=sum(v < -1 for v in added) / len(added) if added else None),
            negative_delta=dict(count=sum(v < 0 for v in added),
                                minimum_us=min(added, default=None),
                                sum_magnitude_us=abs(sum(v for v in added if v < 0))),
            positive_delta=dict(count=sum(v > 0 for v in added),
                                maximum_us=max(added, default=None),
                                sum_us=sum(v for v in added if v > 0)))

    # ------------------------------------------------------ stage comparison
    stage_full = [s["local_ack_ns"] for s in stages]
    stage_no_kv = [s["local_ack_ns"] for s in stages]
    for i, r in enumerate(flows):
        if r[7] >= 0:
            stage_full[r[7]] = max(stage_full[r[7]], full[i])
            stage_no_kv[r[7]] = max(stage_no_kv[r[7]], acks["collective"][i])
    stage_live_error = max(abs(stage_full[i] - s["ack_ns"]) for i, s in enumerate(stages))
    require(stage_live_error <= 1, "stage reconstruction differs from live by >1ns")

    decode_batches = {}
    chunk_by_batch = {}
    final_chunk = {}
    batch_sizes = []
    for r in batches:
        if r["event"] == "decode_batch_started":
            decode_batches[r["batch_id"]] = r
            batch_sizes.append(("decode", r["batch_id"], r.get("batch_size")))
        elif r["event"] == "prefill_batch_started":
            batch_sizes.append(("prefill", r["batch_id"], r.get("batch_size")))
    require(all(size == 1 for _, _, size in batch_sizes),
            "T003 expects batch_size=1 for exact token/batch identity; size>1 found")
    chunk_rows = []
    for r in boundaries:
        if r["event"] == "prefill_chunk_started":
            batch_id = r.get("batch_id")
            if batch_id:
                chunk_by_batch[batch_id] = r
            rid = r["request_id"]
            final_chunk[rid] = max(final_chunk.get(rid, -1), r["chunk_index"])
            chunk_rows.append((rid, r["chunk_index"]))
    require(len(chunk_rows) == len(set(chunk_rows)), "duplicate (request, chunk) boundary rows")

    max_layer = max((c["layer_id"] for c in ep_calls.values() if c.get("layer_id") is not None), default=-1)
    require(max_layer == 60, "final layer id is not 60")

    def stage_meta(i):
        s = stages[i]
        link = ep_stages.get(i, {})
        call = ep_calls.get(link.get("ep_call_id") or s["call_id"], {})
        batch_id = call.get("batch_id") or link.get("batch_id")
        chunk = call.get("chunk_index")
        if chunk is None and batch_id in chunk_by_batch:
            chunk = chunk_by_batch[batch_id]["chunk_index"]
        return dict(call=call, link=link, batch_id=batch_id, chunk_index=chunk,
                    layer_id=call.get("layer_id"), phase=s["phase"], kind=s["kind"])

    stage_rows = []
    stage_groups_raw = defaultdict(list)
    boundary_stages = {"prefill_final": {}, "decode_tokens": {}}
    for i, s in enumerate(stages):
        m = stage_meta(i)
        added_ns = stage_full[i] - stage_no_kv[i]
        is_final = (m["kind"] == "ep_combine" and m["layer_id"] == max_layer)
        token_index = None
        if is_final and m["phase"] == "prefill":
            rid = m["call"].get("request_id") or m["link"].get("request_id")
            if rid and m["chunk_index"] == final_chunk.get(rid):
                boundary_stages["prefill_final"][rid] = i
        if is_final and m["phase"] == "decode" and m["batch_id"] in decode_batches:
            b = decode_batches[m["batch_id"]]
            members = b.get("request_ids", [])
            contexts = b.get("context_tokens", [])
            if len(members) == 1:
                rid = members[0]
                token_index = contexts[0] - input_tokens[rid] + 2
                boundary_stages["decode_tokens"][(rid, token_index)] = i
        stage_rows.append(dict(
            stage_index=i, call_id=s["call_id"], kind=s["kind"], phase=s["phase"],
            ep_call_id=m["link"].get("ep_call_id") or s["call_id"],
            request_id=m["call"].get("request_id") or m["link"].get("request_id"),
            source_instance_id=m["call"].get("source_instance_id"),
            layer_id=m["layer_id"], batch_id=m["batch_id"], chunk_index=m["chunk_index"],
            token_index=token_index, is_final_layer_combine=is_final,
            start_ns=s["start_ns"], local_ack_ns=s["local_ack_ns"],
            ack_live_ns=s["ack_ns"], ack_both_ns=stage_full[i], ack_no_kv_ns=stage_no_kv[i],
            full_latency_us=(stage_full[i] - s["start_ns"]) / 1000,
            no_kv_latency_us=(stage_no_kv[i] - s["start_ns"]) / 1000,
            added_us=added_ns / 1000))
        stage_groups_raw[m["phase"] + "." + m["kind"]].append(added_ns / 1000)
        if is_final:
            stage_groups_raw[m["phase"] + ".final_layer_combine"].append(added_ns / 1000)
        else:
            stage_groups_raw["other_layers"].append(added_ns / 1000)

    with (RESULTS / "stage-comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        columns = list(stage_rows[0].keys())
        w.writerow(columns)
        for row in stage_rows:
            w.writerow([row[c] for c in columns])

    stage_groups = {}
    for key, values in sorted(stage_groups_raw.items()):
        stage_groups[key] = dict(
            count=len(values), added_latency_us=stats(values),
            delayed_by_over_1us=dict(count=sum(v > 1 for v in values),
                                     fraction=sum(v > 1 for v in values) / len(values) if values else None),
            improved_by_over_1us=dict(count=sum(v < -1 for v in values),
                                      fraction=sum(v < -1 for v in values) / len(values) if values else None))

    # --------------------------------------------------------- shared ports
    ports = baseline["ports"]
    mixed_ports = {k: p for k, p in ports.items() if p["kv_bytes"] > 0 and p["ep_bytes"] > 0}
    endpoint_overlap = {k: v for k, v in baseline["endpoint_outstanding_overlap_seconds"].items() if v > 0}
    port_rows = list(csv.DictReader((RESULTS / "port-intervals.csv").open(encoding="utf-8")))
    mixed_intervals = sum(1 for r in port_rows if int(r["delta_kv_bytes"]) > 0 and
                          int(r["delta_collective_bytes"]) > 0)
    port_interval_summary = dict(
        source="results/port-intervals.csv (1 ms cumulative-counter differences)",
        rows_written=len(port_rows),
        rows_omitted=baseline["t3"]["port_interval_rows_omitted"],
        network_samples_seen=baseline["t3"]["network_samples"],
        interval_samples=baseline["t3"]["interval_samples"],
        nonlocal_ports_per_sample=baseline["t3"]["nonlocal_ports"],
        omission_rule=baseline["t3"]["port_interval_omission_rule"],
        mixed_kv_and_ep_1ms_intervals=mixed_intervals,
        max_sampled_queue_bytes=max((int(r["queue_bytes"]) for r in port_rows), default=0),
        max_interval_utilization=max((float(r["interval_utilization"]) for r in port_rows), default=0.0),
        total_delta_pfc_pause=sum(int(r["delta_pfc_pause_count"]) for r in port_rows),
        sum_delta_tx_bytes=sum(int(r["delta_tx_bytes"]) for r in port_rows),
        sum_delta_kv_bytes=sum(int(r["delta_kv_bytes"]) for r in port_rows),
        sum_delta_collective_bytes=sum(int(r["delta_collective_bytes"]) for r in port_rows))
    shared_resource = dict(
        directed_endpoint_outstanding_overlap_seconds=endpoint_overlap,
        directed_endpoints_with_both_classes_in_flight=len(endpoint_overlap),
        any_directed_endpoint_overlap_seconds=baseline["any_endpoint_outstanding_overlap_seconds"],
        ports_with_both_traffic_classes=len(mixed_ports),
        ports_with_both_classes_detail={
            k: dict(node_id=p["node_id"], peer_id=p["peer_id"], capacity_bps=p["capacity_bps"],
                    kv_bytes=p["kv_bytes"], collective_bytes=p["ep_bytes"],
                    tx_bytes=p["tx_bytes"], mixed_1ms_samples=p["mixed_1ms_samples"],
                    active_1ms_samples=p["active_1ms_samples"],
                    peak_1ms_utilization=p["peak_1ms_utilization"],
                    peak_queue_bytes=p["peak_queue_bytes"],
                    pfc_pause_events=p["pfc_pause_events"],
                    mean_utilization_during_flow_span=p["mean_utilization_during_flow_span"])
            for k, p in sorted(mixed_ports.items())},
        one_ms_port_samples=port_interval_summary,
        granularity_note=("Endpoint overlap is exact event-based outstanding duration for one directed "
                          "NIC (tx:/rx: endpoint); 1 ms port counters are cumulative-byte differences "
                          "per sampled egress resource. Neither is per-packet service time."))

    # Transport-level contribution location (never summed into request loss).
    def contributions(kind):
        ids = [i for i, r in enumerate(flows) if r[5] == kind]
        by_nic, by_request, by_stage = defaultdict(list), defaultdict(list), defaultdict(list)
        for i in ids:
            d = (full[i] - isolated[kind][i]) / 1000
            if d <= 0:
                continue
            r = flows[i]
            by_nic["tx:gpu%d(node%d,leaf%d)" % (r[2], r[2] // 8, r[2] // 8 // 2)].append(d)
            by_nic["rx:gpu%d(node%d,leaf%d)" % (r[3], r[3] // 8, r[3] // 8 // 2)].append(d)
            by_request[r[8]].append(d)
            s = stages[r[7]] if r[7] >= 0 else None
            by_stage[(s["phase"] + "." + s["kind"]) if s else "kv"].append(d)
        def top(d, n=8):
            return [dict(key=k, count=len(v), positive_added_us_sum=sum(v),
                         positive_added_us_max=max(v)) for k, v in
                    sorted(d.items(), key=lambda kv: -sum(kv[1]))[:n]]
        return dict(by_directed_nic=top(by_nic), by_request=top(by_request),
                    by_stage_kind=top(by_stage))

    # ------------------------------------------------------ request impact
    kv_flows_by_request = defaultdict(list)
    for i, r in enumerate(flows):
        if r[5] == "kv":
            kv_flows_by_request[r[8]].append(i)
    kv_all_received_live = {}
    for r in boundaries:
        if r["event"] == "kv_all_received":
            kv_all_received_live[r["request_id"]] = r["time_ns"]
    prefill_end = {}
    for r in boundaries:
        if r["event"] == "prefill_chunk_completed":
            prefill_end[r["request_id"]] = max(prefill_end.get(r["request_id"], 0), r["time_ns"])
    first_decode_start = {}
    for r in batches:
        if r["event"] == "decode_batch_started":
            for rid in r.get("request_ids", []):
                first_decode_start[rid] = min(first_decode_start.get(rid, r["time_ns"]), r["time_ns"])

    impact_rows = []
    for row in requests_csv:
        rid = row["request_id"]
        ids = kv_flows_by_request.get(rid, [])
        t_kv_both = max((full[i] for i in ids), default=None)
        t_kv_iso = max((acks["kv"][i] for i in ids), default=None)
        t_p = prefill_end.get(rid)
        g_both = max(t_p, t_kv_both) if t_p is not None and t_kv_both is not None else None
        g_iso = max(t_p, t_kv_iso) if t_p is not None and t_kv_iso is not None else None
        delta_g = (g_both - g_iso) if g_both is not None and g_iso is not None else None
        masked = (t_kv_both is not None and t_p is not None and t_kv_both <= t_p and
                  t_kv_iso is not None and t_kv_iso <= t_p)

        p_stage = boundary_stages["prefill_final"].get(rid)
        d_stages = {idx: stage_rows[i]["added_us"] for (r, idx), i in
                    boundary_stages["decode_tokens"].items() if r == rid}
        following = [v for idx, v in d_stages.items() if idx >= 3]
        first_d = d_stages.get(2)
        p_added = stage_rows[p_stage]["added_us"] if p_stage is not None else None
        all_exposure = ([p_added] if p_added is not None else []) + \
                       ([first_d] if first_d is not None else []) + following
        impact_rows.append(dict(
            request_id=rid, status=row["status"],
            arrived_at_seconds=row["arrived_at_seconds"],
            completed_at_seconds=row["completed_at_seconds"],
            output_tokens=row["output_tokens"],
            ttft_seconds=row["ttft_seconds"], handoff_seconds=row["handoff_seconds"],
            later_max_itl_seconds=row["later_max_itl_seconds"],
            max_itl_seconds=row["max_itl_seconds"], mean_tpot_seconds=row["mean_tpot_seconds"],
            e2e_seconds=row["e2e_seconds"], slo_met=row["slo_met"],
            ttft_slo_met=row["ttft_slo_met"], handoff_slo_met=row["handoff_slo_met"],
            later_itl_slo_met=row["later_itl_slo_met"],
            kv_flow_count=len(ids),
            T_P_ns=t_p, T_KV_all_both_ns=t_kv_both, T_KV_all_kv_only_ns=t_kv_iso,
            kv_all_received_live_ns=kv_all_received_live.get(rid),
            T_KV_all_both_matches_live=(
                t_kv_both is not None and rid in kv_all_received_live and
                abs(t_kv_both - kv_all_received_live[rid]) <= 1),
            G_both_ns=g_both, G_isolated_ns=g_iso, delta_G_ns=delta_g,
            delta_G_us=(delta_g / 1000 if delta_g is not None else None),
            delta_G_masked_by_prefill=masked,
            first_decode_batch_start_ns=first_decode_start.get(rid),
            decode_queue_wait_after_gate_ns=(
                first_decode_start[rid] - g_both if rid in first_decode_start and g_both is not None else None),
            final_prefill_combine_stage_index=p_stage,
            final_prefill_combine_added_us=p_added,
            first_decode_token_combine_added_us=first_d,
            following_decode_token_count=len(following),
            following_decode_combine_added_us_mean=(statistics.mean(following) if following else None),
            following_decode_combine_added_us_max=(max(following) if following else None),
            following_decode_tokens_delayed_over_1us=sum(v > 1 for v in following),
            mapped_decode_token_boundaries=len(d_stages),
            max_boundary_exposure_us=(max(all_exposure) if all_exposure else None),
            max_exposure_over_ttft=(max(all_exposure) / (float(row["ttft_seconds"]) * 1e6)
                                   if all_exposure and float(row["ttft_seconds"]) else None)))

    with (RESULTS / "request-impact.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        cols = list(impact_rows[0].keys())
        w.writerow(cols)
        for r in impact_rows:
            w.writerow([r[c] for c in cols])

    mapped_tokens = len(boundary_stages["decode_tokens"])
    expected_decode_tokens = sum(int(r["output_tokens"]) - 1 for r in requests_csv)
    require(mapped_tokens == expected_decode_tokens,
            "decode final-combine mapping incomplete: %d vs %d" % (mapped_tokens, expected_decode_tokens))
    require(len(boundary_stages["prefill_final"]) == len(requests_csv),
            "prefill final-combine mapping incomplete")
    require(all(r["T_KV_all_both_matches_live"] for r in impact_rows),
            "T_KV_all^both does not equal live kv_all_received")
    # Exact identity between final-layer combine completion and returned tokens.
    token_identity_errors = []
    for (rid, token_index), i in boundary_stages["decode_tokens"].items():
        expected = token_ns.get((rid, token_index))
        if expected is None or abs(stages[i]["ack_ns"] - expected) > 1:
            token_identity_errors.append((rid, token_index, stages[i]["ack_ns"], expected))
    require(not token_identity_errors, "decode token/final-combine identity mismatch: %s" % token_identity_errors[:5])
    first_token_errors = []
    for rid, i in boundary_stages["prefill_final"].items():
        expected = token_ns.get((rid, 1))
        if expected is None or abs(stages[i]["ack_ns"] - expected) > 1:
            first_token_errors.append((rid, stages[i]["ack_ns"], expected))
    require(not first_token_errors, "first-token/final-combine identity mismatch: %s" % first_token_errors)

    delta_g_values = [r["delta_G_us"] for r in impact_rows if r["delta_G_us"] is not None]
    all_token_exposure = []
    for r in impact_rows:
        all_token_exposure.extend([r["final_prefill_combine_added_us"],
                                   r["first_decode_token_combine_added_us"]])
    all_token_exposure = [v for v in all_token_exposure if v is not None]
    for r in impact_rows:
        pass
    # per-token following exposures are re-read from the stage rows for exact stats
    following_all = [stage_rows[i]["added_us"] for (rid, idx), i in
                     boundary_stages["decode_tokens"].items() if idx >= 3]
    total_boundary_exposure = all_token_exposure + following_all

    impact_summary = dict(
        schema_version=1,
        actual_end_to_end=dict(
            completion_window=live_summary["completion_window"],
            arrival_cohort=live_summary["arrival_cohort"],
            latencies=live_summary["latencies"],
            pressure=live_summary["pressure"],
            per_request="results/request-impact.csv"),
        layer1_observed_network_slowdown=dict(
            supported=any(c["added_latency_us"]["maximum"] and c["added_latency_us"]["maximum"] > 0
                          for c in classes.values()),
            classes=classes, ep_stage_groups=stage_groups,
            note="Frozen-submission transport counterfactual; not end-to-end request causality."),
        layer2_exposed_to_request_necessary_boundary=dict(
            d_admission_gate=dict(delta_G_us=stats(delta_g_values),
                                  requests_with_nonzero=sum(v != 0 for v in delta_g_values),
                                  requests_masked_by_prefill=sum(r["delta_G_masked_by_prefill"] for r in impact_rows)),
            final_layer_combine=dict(
                prefill_final_stages=len(boundary_stages["prefill_final"]),
                decode_token_stages=mapped_tokens,
                expected_decode_token_stages=expected_decode_tokens,
                first_decode_token_stages=sum(1 for (r, i) in boundary_stages["decode_tokens"] if i == 2),
                following_decode_token_stages=len(following_all),
                added_latency_us=stats(total_boundary_exposure),
                stages_delayed_over_1us=sum(v > 1 for v in total_boundary_exposure),
                stages_improved_over_1us=sum(v < -1 for v in total_boundary_exposure)),
            requests_with_any_boundary_exposure_over_1us=sum(
                (r["max_boundary_exposure_us"] or 0) > 1 for r in impact_rows),
            tokens_with_final_combine_exposure_over_1us=sum(v > 1 for v in total_boundary_exposure),
            mapping_identity=dict(decode_token_errors=len(token_identity_errors),
                                  first_token_errors=len(first_token_errors),
                                  all_8_requests_mapped=len(boundary_stages["prefill_final"]) == 8,
                                  all_decode_tokens_mapped=mapped_tokens == expected_decode_tokens)),
        layer3_quantified_end_to_end_slo_change=dict(
            status="not_measured",
            reason=("No counterfactual request trajectory was re-executed: replay freezes submissions "
                    "and never reruns Prefill/Decode/scheduler. Therefore no counterfactual goodput, "
                    "TTFT, handoff or ITL exists, and none may be inferred from flow/stage deltas.")),
        magnitude_vs_actual_ms=dict(
            actual_ttft_mean_ms=live_summary["latencies"]["ttft_seconds"]["mean"] * 1000,
            actual_handoff_mean_ms=live_summary["latencies"]["handoff_seconds"]["mean"] * 1000,
            actual_later_max_itl_mean_ms=live_summary["latencies"]["later_max_itl_seconds"]["mean"] * 1000,
            max_boundary_exposure_us=max(total_boundary_exposure, default=None),
            max_delta_G_us=max(delta_g_values, default=None)),
        errors=errors,
        all_checks_passed=not errors)
    dump(RESULTS / "request-impact-summary.json", impact_summary)

    # ------------------------------------------------------- metrics summary
    metrics = dict(schema_version=1, source="results/diagnostic/run/result/summary.json",
                   completion_window=live_summary["completion_window"],
                   arrival_cohort=live_summary["arrival_cohort"],
                   latencies=live_summary["latencies"], pressure=live_summary["pressure"],
                   recording=live_summary["recording"],
                   prefill=json.loads((RUN / "prefill_chunk_summary.json").read_text(encoding="utf-8")),
                   prefill_batches=json.loads((RUN / "prefill_batch_summary.json").read_text(encoding="utf-8")),
                   decode_batches=json.loads((RUN / "decode_batch_summary.json").read_text(encoding="utf-8")),
                   expert_parallel=json.loads((RUN / "expert_parallel_summary.json").read_text(encoding="utf-8")),
                   communication=json.loads((RUN / "observation/summary.json").read_text(encoding="utf-8"))["communication"],
                   final_resources_zero=all(v["occupied_bytes"] == 0 and v["reserved_bytes"] == 0
                                            for v in json.loads((RUN / "final_resources.json").read_text(encoding="utf-8")).values()),
                   per_request=requests_csv)
    dump(RESULTS / "metrics-summary.json", metrics)

    # --------------------------------------------------- contention summary
    contention_summary = dict(
        schema_version=1,
        experiment_type="frozen_network_replay",
        baseline_flow_replay_max_ack_error_ns=fidelity["max_abs_flow_ack_error_ns"],
        baseline_stage_replay_max_ack_error_ns=fidelity["max_abs_ep_stage_error_ns"],
        stage_reconstruction_vs_live_max_error_ns=stage_live_error,
        traffic_classes=classes,
        ep_stage_groups=stage_groups,
        shared_resource=shared_resource,
        positive_transport_contribution_locations={
            kind: contributions(kind) for kind in ("collective", "kv")},
        replays={k: dict(status=v["status"], wall_clock_seconds=v["wall_clock_seconds"],
                         selected_flows=v["selected_flows"], completed_flows=v["completed_flows"],
                         port_cooldown_checks=v["port_cooldown_checks"])
                 for k, v in replay_evidence.items()},
        method=("Same frozen starts, payloads, priorities, per-endpoint order and native TIMER/LOCAL/"
                "LOCAL_PORTS skeleton; only cross-node SEND of the removed class is omitted. "
                "Flow delta = ACK_both - ACK_isolated (signed); EP stage gate = max(frozen local_ack, "
                "corresponding remote flow ACK). Negative deltas are retained."),
        limits=["Finite single-seed 8-request diagnostic; no confidence interval or full-load claim.",
                "Replay freezes submissions: deltas are transport effects, not request/SLO gains.",
                "1 ms mixed-byte samples do not resolve individual packet service or strict priority.",
                "Endpoint outstanding overlap includes pacing/ACK wait, not just queueing delay.",
                "EP/KV logical payloads are not interchangeable with port byte counters (headers/retransmission).",
                "Estimated compute profile, synthetic routing and artificial SLO thresholds inherited."])
    dump(RESULTS / "contention-summary.json", contention_summary)

    # --------------------------------------------- replay inputs manifest
    manifest = dict(schema_version=1,
                    calendar_sha256=sha(DIAG / "native_schedule.jsonl"),
                    flows_csv_sha256=sha(DIAG / "flows.csv"),
                    stages_json_sha256=sha(DIAG / "stages.json"),
                    config_sha256=sha(DIAG / "config.json"),
                    inputs={name: sha(DIAG / "inputs" / name) for name in
                            ("compute_profile.json", "workload.json", "binding_plan.json")},
                    shared_source=dict(manifest_sha256=sha(DIAG / "inputs/source/manifest.json"),
                                       archive_sha256=sha(DIAG / "inputs/source/source.tar.gz"),
                                       source_tree_sha256=json.loads(
                                           (DIAG / "inputs/source/manifest.json").read_text(encoding="utf-8"))["source_tree_sha256"]),
                    backend_manifest_sha256=sha(ROOT / "joint_scheduling/artifacts/backend/current/build_manifest.json"),
                    event_links_sha256=sha(RESULTS / "event-links.jsonl"),
                    kv_transfer_links_sha256=sha(RESULTS / "kv-transfer-links.json"),
                    port_intervals_sha256=sha(RESULTS / "port-intervals.csv"),
                    replay_outputs={name: {p.relative_to(DIAG / ("replay-" + name)).as_posix(): sha(p)
                                           for p in sorted((DIAG / ("replay-" + name)).rglob("*")) if p.is_file()}
                                    for name in ("both", "collective", "kv")},
                    scope="Replay can be reconstructed from these files alone; no live process state is required.")
    dump(RESULTS / "replay-inputs-manifest.json", manifest)

    # ------------------------------------------------ acceptance summary
    wall = {k: v["wall_clock_seconds"] for k, v in replay_evidence.items()}
    wall["live"] = live_stage["wall_clock_seconds"]
    items = [
        dict(id="A1", status="passed" if (identity["manifest_sha256_match"] and
             all(v["match"] for v in identity["frozen_files"].values()) and
             environment["all_passed"] and
             len(sample_manifest["config_diff_vs_frozen"]) == 3 and
             all(p["endpoints_equal"] and p["full_row_equal"] for p in sample_manifest["binding_projection"]))
             else "failed",
             note="Frozen manifest 811cc296..., 8 original requests, parent binding projection, only permitted config diffs; source/backend identical to accepted T002-r03.",
             evidence=["results/identity-check.json", "results/environment-check.json", "inputs/sample-manifest.json"]),
        dict(id="A2", status="passed" if mechanism["all_passed"] and live_stage["status"] == "completed" else "failed",
             note="8 requests / 550 tokens completed, 11 chunks / 542 decode steps / 32074 EP calls, KV logical payload 274974336 B, 32 memory domains empty, EP audit passed.",
             evidence=["results/mechanism-checks.json", "results/live-stage.json", "results/metrics-summary.json", "results/diagnostic/run"]),
        dict(id="A3", status="passed" if fidelity["all_passed"] else "failed",
             note="Both replay drained; per-flow and per-EP-stage error <=1 ns; separate-by-class and port cooldown checks retained; native TIMER/LOCAL/LOCAL_PORTS skeleton preserved.",
             evidence=["results/replay-fidelity.json", "results/diagnostic/replay-both"]),
        dict(id="A4", status="passed" if all(replay_evidence[k]["status"] == "completed" for k in ("both", "collective", "kv"))
             and not errors and Path(RESULTS / "flow-comparison.csv").is_file()
             and Path(RESULTS / "stage-comparison.csv").is_file() else "failed",
             note="Two removal replays with signed flow/stage deltas, four EP phase/kind groups and directed-NIC/1 ms port sharing evidence. Absence of contention would also be a valid result.",
             evidence=["results/contention-summary.json", "results/flow-comparison.csv", "results/stage-comparison.csv", "results/port-intervals.csv"]),
        dict(id="A5", status="passed" if impact_summary["all_checks_passed"] and
             impact_summary["layer2_exposed_to_request_necessary_boundary"]["mapping_identity"]["all_8_requests_mapped"] and
             impact_summary["layer2_exposed_to_request_necessary_boundary"]["mapping_identity"]["all_decode_tokens_mapped"] else "failed",
             note="All 8 KV admission gates and all 550 token final-layer combine stages mapped by real identity; conditional exposure alongside actual metrics; end-to-end counterfactual explicitly not measured.",
             evidence=["results/event-links.jsonl", "results/request-impact.csv", "results/request-impact-summary.json"]),
        dict(id="A6", status="passed" if all(v <= limit for v, limit in
             ((wall["live"], 1500), (wall["both"], 900), (wall["collective"], 900), (wall["kv"], 900))) else "failed",
             note="Budget and scope recorded; single seed, no algorithm comparison, no scale-out, no full trace; negative results allowed.",
             evidence=["REPORT.md", "results/acceptance-summary.json", "logs/commands.md"]),
    ]
    acceptance = dict(schema_version=1, task_id="T003", status="submitted" if
                      all(i["status"] == "passed" for i in items) else "changes_needed",
                      all_passed=all(i["status"] == "passed" for i in items),
                      items=items, wall_clock_seconds=wall,
                      ns3_simulation_time_limit_seconds=30.0,
                      network_replay_deadline_rule="max(30 s, last baseline ACK + 1 s)",
                      errors=errors,
                      limitations=["8 requests, all batches size 1; no sustained load or batch>1 coverage.",
                                   "Single seed 7; no confidence interval.",
                                   "End-to-end counterfactual SLO/goodput change not measured.",
                                   "Estimated profile, synthetic routing, artificial SLO."])
    dump(RESULTS / "acceptance-summary.json", acceptance)

    print(json.dumps(dict(all_passed=acceptance["all_passed"],
                          statuses={i["id"]: i["status"] for i in items},
                          errors=errors,
                          replay_error_ns=fidelity["max_abs_flow_ack_error_ns"],
                          stage_error_ns=fidelity["max_abs_ep_stage_error_ns"],
                          mapped_decode_tokens=mapped_tokens,
                          expected_decode_tokens=expected_decode_tokens,
                          mixed_ports=len(mixed_ports),
                          mixed_1ms_intervals=mixed_intervals,
                          classes={k: dict(count=v["flow_count"],
                                           delayed_gt_1us=v["delayed_by_over_1us"]["count"],
                                           max_added_us=v["added_latency_us"]["maximum"])
                                   for k, v in classes.items()}), ensure_ascii=False))
    return 0 if acceptance["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
