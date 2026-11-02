"""Pure, bounded rollout of a whole binding plan over visible state.

The shadow clock never advances ns-3. Legacy P/D uses FIFO/token round-robin;
chunk/batch configurations use the shared bounded local execution forecast.
layer-ready KV and ring collectives compete on directed shared resources.
Network service is a fluid approximation, not an RDMA/DCQCN replica.
"""

import copy
import hashlib
import heapq
import math
from collections import deque

from . import RequestPrediction
from .execution import batched_execution
from ..kv_scheduler.adaptive import NetworkResources
from ..prefix_cache import PrefixCache
from ..prefix_cache.layout import PrefixPlan
from ..reservations import PhysicalObjectReservation
from ..reservations.ledger import CapacityLedger
from ..topology import build_manifest


def topology_resources(topology):
    graph = build_manifest(topology)
    switches = {s["id"]: i for i, s in enumerate(graph["switches"], topology.num_nodes)}
    def node(name):
        return int(name.split("-")[1]) if name.startswith("node-") else switches[name]
    result = {}
    for link in graph["links"]:
        a, b = node(link["src"]), node(link["dst"])
        for src, dst in ((a, b), (b, a)):
            key = "edge:{}:{}".format(src, dst)
            result[key] = dict(node_id=src, peer_id=dst,
                               capacity_bps=link["bandwidth_bytes_per_second"]*8)
    for host in range(topology.num_nodes):
        result["local:{}".format(host)] = dict(node_id=host, peer_id=host,
            capacity_bps=topology.intra_node_bandwidth_gbps*1e9)
    return result


