"""Shared-resource token admission, collective guards and request slack order."""

from collections import deque
import math
from dataclasses import dataclass, field

from . import TransferPermit


class NetworkResources:
    def __init__(self, topology, initial_resources):
        self.topology = topology
        self.capacities = {key: row["capacity_bps"] / 8 for key, row in initial_resources.items()}
        self.edges = [(r["node_id"], r["peer_id"], key) for key, r in initial_resources.items() if not key.startswith("local:")]
        self.adjacency = {}
        for src, dst, key in self.edges:
            self.adjacency.setdefault(src, []).append(dst)
        self.cache = {}

    def for_gpus(self, source, destination):
        src, dst = source // self.topology.gpus_per_node, destination // self.topology.gpus_per_node
        if src == dst:
            return ("local:{}".format(src),)
        if (src, dst) not in self.cache:
            def distances(root):
                result, pending = {root: 0}, deque([root])
                while pending:
                    node = pending.popleft()
                    for peer in self.adjacency.get(node, ()):
                        if peer not in result:
                            result[peer] = result[node] + 1
                            pending.append(peer)
                return result
            left, right = distances(src), distances(dst)
            if dst not in left:
                raise ValueError("KV endpoints are disconnected")
            # ECMP is not exposed as a fixed application route: charge the
            # union of all shortest-path directed edges conservatively.
            self.cache[src, dst] = tuple(sorted(key for a, b, key in self.edges
                if a in left and b in right and left[a]+1+right[b] == left[dst]))
        return self.cache[src, dst]

    def ring_work(self, ranks, size):
        if len(ranks) <= 1:
            return {}
        chunk = (size + len(ranks)-1) // len(ranks)
        work = {}
        for index, source in enumerate(ranks):
            for key in self.for_gpus(source, ranks[(index+1) % len(ranks)]):
                work[key] = work.get(key, 0) + chunk * 2 * (len(ranks)-1)
        return work


@dataclass(frozen=True)
class RequestBudget:
    arrived_at_seconds: float
    ttft_deadline_seconds: float
    predicted_first_token_seconds: float
    latest_kv_delivery_seconds: float
    remaining_kv_bytes: int
    remaining_transfer_seconds: float
    slack_seconds: float
    normalized_slack: float
    ttft_feasible: bool
    decode_instance_id: str = ""
    final_layer_id: int = -1
    remaining_resource_bytes: dict = field(default_factory=dict)
    production_ready_seconds: float = 0.0
    decode_wait_seconds: float = 0.0
    decode_step_seconds: float = 0.0
    expert_queue_wait_seconds: float = 0.0

    @property
    def viable(self):
        return self.ttft_feasible and self.slack_seconds >= 0


