"""Offline workload demand accounting; never starts a simulator or measures capacity."""

import bisect
import math
from collections import defaultdict

from ..workload.trace import profile_coverage


def estimate_capacity(workload, profile, config):
    if config.expert_parallel.enabled or profile.expert is not None:
        raise ValueError("Offline dense capacity accounting does not model EP expert queues or routing")
    if not workload.get("requests"):
        raise ValueError("capacity estimate needs a nonempty workload")
    coverage = profile_coverage(workload, profile)
    if coverage["out_of_coverage_count"]:
        raise ValueError("capacity estimate requires full profile coverage; no clipping is allowed")
    if not profile.layered:
        raise ValueError("capacity accounting requires explicit Prefill layers")
    ps = [p for p in config.topology.placements if p.role == "prefill"]
    ds = [p for p in config.topology.placements if p.role == "decode"]
    topology = config.topology
    for placement in ps + ds:
        if len(placement.gpu_ids) != profile.phases[placement.role]["tp_size"]:
            raise ValueError("placement and public profile TP differ")

    def compile_points(points):
        tokens = [point["tokens"] for point in points]
        def lookup(t):
            if not tokens[0] <= t <= tokens[-1]:
                raise ValueError("capacity query outside profile range")
            index = bisect.bisect_left(tokens, t)
            if index < len(tokens) and tokens[index] == t:
                return points[index]["compute_seconds"], points[index]["collective_bytes"]
            lo, hi = points[index-1], points[index]
            fraction = (t-lo["tokens"])/(hi["tokens"]-lo["tokens"])
            return (lo["compute_seconds"] + fraction*(hi["compute_seconds"]-lo["compute_seconds"]),
                    math.ceil(lo["collective_bytes"] + fraction*(hi["collective_bytes"]-lo["collective_bytes"])))
        return lookup

    p_curves = [(compile_points(layer["points"]), layer["collectives_per_step"])
                for layer in profile.phases["prefill"]["layers"]]
    d_curve = compile_points(profile.phases["decode"]["points"])
    d_groups = profile.phases["decode"]["collectives_per_step"]
    max_context = max((row["input_tokens"] + row["output_tokens"] - 2
                       for row in workload["requests"] if row["output_tokens"] > 1), default=1)
    # Prefix sums account for every actual output step, rather than estimating
    # Decode cost at the mean input length or assuming length independence.
    d_compute = [0.0]
    d_ring_payload = [0.0]
    tp = profile.phases["decode"]["tp_size"]
    for tokens in range(1, max_context + 1):
        if tokens < profile.phases["decode"]["points"][0]["tokens"]:
            d_compute.append(d_compute[-1])
            d_ring_payload.append(d_ring_payload[-1])
            continue
        compute, size = d_curve(tokens)
        d_compute.append(d_compute[-1] + compute)
        d_ring_payload.append(d_ring_payload[-1] + d_groups * 2 * (tp-1) * math.ceil(size/tp))
    p_cache = {}
    compute_demand = defaultdict(float)
    resource_bytes = defaultdict(float)
    capacities = {}
    total_p, total_d, total_kv, total_groups = 0.0, 0.0, 0, 0
    payload_packets_lower_bound = 0
    max_request_memory = 0

    def flow(src, dst, amount):
        nonlocal payload_packets_lower_bound
        if not amount:
            return
        a, b = src // topology.gpus_per_node, dst // topology.gpus_per_node
        if a == b:
            key = "node-{}:local".format(a)
            resource_bytes[key] += amount
            capacities[key] = topology.intra_node_bandwidth_gbps * 1e9 / 8
            return
        for key in ("node-{}:tx".format(a), "node-{}:rx".format(b)):
            resource_bytes[key] += amount
            capacities[key] = topology.nic_bandwidth_gbps * 1e9 / 8
        la, lb = a // topology.nodes_per_leaf, b // topology.nodes_per_leaf
        if la != lb:
            for key in ("leaf-{}:uplink-tx".format(la), "leaf-{}:uplink-rx".format(lb)):
                resource_bytes[key] += amount
                capacities[key] = topology.num_spines * topology.uplink_bandwidth_gbps * 1e9 / 8
        payload_packets_lower_bound += math.ceil(amount/config.runtime.packet_payload_bytes)

    def ring(placement, per_edge_payload):
        ranks = placement.gpu_ids
        for rank, src in enumerate(ranks):
            flow(src, ranks[(rank + 1) % len(ranks)], per_edge_payload)

    for index, row in enumerate(workload["requests"]):
        inp, out = row["input_tokens"], row["output_tokens"]
        p, d = ps[index % len(ps)], ds[index % len(ds)]
        if inp not in p_cache:
            p_cost, p_edge, p_groups = 0.0, 0, 0
            for curve, groups in p_curves:
                compute, size = curve(inp)
                p_cost += compute
                p_groups += groups
                p_edge += groups * 2 * (len(p.gpu_ids)-1) * math.ceil(size/len(p.gpu_ids))
            p_cache[inp] = p_cost, p_edge, p_groups
        p_cost, p_edge, p_groups = p_cache[inp]
        d_cost = d_compute[inp+out-2] - d_compute[inp-1] if out > 1 else 0.0
        d_edge = d_ring_payload[inp+out-2] - d_ring_payload[inp-1] if out > 1 else 0.0
        compute_demand[p.instance_id] += p_cost
        compute_demand[d.instance_id] += d_cost
        total_p += p_cost
        total_d += d_cost
        total_groups += p_groups + d_groups * (out-1)
        ring(p, p_edge)
        ring(d, d_edge)
        if out > 1:
            base, extra = divmod(inp*profile.kv_bytes_per_token, len(p.gpu_ids))
            for rank, (src, dst) in enumerate(zip(p.gpu_ids, d.gpu_ids)):
                flow(src, dst, base + int(rank < extra))
            total_kv += inp * profile.kv_bytes_per_token
        reserved_output = row.get("max_output_tokens", config.reservations.reserve_max_output_tokens)
        max_request_memory = max(max_request_memory, math.ceil((inp + max(out, reserved_output))*profile.kv_bytes_per_token/len(d.gpu_ids)))
    count = len(workload["requests"])
    resource_rows = [dict(resource=key, bytes_per_global_request=value/count,
                          capacity_bytes_per_second=capacities[key],
                          optimistic_payload_bound_rps=capacities[key]/(value/count))
                     for key, value in sorted(resource_bytes.items()) if value]
    p_bound = min(count/compute_demand[p.instance_id] for p in ps if compute_demand[p.instance_id])
    d_values = [count/compute_demand[d.instance_id] for d in ds if compute_demand[d.instance_id]]
    d_bound = min(d_values) if d_values else None
    network_bound = min(row["optimistic_payload_bound_rps"] for row in resource_rows)
    reference = min(value for value in (p_bound, d_bound, network_bound) if value is not None)
    free_memory = int(topology.gpu_memory_gib*2**30*(1-config.reservations.capacity_safety_fraction))-profile.weights_bytes_per_gpu
    return dict(status="offline_estimate_not_measured", sustainable_capacity_rps=None,
                reference_definition="Minimum compute-only instance limit and average per-resource payload bandwidth limit. Optimistic coordinate for selecting calibration inputs; not sustainable capacity or SLO capacity.",
                request_count=count, mean_prefill_compute_seconds=total_p/count,
                mean_decode_compute_seconds_per_request=total_d/count,
                mean_kv_bytes_per_request=total_kv/count,
                prefill_compute_bound_rps=p_bound, decode_compute_bound_rps=d_bound,
                network_payload_bound_rps=network_bound, optimistic_reference_rps=reference,
                mean_compute_demand_by_instance={key:value/count for key,value in sorted(compute_demand.items())},
                resource_demands=resource_rows, modeled_collective_group_count=total_groups,
                network_payload_packet_count_lower_bound=payload_packets_lower_bound,
                available_kv_bytes_per_gpu=free_memory,
                maximum_standalone_request_reservation_bytes_per_gpu=max_request_memory,
                all_standalone_requests_fit_memory=max_request_memory <= free_memory,
                profile_coverage=coverage,
                excluded_from_estimate=["queueing and burst duration", "packet headers, ACKs, congestion, PFC, ECMP imbalance",
                    "ring propagation/barrier/serialization delay added to instance execution", "CPU and missing profile kernels", "multi-request continuous batching"])