class _Rollout:
    def __init__(self, config, profile, state, resources):
        self.config, self.profile, self.state, self.resources = config, profile, state, resources
        self.now = state["observed_at_seconds"]
        self.deadline = state["now_seconds"]+config.binding.prediction_time_limit_seconds
        self.events, self.flows, self.sequence, self.steps = [], {}, 0, 0
        self.jobs = {}
        self.queues = {p.instance_id: deque() for p in config.topology.placements}
        self.active = {p.instance_id: None for p in config.topology.placements}
        self.placements = {p.instance_id: p for p in config.topology.placements}
        self.capacities, self.queue_delays = {}, {}
        observed = state.get("network")
        ports = {(p["node_id"], p["peer_id"]): p for p in observed["resources"].values()} if observed else {}
        for key, capacity in resources.capacities.items():
            raw = resources.initial[key]
            port = ports.get((raw["node_id"], raw["peer_id"]), {})
            available = max(capacity*.01, capacity*(1-config.kv_scheduler.safety_margin_fraction)
                            - port.get("other_bytes_per_second", 0))
            self.capacities[key] = available
            self.queue_delays[key] = self.now+port.get("queue_bytes", 0)/available
        self.truncated = False

    def later(self, delay, callback):
        self.sequence += 1
        heapq.heappush(self.events, (self.now+max(0, delay), self.sequence, callback))

    def send(self, source, destination, size, kind, callback):
        keys = self.resources.for_gpus(source, destination)
        latency = (self.config.topology.intra_node_latency_us if keys[0].startswith("local:") else
                   self.config.topology.link_latency_us*(2 if source//self.config.topology.gpus_per_node//self.config.topology.nodes_per_leaf ==
                        destination//self.config.topology.gpus_per_node//self.config.topology.nodes_per_leaf else 4))/1e6
        def start():
            self.sequence += 1
            self.flows[self.sequence] = dict(left=float(size), keys=keys, kind=kind,
                                            done=lambda: self.later(latency, callback))
        self.later(max(0, max((self.queue_delays[k] for k in keys), default=self.now)-self.now), start)

    def rates(self):
        users = {}
        for identity, flow in self.flows.items():
            for key in flow["keys"]:
                users.setdefault(key, []).append(identity)
        shares = {}
        for key, identities in users.items():
            collective = [i for i in identities if self.flows[i]["kind"] == "collective"]
            kv = [i for i in identities if self.flows[i]["kind"] == "kv"]
            reserve = (self.config.kv_scheduler.collective_reserve_fraction
                       if collective and kv and self.config.kv_scheduler.collective_protection_enabled
                       and self.config.runtime.kv_transfer_mode in ("layer_ready_fifo", "layer_ready_slo") else 0)
            collective_fraction = max(reserve, len(collective)/len(identities))
            for identity in identities:
                if reserve:
                    fraction = collective_fraction/len(collective) if identity in collective else (1-collective_fraction)/len(kv)
                else:
                    fraction = 1/len(identities)
                shares[identity, key] = self.capacities[key]*fraction
        return {i: min(shares[i, k] for k in flow["keys"]) for i, flow in self.flows.items()}

    def run(self):
        while self.events or self.flows:
            if self.steps >= self.config.binding.prediction_max_events:
                self.truncated = True
                break
            rates = self.rates()
            timer = self.events[0][0] if self.events else math.inf
            delivery = min((self.now+f["left"]/rates[i] for i, f in self.flows.items()), default=math.inf)
            target = min(timer, delivery, self.deadline)
            elapsed = max(0, target-self.now)
            for identity, flow in self.flows.items():
                flow["left"] = max(0, flow["left"]-rates[identity]*elapsed)
            self.now = target
            self.steps += 1
            finished = [i for i, f in self.flows.items() if f["left"] <= 1e-5]
            callbacks = [self.flows.pop(i)["done"] for i in finished]
            for callback in callbacks:
                callback()
            while self.events and self.events[0][0] <= self.now+1e-14:
                _, _, callback = heapq.heappop(self.events)
                callback()
                self.steps += 1
                if self.steps >= self.config.binding.prediction_max_events:
                    break
            if self.now >= self.deadline:
                self.truncated = bool(self.events or self.flows)
                break

    def allreduce(self, ranks, size, complete):
        if len(ranks) == 1 or not size:
            complete()
            return
        chunk = math.ceil(size/len(ranks))
        def round_at(index):
            if index == 2*(len(ranks)-1):
                complete()
                return
            pending = [len(ranks)]
            def done():
                pending[0] -= 1
                if not pending[0]:
                    round_at(index+1)
            for rank, source in enumerate(ranks):
                self.send(source, ranks[(rank+1) % len(ranks)], chunk, "collective", done)
        round_at(0)

    def phase(self, job, sample, role, complete, resume=False):
        ranks = self.placements[job[role]].gpu_ids
        count = sample["collectives_per_step"]
        part = sample["compute_seconds"]/max(1, count)
        def segment(index, remaining=None, waiting=False):
            if index >= count and count:
                complete()
                return
            def computed():
                if count:
                    self.allreduce(ranks, sample["collective_bytes"], lambda: segment(index+1))
                else:
                    complete()
            if waiting:
                computed()  # conservative replay of the unfinished whole ring
            else:
                self.later(part if remaining is None else remaining, computed)
        if resume:
            segment(job.get("segment_index", 0), job.get("compute_remaining_seconds", part),
                    not job.get("compute_active", False) and count > 0)
        else:
            segment(0)

    def make_job(self, rid, row, placement, cache):
        p, d = placement
        job = copy.deepcopy(row)
        job.update(rid=rid, p=p, d=d, tokens=list(row.get("token_times", ())), kv_units=[],
                   kv_pending=0, ready=False, started=False)
        job["input_tokens"] = row["input_tokens"]
        bound = row.get("max_output_tokens", self.config.reservations.reserve_max_output_tokens)
        desired = min(bound, max(1, len(job["tokens"]))+self.config.binding.prediction_decode_steps)
        ceiling = self.profile.phases["decode"]["points"][-1]["tokens"]-job["input_tokens"]+2
        job["target"] = max(len(job["tokens"]), min(desired, ceiling))
        job["coverage_limited"] = job["target"] < desired
        job["output_bound"] = bound
        cold = self.profile.prefill_steps(job["input_tokens"], len(self.placements[p].gpu_ids))
        trial_row = dict(request_id=rid, input_tokens=job["input_tokens"], output_tokens=bound,
                         prefix_blocks=job.get("prefix_blocks", []), prefill_steps=cold)
        layout = PrefixPlan(cache, trial_row, self.placements[p], self.placements[d], self.profile,
                            self.state["observed_at_seconds"]) if cache else None
        ph = row.get("prefill_hit_tokens", layout.hit_tokens("p") if layout else 0)
        dh = row.get("decode_hit_tokens", layout.hit_tokens("d") if layout else 0)
        job["ph"], job["dh"] = ph, dh
        job["samples"] = self.profile.prefill_steps(job["input_tokens"], len(self.placements[p].gpu_ids),
                                                  min(ph, job["input_tokens"]-1))
        sizes = [b["tokens"] for b in job.get("prefix_blocks", [])] if cache else []
        suffix = job["input_tokens"]-sum(sizes)
        if suffix:
            sizes.append(suffix)
        ranks = self.placements[p].gpu_ids
        destinations = self.placements[d].gpu_ids
        cursor = 0
        if bound > 1:
            for tokens in sizes:
                end = cursor+tokens
                for layer, sample in enumerate(cold):
                    amount = tokens*sample["kv_bytes_per_token"]
                    base, extra = divmod(amount, len(ranks))
                    if end > dh:
                        for rank, (src, dst) in enumerate(zip(ranks, destinations)):
                            size = base+int(rank < extra)
                            if size:
                                job["kv_units"].append(dict(src=src, dst=dst, rank=rank, size=size,
                                    layer=layer, cached=end <= ph, submitted=False))
                cursor = end
        # Completed bytes are removed only from units whose layers were ready
        # in the sampled state; unknown ACK progress is never invented.
        received = row.get("received_transfer_bytes_by_rank", [0]*len(ranks))
        ready_layers = row.get("ready_layers", 0)
        for rank, amount in enumerate(received):
            for unit in sorted(job["kv_units"], key=lambda u: (not u["cached"], u["layer"])):
                if unit["rank"] == rank and (unit["cached"] or unit["layer"] < ready_layers):
                    taken = min(amount, unit["size"])
                    unit["size"] -= taken
                    amount -= taken
        job["kv_units"] = [u for u in job["kv_units"] if u["size"]]
        job["kv_pending"] = len(job["kv_units"])
        self.jobs[rid] = job
        return job, layout

    def submit_ready(self, job, layer=None, all_ready=False):
        for unit in job["kv_units"]:
            if unit["submitted"] or not (all_ready or unit["cached"] or unit["layer"] == layer):
                continue
            unit["submitted"] = True
            def done(job=job):
                job["kv_pending"] -= 1
                self.maybe_decode(job)
            self.send(unit["src"], unit["dst"], unit["size"], "kv", done)

    def prefill(self, job, resume=False):
        self.active[job["p"]] = job["rid"]
        job["started"] = True
        layered = self.config.runtime.kv_transfer_mode.startswith("layer_ready_")
        if layered:
            self.submit_ready(job)
            for index in range(job.get("ready_layers", 0)):
                self.submit_ready(job, layer=index)
        def layer_at(index):
            if index == len(job["samples"]):
                job["prefill_finished"] = True
                job["tokens"].append(self.now)
                self.active[job["p"]] = None
                if not layered:
                    self.submit_ready(job, all_ready=True)
                self.maybe_decode(job)
                self.kick_p(job["p"])
                return
            def done():
                if layered:
                    self.submit_ready(job, layer=index)
                layer_at(index+1)
            self.phase(job, job["samples"][index], "p", done,
                       resume=resume and index == job.get("prefill_layer_index", 0))
        layer_at(job.get("prefill_layer_index", 0) if resume else 0)

    def kick_p(self, instance):
        if self.active[instance] is None and self.queues[instance]:
            self.prefill(self.jobs[self.queues[instance].popleft()])

    def maybe_decode(self, job):
        if not job.get("prefill_finished") or job["kv_pending"] or job["ready"] or len(job["tokens"]) >= job["target"]:
            return
        job["ready"] = True
        self.queues[job["d"]].append(job["rid"])
        self.kick_d(job["d"])

    def decode(self, job, resume=False):
        self.active[job["d"]] = job["rid"]
        sample = self.profile.lookup("decode", job["input_tokens"]+len(job["tokens"])-1,
                                     len(self.placements[job["d"]].gpu_ids))
        def done():
            job["tokens"].append(self.now)
            self.active[job["d"]] = None
            if len(job["tokens"]) < job["target"]:
                self.queues[job["d"]].append(job["rid"])
            self.kick_d(job["d"])
        self.phase(job, sample, "d", done, resume)

    def kick_d(self, instance):
        if self.active[instance] is None and self.queues[instance]:
            self.decode(self.jobs[self.queues[instance].popleft()])


class WholePlanPredictor:
    def __init__(self, config, profile):
        if config.kv_scheduler.policy=='slo_mlu':
            raise ValueError('MLU binding prediction is not implemented')
        if config.expert_parallel.enabled or profile.expert is not None:
            raise ValueError("EP binding prediction is not implemented")
        self.config, self.profile = config, profile
        initial = topology_resources(config.topology)
        self.resources = NetworkResources(config.topology, initial)
        self.resources.initial = initial
        self.placements = {p.instance_id: p for p in config.topology.placements}
        self.namespace = hashlib.sha256(profile.path.read_bytes()).hexdigest()
        self.last_report = {}

    def empty_state(self, now_seconds=0.0):
        capacity = int(self.config.topology.gpu_memory_gib*2**30*(1-self.config.reservations.capacity_safety_fraction))-self.profile.weights_bytes_per_gpu
        return dict(now_seconds=now_seconds, observed_at_seconds=0.0, visible_at_seconds=0.0,
                    requests={}, instances={}, network=None, cache_entries={},
                    ledger=CapacityLedger({"gpu-{}".format(i): capacity for i in range(self.config.topology.num_gpus)}))

    def predict(self, plan, observed_state):
        state = copy.deepcopy(observed_state)
        now, sampled = state["now_seconds"], state["observed_at_seconds"]
        if not all(math.isfinite(t) for t in (now, sampled, state.get("visible_at_seconds", sampled))):
            raise ValueError("prediction times must be finite")
        if not 0 <= sampled <= now or state.get("visible_at_seconds", sampled) > now+1e-12:
            raise ValueError("prediction state is from the future or not visible")
        network = state.get("network")
        if network and (network["time_seconds"] > sampled+1e-12 or network["visible_at_seconds"] > now+1e-12):
            raise ValueError("network prediction state is from the future or not visible")
        ids = [b.request_id for b in plan]
        if len(ids) != len(set(ids)):
            raise ValueError("a request may appear only once in a tentative plan")
        for rid, row in state["requests"].items():
            if row["arrival_seconds"] > now+1e-12 or any(t > sampled+1e-12 for t in row.get("token_times", ())):
                raise ValueError("prediction state contains future request/token information")
        bindings = {}
        for binding in plan:
            rid = binding.request_id
            if rid not in state["requests"]:
                raise ValueError("candidate request has not arrived")
            if state["requests"][rid].get("state") not in ("pending_binding", "binding_decision"):
                raise ValueError("already bound requests cannot be rebound")
            p, d = binding.prefill_instance_id, binding.decode_instance_id
            if p not in self.placements or d not in self.placements or self.placements[p].role != "prefill" or self.placements[d].role != "decode":
                raise ValueError("candidate has invalid P/D roles")
            if len(self.placements[p].gpu_ids) != len(self.placements[d].gpu_ids):
                raise ValueError("predictor does not support KV resharding")
            bindings[rid] = (p, d)
        if batched_execution(self.config):
            return self._predict_batched(plan, state)
        sim = _Rollout(self.config, self.profile, state, self.resources)
        cache = None
        if self.config.prefix_cache.enabled:
            cache = PrefixCache(self.config.prefix_cache, state["ledger"], self.namespace)
            cache.entries = state.get("cache_entries", {})
        objects = []
        for rid, row in state["requests"].items():
            placement = bindings.get(rid) or ((row.get("p"), row.get("d")) if row.get("p") and row.get("d") else None)
            if placement is None or row.get("state") in ("completed", "failed", "not_arrived"):
                continue
            job, layout = sim.make_job(rid, row, placement, cache)
            if not row.get("resources_committed", False):
                if layout:
                    objects.extend(layout.objects)
                else:
                    p, d = (self.placements[x] for x in placement)
                    rank_bytes = [0]*len(p.gpu_ids)
                    for step in job["samples"]:
                        base, extra = divmod(job["input_tokens"]*step["kv_bytes_per_token"], len(p.gpu_ids))
                        rank_bytes = [size+base+int(rank < extra) for rank, size in enumerate(rank_bytes)]
                    objects.extend(PhysicalObjectReservation("p:"+rid, "gpu-{}".format(g), size, (rid,)) for g, size in zip(p.gpu_ids, rank_bytes))
                    if job["output_bound"] > 1:
                        growth = math.ceil(job["output_bound"]*self.profile.kv_bytes_per_token/len(d.gpu_ids))
                        objects.extend(PhysicalObjectReservation("d:"+rid, "gpu-{}".format(g), size+growth, (rid,)) for g, size in zip(d.gpu_ids, rank_bytes))
        feasible = state["ledger"].can_commit(objects)
        late = {rid for rid, job in sim.jobs.items() if rid not in bindings and
                job.get("committed_at_seconds", sampled) > sampled}
        # Start the sampled active steps before their sampled FIFO queues.
        for instance, info in state.get("instances", {}).items():
            rid = info.get("active_request_id")
            if rid in sim.jobs and rid not in bindings and rid not in late:
                job = sim.jobs[rid]
                if self.placements[instance].role == "prefill":
                    sim.prefill(job, resume=True)
                else:
                    job["ready"] = True
                    sim.decode(job, resume=True)
            sim.queues[instance].extend(r for r in info.get("queue", ()) if r in sim.jobs and r not in bindings and r not in late)
        for rid, job in sim.jobs.items():
            if rid in bindings or rid in late:
                continue
            if job.get("prefill_finished"):
                sim.submit_ready(job, all_ready=True)
                if job.get("state") in ("queued_decode", "decode"):
                    job["ready"] = True
                sim.maybe_decode(job)
        for instance, p in self.placements.items():
            (sim.kick_p if p.role == "prefill" else sim.kick_d)(instance)
        for rid in sorted(late, key=lambda r: (sim.jobs[r]["committed_at_seconds"], r)):
            def dispatch(rid=rid):
                job = sim.jobs[rid]
                sim.queues[job["p"]].append(rid)
                sim.kick_p(job["p"])
            sim.later(sim.jobs[rid]["committed_at_seconds"]-sim.now, dispatch)
        release = now+self.config.binding.decision_overhead_us/1e6
        def admit():
            for rid in ids:
                job = sim.jobs[rid]
                sim.queues[job["p"]].append(rid)
            for instance, p in self.placements.items():
                if p.role == "prefill":
                    sim.kick_p(instance)
        sim.later(release-sim.now, admit)
        sim.run()
        result = {}
        for rid, job in sim.jobs.items():
            tokens = job["tokens"]
            complete = len(tokens) >= job["target"] and not job["coverage_limited"]
            ttft = tokens[0]-job["arrival_seconds"] if tokens else None
            handoff = tokens[1]-tokens[0] if len(tokens) >= 2 else None
            later = max((b-a for a, b in zip(tokens[1:], tokens[2:])), default=None)
            f = job.get("ttft_slo_seconds", self.config.evaluation.default_ttft_slo_seconds)
            i = job.get("itl_slo_seconds", self.config.evaluation.default_itl_slo_seconds)
            margins = [(f-ttft)/f] if ttft is not None else []
            margins.extend((i-value)/i for value in (handoff, later) if value is not None)
            allowed = feasible or rid not in bindings
            result[rid] = RequestPrediction(ttft, handoff, later, min(margins) if margins and complete and allowed else None,
                feasible=allowed, complete=complete,
                reason="capacity_exceeded" if not allowed else "profile_coverage" if job["coverage_limited"] else
                       "prediction_budget_exhausted" if not complete else None,
                prefill_hit_tokens=job["ph"], decode_hit_tokens=job["dh"], predicted_token_times=tuple(tokens),
                output_token_limit=job["output_bound"], horizon_output_tokens=job["target"])
        self.last_report = dict(model="profile_fifo_token_rr_fluid_network", observed_at_seconds=sampled,
            visible_at_seconds=state.get("visible_at_seconds", sampled), now_seconds=now,
            decision_overhead_seconds=self.config.binding.decision_overhead_us/1e6,
            decode_horizon_steps=self.config.binding.prediction_decode_steps,
            joint_capacity_feasible=feasible, shadow_events=sim.steps, truncated=sim.truncated,
            modeled_request_ids=sorted(sim.jobs), candidate_request_ids=ids,
            assumptions=["no_future_arrivals_or_exact_EOS", "no_assumed_future_cache_warming",
                         "shared_directed_ECMP_union_fluid_service", "active_collective_replayed_conservatively",
                         "KV_tokens_and_inflight_admission_not_replayed", "capacity_trial_without_eviction"])
        return result

    def _predict_batched(self, plan, state):
        # Same local policy and compute surfaces as online scoring. Keep the
        # diagnostic entry point independent of online candidate enumeration.
        from dataclasses import replace
        from .fast import FastPlanPredictor, WorkBudget, BudgetExhausted, PredictionUnavailable
        from .execution import ExecutionForecast
        predictor = FastPlanPredictor(self.config, self.profile)
        budget = WorkBudget(self.config.binding.prediction_max_events*16)
        feasible, forecast = True, None
        ids = {b.request_id for b in plan}
        try:
            predictor.begin(state, budget)
            for b in plan:
                candidate = predictor.candidate(b.request_id, state['requests'][b.request_id],
                                                b.prefill_instance_id, b.decode_instance_id)
                feasible = feasible and predictor.memory.fits(candidate.objects)
                predictor.accept(candidate)
            forecast = ExecutionForecast(predictor, predictor.jobs)
            result = forecast.run()
            if not feasible:
                result = {rid: replace(p, feasible=False, minimum_normalized_margin=None,
                                       reason='capacity_exceeded') if rid in ids else p for rid,p in result.items()}
        except (BudgetExhausted, PredictionUnavailable, ValueError) as error:
            if isinstance(error, ValueError) and not ('coverage' in str(error) or 'outside' in str(error)):
                raise
            reason = 'profile_coverage' if isinstance(error, ValueError) else 'prediction_budget_exhausted'
            result = {}
            for rid,row in state['requests'].items():
                if rid not in ids and (not row.get('p') or row.get('state') in ('completed','failed','not_arrived')):
                    continue
                bound = row.get('max_output_tokens', self.config.reservations.reserve_max_output_tokens)
                result[rid] = RequestPrediction(None,None,None,None,complete=False,reason=reason,
                    output_token_limit=bound,horizon_output_tokens=min(bound,
                        max(1,len(row.get('token_times',())))+self.config.binding.prediction_decode_steps))
        self.last_report = dict(model='bounded_chunk_batch_resource_forecast',
            observed_at_seconds=state['observed_at_seconds'], visible_at_seconds=state.get('visible_at_seconds',0),
            now_seconds=state['now_seconds'], joint_capacity_feasible=feasible,
            shadow_events=forecast.steps if forecast else 0, score_work=budget.used,
            truncated=budget.exhausted or any(not p.complete for p in result.values()),
            modeled_request_ids=sorted(result), candidate_request_ids=sorted(ids),
            decode_horizon_steps=self.config.binding.prediction_decode_steps,
            assumptions=['no_future_arrivals_or_exact_EOS','no_assumed_future_cache_warming',
                'sampled_active_batch_replayed_conservatively','chunk_layer_network_work_aggregated_at_chunk_end',
                'directed_ECMP_resource_reservations','KV_admission_not_replayed',
                'peers_retained_to_visible_output_bound_with_horizon_context_clamped'])
        return result