class AdaptiveKVPolicy:
    def __init__(self, config, resources, priority="slo", decision_delay_seconds=0.0):
        self.config, self.resources = config, resources
        self.priority = priority if config.slo_priority_enabled else "fifo"
        self.pending, self.inflight = {}, {}
        self.decode_owners = {}
        self.started_at = {}
        self.decision_delay_seconds = decision_delay_seconds
        self.last_time = 0.0
        self.states = {key: dict(tokens=0.0, rate=0.0, inflight_bytes=0, protected=False, collective_reserved=False,
                                estimated_rate=value*(1-config.safety_margin_fraction), queue_bytes=0,
                                utilization=0.0, inflight_limit_bytes=config.max_resource_inflight_bytes,
                                pipeline_limit_bytes=config.max_resource_inflight_bytes,
                                collective_limit_bytes=None, next_collective_seconds=None, completion_latency_seconds=None,
                                completion_samples=0) for key, value in resources.capacities.items()}
        self.last_decision = {}
        self.summary = dict(decisions=0, admitted_blocks=0, admitted_bytes=0,
                            peak_pending_blocks=0, peak_inflight_bytes=0, completion_samples=0, blocked_checks={},
                            decode_slots_acquired=0, decode_slots_released=0, peak_decode_owners={})

    def on_layer_ready(self, blocks):
        for block in blocks:
            if block.block_id in self.pending or block.block_id in self.inflight:
                raise ValueError("duplicate KV block")
            self.pending[block.block_id] = block
        self.summary["peak_pending_blocks"] = max(self.summary["peak_pending_blocks"], len(self.pending))

    def estimate_rate(self, key):
        # A temporary collective guard is not a claim that future KV service
        # is impossible; keep a conservative positive rate for the predictor.
        capacity = self.resources.capacities[key]
        return max(capacity * 0.05, self.states[key]["estimated_rate"])

    def refresh(self, now, network, collectives):
        if now < self.last_time-1e-12:
            raise ValueError("KV budget clock moved backwards")
        if network is not None and network["visible_at_seconds"] > now+1e-12:
            raise ValueError("network observation is not visible yet")
        elapsed = max(0, now-self.last_time)
        for key, state in self.states.items():
            state["tokens"] = min(self.config.max_resource_inflight_bytes, state["tokens"] + state["rate"] * elapsed)
            capacity = self.resources.capacities[key]
            port = network["resources"].get(key) if network is not None else None
            queue = port["queue_bytes"] if port else 0
            guard = self.config.collective_guard_us / 1e6 + (state["inflight_bytes"] +
                self.config.max_block_bytes + queue) / (capacity*(1-self.config.safety_margin_fraction))
            starts = [demand["starts_at_seconds"] for demand in collectives if key in demand["resources"]
                      and self.config.collective_protection_enabled]
            reserved = any(start <= now+guard for start in starts)
            protected = reserved and (self.config.pause_on_collective or queue >= self.config.queue_target_bytes
                                       or port is not None and port.get("pfc_pause_delta", 0) > 0)
            rate = 0.0
            if port is not None:
                residual = max(0.0, capacity * (1-self.config.safety_margin_fraction)
                               - port["collective_bytes_per_second"] - port["other_bytes_per_second"])
                rate = residual / (1 + port["queue_bytes"] / self.config.queue_target_bytes)
            estimate = rate
            if port is not None and reserved:
                residual = max(0.0, capacity*(1-self.config.safety_margin_fraction)
                    - max(port["collective_bytes_per_second"], capacity*self.config.collective_reserve_fraction)
                    - port["other_bytes_per_second"])
                rate = residual / (1+queue/self.config.queue_target_bytes)
                if self.config.inflight_budget_mode == "fixed":
                    state["tokens"] = min(state["tokens"], self.config.max_block_bytes)
            rate = 0.0 if protected else rate
            pipeline_limit = self.config.max_resource_inflight_bytes
            collective_limit = None
            if self.config.inflight_budget_mode == "adaptive":
                # A byte window covers service until an ACK and the next decision.
                # Only completed transfers contribute latency samples; no future
                # completion times or instantaneous hidden port state are used.
                horizon = max(self.config.control_interval_us/1e6,
                              state["completion_latency_seconds"] or 0) + self.decision_delay_seconds
                pipeline_limit = min(pipeline_limit, max(self.config.max_block_bytes, math.ceil(rate*horizon)))
                if starts:
                    # Conservative drain envelope: all unacknowledged KV bytes
                    # plus the observed queue must fit before the next collective
                    # plus its explicitly allowed blocking time. Do not round up
                    # to one block when a whole block cannot fit.
                    drain_time = max(0, min(starts)-now) + self.config.collective_max_blocking_us/1e6
                    collective_limit = max(0, math.floor(capacity*(1-self.config.safety_margin_fraction)*drain_time-queue))
                limit = min(pipeline_limit, collective_limit) if collective_limit is not None else pipeline_limit
            else:
                limit = self.config.max_block_bytes if reserved else pipeline_limit
            if protected or port is None:
                limit = 0
            state["tokens"] = min(state["tokens"], limit)
            state.update(rate=0.0 if protected else rate, estimated_rate=estimate, collective_reserved=reserved,
                         protected=protected, queue_bytes=port["queue_bytes"] if port else 0,
                         utilization=port["utilization"] if port else 0.0,
                         inflight_limit_bytes=limit, pipeline_limit_bytes=pipeline_limit,
                         collective_limit_bytes=collective_limit, next_collective_seconds=min(starts) if starts else None)
            if protected or port is None:
                state["tokens"] = 0.0
        self.last_time = now

    def admit(self, now_seconds, observed_resources):
        network = observed_resources["network"]
        self.refresh(now_seconds, network, observed_resources["collectives"])
        budgets = observed_resources["requests"]
        def order(block):
            budget = budgets[block.request_id]
            tail = (block.ready_at_seconds, block.layer_id, block.source_gpu_id, block.offset_bytes, block.block_id)
            if self.priority == "fifo":
                return (budget.arrived_at_seconds,) + tail
            # Feasible requests get first chance; overdue work remains queued
            # and is served best-effort in arrival order, never silently dropped.
            return (0 if budget.viable else 1,
                    budget.normalized_slack if budget.viable else budget.arrived_at_seconds,
                    budget.arrived_at_seconds) + tail
        permits, blocked = [], {}
        inflight_bytes = sum(block.size_bytes for block in self.inflight.values())
        for block in sorted(self.pending.values(), key=order):
            reason = None
            budget = budgets[block.request_id]
            guarded_tail = self.config.decode_admission_limit > 0 and block.layer_id == budget.final_layer_id
            if self.config.decode_admission_limit > 0 and (not budget.decode_instance_id or budget.final_layer_id < 0):
                raise ValueError("decode admission requires a destination instance and final-layer metadata")
            keys = self.resources.for_gpus(block.source_gpu_id, block.destination_gpu_id)
            if network is None:
                reason = "observation_not_visible"
            elif guarded_tail and block.request_id not in self.decode_owners and sum(
                    owner == budget.decode_instance_id for owner in self.decode_owners.values()
                    ) >= self.config.decode_admission_limit:
                reason = "decode_admission"
            elif len(self.inflight) >= self.config.max_concurrent_transfers or inflight_bytes+block.size_bytes > self.config.max_inflight_bytes:
                reason = "global_inflight_limit"
            elif any(self.states[key]["protected"] for key in keys):
                reason = "collective_protection"
            elif any(self.states[key]["collective_limit_bytes"] is not None and
                     self.states[key]["inflight_bytes"]+block.size_bytes > self.states[key]["collective_limit_bytes"]
                     or self.config.inflight_budget_mode == "fixed" and self.states[key]["collective_reserved"] and
                     self.states[key]["inflight_bytes"]+block.size_bytes > self.config.max_block_bytes for key in keys):
                reason = "collective_protection"
            elif any(self.states[key]["inflight_bytes"]+block.size_bytes > self.states[key]["inflight_limit_bytes"] for key in keys):
                reason = "resource_inflight_limit"
            elif any(self.states[key]["tokens"]+1e-7 < block.size_bytes for key in keys):
                reason = "bandwidth_tokens"
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
                continue
            if guarded_tail and block.request_id not in self.decode_owners:
                # Reserve before the first tail block starts, so concurrent ACKs
                # cannot over-admit. KV completion does not release this slot;
                # it belongs to the request until its output is fully served.
                self.decode_owners[block.request_id] = budget.decode_instance_id
                self.summary["decode_slots_acquired"] += 1
                count = sum(owner == budget.decode_instance_id for owner in self.decode_owners.values())
                peaks = self.summary["peak_decode_owners"]
                peaks[budget.decode_instance_id] = max(peaks.get(budget.decode_instance_id, 0), count)
            for key in keys:
                self.states[key]["tokens"] = max(0.0, self.states[key]["tokens"]-block.size_bytes)
                self.states[key]["inflight_bytes"] += block.size_bytes
            self.inflight[block.block_id] = block
            self.started_at[block.block_id] = now_seconds
            self.pending.pop(block.block_id)
            inflight_bytes += block.size_bytes
            permits.append(TransferPermit(block.block_id, block.size_bytes))
        self.last_decision = dict(blocked=blocked, admitted_blocks=[p.block_id for p in permits],
            observed_sequence=network["sequence"] if network is not None else None,
            observed_at_seconds=network["time_seconds"] if network is not None else None,
            resources={key: dict(value) for key, value in self.states.items()})
        self.summary["decisions"] += 1
        self.summary["admitted_blocks"] += len(permits)
        self.summary["admitted_bytes"] += sum(p.allowed_bytes for p in permits)
        self.summary["peak_inflight_bytes"] = max(self.summary["peak_inflight_bytes"], inflight_bytes)
        for reason, count in blocked.items():
            self.summary["blocked_checks"][reason] = self.summary["blocked_checks"].get(reason, 0) + count
        return tuple(permits)

    def request_completed(self, request_id):
        if self.decode_owners.pop(request_id, None) is not None:
            self.summary["decode_slots_released"] += 1

    def next_credit_delay(self):
        delays = []
        for block in self.pending.values():
            states = [self.states[key] for key in self.resources.for_gpus(block.source_gpu_id, block.destination_gpu_id)]
            if any(s["protected"] or s["rate"] <= 0 for s in states):
                continue
            delays.append(max(max(0, block.size_bytes-s["tokens"])/s["rate"] for s in states))
        return min((delay for delay in delays if delay > 1e-9), default=self.config.control_interval_us/1e6)

    def completed(self, block_id, now_seconds=None):
        started = self.started_at[block_id]
        if now_seconds is not None and (now_seconds < started or now_seconds < self.last_time-1e-12):
            raise ValueError("KV completion clock moved backwards")
        block = self.inflight.pop(block_id)
        self.started_at.pop(block_id)
        for key in self.resources.for_gpus(block.source_gpu_id, block.destination_gpu_id):
            self.states[key]["inflight_bytes"] -= block.size_bytes
            if now_seconds is not None:
                state = self.states[key]
                latency = now_seconds-started
                old = state["completion_latency_seconds"]
                alpha = self.config.completion_feedback_alpha
                state["completion_latency_seconds"] = latency if old is None else alpha*latency+(1-alpha)*old
                state["completion_samples"] += 1
        if now_seconds is not None:
            self.summary["completion_samples"] += 1
