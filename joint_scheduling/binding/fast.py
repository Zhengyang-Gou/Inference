"""Bounded, deterministic summary predictor and incremental binding selection.

Legacy FIFO uses summaries. Chunk/batch execution uses a work-budgeted local
rollout with aggregate network service and shared batch computation.
All predictions cover a finite visible-output horizon, not unknown EOS.
"""

import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache

from . import Binding, BindingDecision, RequestPrediction
from .predictor import topology_resources
from .execution import ExecutionForecast, batched_execution
from ..kv_scheduler.adaptive import NetworkResources
from ..prefix_cache import PrefixCache, prefix_chain
from ..prefix_cache.layout import PrefixPlan
from ..reservations import PhysicalObjectReservation as Object
from ..reservations.ledger import CapacityLedger


class BudgetExhausted(Exception):
    pass


class PredictionUnavailable(Exception):
    pass


class WorkBudget:
    def __init__(self, limit):
        self.limit, self.used, self.exhausted = limit, 0, False

    def take(self, units=1):
        if self.used + units > self.limit:
            self.exhausted = True
            raise BudgetExhausted()
        self.used += units


@dataclass
class Candidate:
    binding: Binding
    row: dict
    objects: tuple
    layout: object
    steps: tuple
    ps: float
    ds: float
    first: float
    work: dict
    kv: dict
    pc: dict
    dc: dict
    ph: int
    dh: int
    horizon: int
    covered: bool


class ShadowMemory:
    def __init__(self, ledger):
        self.sizes = {key: obj.total for key, obj in ledger.objects.items()}
        self.refs = {key: set(obj.refs) for key, obj in ledger.objects.items()}
        self.free = {domain: row['available_bytes'] for domain, row in ledger.snapshot().items()}

    def additions(self, objects):
        sizes = {}
        for obj in objects:
            key = (obj.memory_domain_id, obj.object_id)
            sizes[key] = max(sizes.get(key, 0), obj.total_size_bytes)
        charges = {}
        for key, size in sizes.items():
            charges[key[0]] = charges.get(key[0], 0) + max(0, size-self.sizes.get(key, 0))
        return sizes, charges

    def fits(self, objects):
        _, charges = self.additions(objects)
        return all(size <= self.free[domain] for domain, size in charges.items())

    def commit(self, objects):
        sizes, charges = self.additions(objects)
        for domain, size in charges.items():
            self.free[domain] -= size
        for key, size in sizes.items():
            self.sizes[key] = max(self.sizes.get(key, 0), size)
        for obj in objects:
            self.refs.setdefault((obj.memory_domain_id, obj.object_id), set()).update(obj.request_ids)
        return sum(charges.values())


class _SnapshotCache(PrefixCache):
    """Memoized pure index queries within one immutable snapshot."""
    def lookup(self, instance_id, blocks, now_seconds):
        identity = (instance_id, tuple((b['id'], b['tokens']) for b in blocks))
        if identity not in self.lookups:
            result = []
            for key in self.keys_for(identity[1]):
                entry = self.entries.get((instance_id, key))
                if entry is None or entry.ready_at_seconds > now_seconds:
                    break
                result.append(entry)
            self.lookups[identity] = tuple(result)
        return self.lookups[identity]


class FastPlanPredictor:
    def __init__(self, config, profile):
        if config.kv_scheduler.policy=='slo_mlu':
            raise ValueError('MLU binding prediction is not implemented')
        if config.expert_parallel.enabled or profile.expert is not None:
            raise ValueError("EP joint binding prediction is not implemented")
        self.config, self.profile = config, profile
        self.batched_execution = batched_execution(config)
        self.placements = {p.instance_id: p for p in config.topology.placements}
        initial = topology_resources(config.topology)
        self.resources = NetworkResources(config.topology, initial)
        self.resources.initial = initial
        self.namespace = hashlib.sha256(profile.path.read_bytes()).hexdigest()
        self.pairs = tuple((p.instance_id, d.instance_id) for p in config.topology.placements if p.role == 'prefill'
                           for d in config.topology.placements if d.role == 'decode' and len(p.gpu_ids) == len(d.gpu_ids))
        # Paths and unit ring work are static, built once outside online rounds.
        self.paths = {(p, d): tuple(self.resources.for_gpus(a, b) for a, b in
                      zip(self.placements[p].gpu_ids, self.placements[d].gpu_ids)) for p, d in self.pairs}
        self.rings = {p: self.resources.ring_work(v.gpu_ids, len(v.gpu_ids)) for p, v in self.placements.items()}
        self.last_report = {}

    def empty_state(self, now_seconds=0):
        capacity = int(self.config.topology.gpu_memory_gib*2**30*(1-self.config.reservations.capacity_safety_fraction))-self.profile.weights_bytes_per_gpu
        return dict(now_seconds=now_seconds, observed_at_seconds=0., visible_at_seconds=0., requests={},
                    instances={}, cache_entries={}, network=None,
                    ledger=CapacityLedger({'gpu-{}'.format(g): capacity for g in range(self.config.topology.num_gpus)}))

    @lru_cache(maxsize=512)
    def prefill(self, tokens, p, hit):
        placement = self.placements[p]
        tp = len(placement.gpu_ids)
        if self.config.runtime.prefill_chunk_tokens:
            # Layout metadata remains per layer. Timing follows query/history
            # chunk curves, not the old whole-input cached_points surface.
            steps = self.profile.prefill_steps(tokens, tp)
            cap = self.config.runtime.prefill_chunk_tokens
            if self.config.runtime.prefill_batch_token_budget:
                cap = min(cap, self.config.runtime.prefill_batch_token_budget)
            def sample(query, cursor):
                return (self.profile.prefill_batch_steps([dict(chunk_tokens=query,cached_tokens=cursor)], tp)
                    if self.config.runtime.prefill_batch_token_budget else self.profile.prefill_chunk_steps(query,cursor,tp))
            cursor = min(hit, tokens-1)
            full, tail = divmod(tokens-cursor, cap)
            timed = []
            if full:
                first, last = sample(cap,cursor), sample(cap,cursor+(full-1)*cap)
                # Cheap pruning proxy only. Exact chunk/history queries and
                # queue/batch competition are evaluated by ExecutionForecast.
                timed.extend(dict(s,compute_seconds=max(s['compute_seconds'],t['compute_seconds'])*full,
                    collective_bytes=max(s['collective_bytes'],t['collective_bytes']),
                    collectives_per_step=max(s['collectives_per_step'],t['collectives_per_step'])*full)
                    for s,t in zip(first,last))
            if tail:
                timed.extend(sample(tail,tokens-tail))
        else:
            steps = self.profile.prefill_steps(tokens, tp, min(hit, tokens-1))
            timed = steps
        work, latency = {}, 0.
        for sample in timed:
            for key, size in self.resources.ring_work(placement.gpu_ids, sample['collective_bytes']).items():
                work[key] = work.get(key, 0)+size*sample['collectives_per_step']
            latency += self.ring_latency(p)*sample['collectives_per_step']
        return steps, sum(s['compute_seconds'] for s in timed)+latency, work

    def ring_latency(self, instance):
        return 2*(len(self.placements[instance].gpu_ids)-1)*self.config.topology.link_latency_us*4/1e6

    @lru_cache(maxsize=512)
    def decode(self, tokens, d, begin, end):
        points = self.profile.phases['decode']['points']
        lo, hi = tokens+max(0, begin-1), tokens+max(0, end-2)
        covered = lo >= points[0]['tokens'] and hi <= points[-1]['tokens']
        if not covered:
            return 0., {}, False
        # Max at endpoints and intervening calibrated knots; no per-token loop.
        samples = [self.profile.lookup('decode', t, len(self.placements[d].gpu_ids))
                   for t in {lo, hi} | {p['tokens'] for p in points if lo < p['tokens'] < hi}]
        work = {}
        for sample in samples:
            for key, size in self.resources.ring_work(self.placements[d].gpu_ids, sample['collective_bytes']).items():
                work[key] = max(work.get(key, 0), size*sample['collectives_per_step'])
        compute = max(s['compute_seconds']+self.ring_latency(d)*s['collectives_per_step'] for s in samples)
        return compute, work, True

    @lru_cache(maxsize=512)
    def prefix_keys(self, blocks):
        return prefix_chain([dict(id=identity, tokens=count) for identity, count in blocks], self.namespace)

    @lru_cache(maxsize=512)
    def segment_sizes(self, counts, tp):
        layers = (tuple(s['kv_bytes_per_token'] for s in self.profile.phases['prefill']['layers'])
                  if self.profile.layered else (self.profile.kv_bytes_per_token,))
        return tuple(tuple(sum(count*size//tp+int(rank < count*size % tp) for size in layers)
                           for rank in range(tp)) for count in counts)

    def layout(self, row, p, d, cold):
        # Same byte-exact PrefixPlan layout/lifecycle, with immutable shard
        # templates shared across pairs. Never share newly produced KV objects.
        plan = object.__new__(PrefixPlan)
        plan.cache, plan.row, plan.profile = self.cache, row, self.profile
        plan.prefill, plan.decode = self.placements[p], self.placements[d]
        plan.blocks = row['prefix_blocks']
        plan.keys = self.prefix_keys(tuple((b['id'], b['tokens']) for b in plan.blocks))
        bound, rid = row['output_tokens'], row['request_id']
        plan.hits = dict(p=self.cache.lookup(p, plan.blocks, self.state['observed_at_seconds'])
                            if self.config.prefix_cache.prefill_reuse_enabled else (),
                         d=self.cache.lookup(d, plan.blocks, self.state['observed_at_seconds'])
                            if self.config.prefix_cache.decode_reuse_enabled and bound > 1 else ())
        plan.counts = [b['tokens'] for b in plan.blocks]
        suffix = row['input_tokens']-sum(plan.counts)
        if suffix:
            plan.counts.append(suffix)
        plan.layer_bytes = [s['kv_bytes_per_token'] for s in cold]
        sizes = self.segment_sizes(tuple(plan.counts), len(plan.prefill.gpu_ids))
        plan.objects, plan.by_role = [], {'p': [], 'd': []}
        for role, placement in (('p', plan.prefill), ('d', plan.decode)):
            if role == 'd' and bound == 1:
                continue
            for index, shard in enumerate(sizes):
                if index < len(plan.hits[role]):
                    objects = tuple(Object(o.object_id, o.memory_domain_id, o.total_size_bytes, (rid,))
                                    for o in plan.hits[role][index].objects)
                else:
                    identity = '{}:{}:segment:{}'.format(role, rid, index)
                    objects = tuple(Object(identity, 'gpu-{}'.format(g), size, (rid,))
                                    for g, size in zip(placement.gpu_ids, shard))
                plan.by_role[role].append(objects)
                plan.objects.extend(objects)
        plan.growth_id = 'd:{}:growth'.format(rid)
        if bound > 1:
            size = math.ceil(bound*self.profile.kv_bytes_per_token/len(plan.decode.gpu_ids))
            plan.objects.extend(Object(plan.growth_id, 'gpu-{}'.format(g), size, (rid,)) for g in plan.decode.gpu_ids)
        return plan

    def begin(self, state, budget):
        now, sampled = state['now_seconds'], state['observed_at_seconds']
        if not 0 <= sampled <= now or state.get('visible_at_seconds', sampled) > now+1e-12:
            raise ValueError('fast prediction requires a visible, nonfuture snapshot')
        network = state.get('network')
        if network and (network['time_seconds'] > sampled+1e-12 or network['visible_at_seconds'] > now+1e-12):
            raise ValueError('network snapshot is not visible')
        self.state, self.now, self.budget = state, now, budget
        self.rates, self.debt = {}, {}
        ports = {(v['node_id'], v['peer_id']): v for v in network['resources'].values()} if network else {}
        budget.take(len(self.resources.capacities)+len(state['ledger'].objects)+len(state['requests']))
        for key, capacity in self.resources.capacities.items():
            raw = self.resources.initial[key]
            port = ports.get((raw['node_id'], raw['peer_id']), {})
            self.rates[key] = max(capacity*.01, capacity*(1-self.config.kv_scheduler.safety_margin_fraction)
                                  -port.get('other_bytes_per_second', 0))
            self.debt[key] = port.get('queue_bytes', 0)
        self.memory = ShadowMemory(state['ledger'])
        self.cache = None
        if self.config.prefix_cache.enabled:
            self.cache = _SnapshotCache(self.config.prefix_cache, state['ledger'], self.namespace)
            self.cache.lookups, self.cache.keys_for = {}, self.prefix_keys
            self.cache.entries = state.get('cache_entries', {})
        self.pq = {p: 0. for p in self.placements}
        self.dload, self.net, self.jobs = {}, {}, []
        self.reserved_bytes = 0
        # Queued P jobs follow the sampled FIFO; controller-owned dispatches
        # absent from telemetry follow their declared dispatch order.
        order = []
        for p, info in state.get('instances', {}).items():
            if self.placements[p].role == 'prefill':
                order.extend([info.get('active_request_id')]+list(info.get('queue', ())))
        order.extend(state['requests'])
        seen = set()
        for rid in order:
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            row = state['requests'].get(rid)
            if not row or not row.get('p') or row.get('state') in ('completed', 'failed', 'not_arrived'):
                continue
            self.validate_row(row)
            candidate = self.candidate(rid, row, row['p'], row['d'], existing=True)
            if not candidate.covered:
                raise PredictionUnavailable('existing_profile_coverage')
            if not row.get('resources_committed'):
                objects = row.get('reservation_objects', candidate.objects)
                if not self.memory.fits(objects):
                    raise PredictionUnavailable('known_capacity_conflict')
                self.memory.commit(objects)
            self.accept(candidate, reserve=False)

    def validate_row(self, row):
        if row['arrival_seconds'] > self.now+1e-12 or any(t > self.state['observed_at_seconds']+1e-12 for t in row.get('token_times', ())):
            raise ValueError('future request or token in fast prediction')

    def delay(self, work, net=None):
        net = self.net if net is None else net
        return max(((net.get(k, 0)+self.debt[k])/self.rates[k] for k in work), default=0.)

    def candidate(self, rid, row, p, d, existing=False):
        self.validate_row(row)
        if self.config.runtime.prefill_chunk_tokens:
            # Endpoint/tail samples keep pruning independent of chunk count.
            # The detailed local forecast is separately charged per chunk.
            self.budget.take(6*len(self.profile.phases['prefill']['layers']))
        bound = row.get('max_output_tokens', self.config.reservations.reserve_max_output_tokens)
        tokens = row.get('token_times', ())
        horizon = min(bound, max(1, len(tokens))+self.config.binding.prediction_decode_steps)
        cold, _, _ = self.prefill(row['input_tokens'], p, 0)
        # Bound object layout and path processing as well as request scoring.
        self.budget.take(1+len(cold)*(len(row.get('prefix_blocks', ()))+1)*len(self.placements[p].gpu_ids)*2)
        trial = dict(request_id=rid, input_tokens=row['input_tokens'], output_tokens=bound,
                     prefix_blocks=row.get('prefix_blocks', ()), prefill_steps=cold)
        layout = self.layout(trial, p, d, cold) if self.cache and not existing else None
        ph = row.get('prefill_hit_tokens', layout.hit_tokens('p') if layout else 0)
        dh = row.get('decode_hit_tokens', layout.hit_tokens('d') if layout else 0)
        steps, compute, pc = self.prefill(row['input_tokens'], p, ph)
        remaining = 0. if tokens else (max(0., 1-row.get('ready_layers', 0)/len(steps)) if existing else 1.)
        pc = {k: v*remaining for k, v in pc.items()}
        ps = compute*remaining + max((v/self.rates[k] for k, v in pc.items()), default=0.)
        first = tokens[0] if tokens else max(self.now+self.config.binding.decision_overhead_us/1e6,
            row.get('committed_at_seconds', self.now))+self.pq[p]+ps
        ds, dc, covered = self.decode(row['input_tokens'], d, len(tokens), horizon) if horizon > 1 and len(tokens) < bound else (0., {}, True)
        ds += max((v/self.rates[k] for k, v in dc.items()), default=0.)
        rank_bytes = self.segment_sizes((row['input_tokens'],), len(self.placements[p].gpu_ids))[0]
        if existing:
            objects = tuple(row.get('reservation_objects', ()))
            counts = [b['tokens'] for b in row.get('prefix_blocks', ())]
            suffix = row['input_tokens']-sum(counts)
            if suffix:
                counts.append(suffix)
            missing = [0]*len(rank_bytes)
            cursor = 0
            for count in counts:
                cursor += count
                if cursor <= dh or bound == 1:
                    continue
                for step in cold:
                    base, extra = divmod(count*step['kv_bytes_per_token'], len(rank_bytes))
                    missing = [v+base+int(i < extra) for i, v in enumerate(missing)]
        elif layout:
            objects = tuple(layout.objects)
            missing = [sum(segment[i].total_size_bytes for segment in layout.by_role['d'][len(layout.hits['d']):])
                       if bound > 1 else 0 for i in range(len(rank_bytes))]
        else:
            objects = tuple(Object('p:'+rid, 'gpu-{}'.format(g), size, (rid,))
                            for g, size in zip(self.placements[p].gpu_ids, rank_bytes))
            if bound > 1:
                growth = math.ceil(bound*self.profile.kv_bytes_per_token/len(rank_bytes))
                objects += tuple(Object('d:'+rid, 'gpu-{}'.format(g), size+growth, (rid,))
                                 for g, size in zip(self.placements[d].gpu_ids, rank_bytes))
            missing = rank_bytes if bound > 1 else [0]*len(rank_bytes)
        kv = {}
        received = row.get('received_transfer_bytes_by_rank', ())
        for i, path in enumerate(self.paths[p, d]):
            size = max(0, missing[i]-(received[i] if i < len(received) else 0))
            if row.get('state') in ('decode', 'queued_decode'):
                size = 0
            for key in path:
                kv[key] = kv.get(key, 0)+size
        kv = {k: v for k, v in kv.items() if v}
        work = dict(kv)
        for part in (pc, dc):
            for k, v in part.items():
                work[k] = work.get(k, 0)+v
        self.budget.take(len(work)+len(objects))
        return Candidate(Binding(rid, p, d), row, objects, layout, steps, ps, ds, first, work, kv, pc, dc,
                         ph, dh, horizon, covered)

    def predict_job(self, job, dload, net):
        self.budget.take(1+len(job.work))
        row, b = job.row, job.binding
        tokens = row.get('token_times', ())
        def peer_delay(work):
            return max(((max(0, net.get(k, 0)-job.work[k])+self.debt[k])/self.rates[k] for k in work), default=0.)
        first = job.first if tokens else job.first+peer_delay(job.pc)
        ttft = first-row['arrival_seconds']
        cycle = dload.get(b.decode_instance_id, 0)+peer_delay(job.dc)
        transfer = self.delay(job.kv, net)
        # Whole-KV wait is charged to handoff only; layer overlap is deliberately
        # conservative here. Past token gaps remain actual observations.
        handoff = (tokens[1]-tokens[0] if len(tokens) > 1 else
                   max(0, self.now-first)+transfer+cycle if job.horizon > 1 else None)
        later = max((v-u for u, v in zip(tokens[1:], tokens[2:])), default=0.)
        if job.horizon >= 3:
            later = max(later, cycle, self.now-tokens[-1]+cycle if len(tokens) >= 2 else 0.)
        else:
            later = None
        f = row.get('ttft_slo_seconds', self.config.evaluation.default_ttft_slo_seconds)
        i = row.get('itl_slo_seconds', self.config.evaluation.default_itl_slo_seconds)
        margin = min([(f-ttft)/f]+[(i-v)/i for v in (handoff, later) if v is not None]) if job.covered else None
        return RequestPrediction(ttft, handoff, later, margin, complete=job.covered,
            reason=None if job.covered else 'profile_coverage', prefill_hit_tokens=job.ph, decode_hit_tokens=job.dh,
            output_token_limit=row.get('max_output_tokens', self.config.reservations.reserve_max_output_tokens),
            horizon_output_tokens=job.horizon)

    def score(self, candidate=None):
        dload, net = self.dload, self.net
        jobs = self.jobs
        if candidate:
            dload, net = dict(dload), dict(net)
            d = candidate.binding.decode_instance_id
            dload[d] = dload.get(d, 0)+candidate.ds
            for k, v in candidate.work.items():
                net[k] = net.get(k, 0)+v
            jobs = jobs+[candidate]
        network_work = sum(net.values())
        if self.batched_execution:
            forecast = ExecutionForecast(self, jobs)
            try:
                predictions = forecast.run()
            except ValueError as error:
                if 'coverage' in str(error) or 'outside' in str(error):
                    raise PredictionUnavailable('batch_profile_coverage') from error
                raise
            if any(not p.complete for p in predictions.values()):
                raise PredictionUnavailable('execution_prediction_incomplete')
            network_work = forecast.network_work
        else:
            predictions = {j.binding.request_id: self.predict_job(j, dload, net) for j in jobs}
        margins = [p.minimum_normalized_margin for p in predictions.values()]
        valid = [m for m in margins if m is not None]
        key = (sum(m >= -1e-12 for m in valid), min(valid, default=-1e30), sum(valid),
               -network_work, -(self.reserved_bytes+sum(self.memory.additions(candidate.objects)[1].values()) if candidate else self.reserved_bytes))
        return key, predictions

    def accept(self, candidate, reserve=True):
        p, d = candidate.binding.prefill_instance_id, candidate.binding.decode_instance_id
        self.jobs.append(candidate)
        self.pq[p] += candidate.ps
        self.dload[d] = self.dload.get(d, 0)+candidate.ds
        for k, v in candidate.work.items():
            self.net[k] = self.net.get(k, 0)+v
        if reserve:
            self.reserved_bytes += self.memory.commit(candidate.objects)


class IncrementalJointPolicy:
    def __init__(self, config, predictor):
        self.config, self.predictor = config, predictor
        self.last_candidates, self.last_report = {}, {}

    def select(self, pending_request_ids, observed_state, predictor=None):
        predictor = predictor or self.predictor
        cfg = self.config.binding
        budget = WorkBudget(cfg.max_score_work)
        selected, scores, considered, pairs = [], 0, 0, 0
        self.last_candidates = {}
        reason = None
        try:
            if observed_state.get('overflow') or len(observed_state['requests']) > cfg.max_state_requests or len(observed_state['ledger'].objects) > cfg.max_state_objects:
                raise BudgetExhausted()
            predictor.begin(observed_state, budget)
            def urgency(rid):
                r = observed_state['requests'][rid]
                wait = max(0, predictor.now-r['arrival_seconds'])
                slo = r.get('ttft_slo_seconds', self.config.evaluation.default_ttft_slo_seconds)
                return (0 if wait >= cfg.starvation_ms/1000 else 1,
                        (slo-wait)/slo-wait/(cfg.starvation_ms/1000), r['arrival_seconds'], rid)
            ids = sorted(tuple(pending_request_ids)[:cfg.max_pending_scan], key=urgency)
            for rid in ids[:cfg.max_requests_per_round]:
                considered += 1
                row = observed_state['requests'][rid]
                baseline, _ = predictor.score()
                # A bounded pair pool, interleaving load, P-cache, D-cache and
                # static network order. Every endpoint contributes before pairs
                # repeat; the pool is capped independently from scoring count.
                pool = self.pair_pool(row, predictor, cfg.max_pair_checks)
                pairs += len(pool)
                trials = []
                for p, d in pool:
                    try:
                        c = predictor.candidate(rid, row, p, d)
                    except ValueError as error:
                        if 'coverage' in str(error) or 'outside' in str(error):
                            continue
                        raise
                    if c.covered and predictor.memory.fits(c.objects):
                        trials.append(c)
                # Diversify pruning: keep the best cache and best load/network
                # offers, then fill by the combined inexpensive proxy.
                network_cost = {id(c): max((v/predictor.rates[k] for k, v in c.kv.items()), default=0.)+predictor.delay(c.kv) for c in trials}
                costs = {id(c): c.first+predictor.dload.get(c.binding.decode_instance_id, 0)+c.ds+network_cost[id(c)] for c in trials}
                def proxy(c):
                    return costs[id(c)]
                offers, offered = [], set()
                rankings = [sorted(trials, key=lambda c: (proxy(c), c.binding.prefill_instance_id, c.binding.decode_instance_id)),
                            sorted(trials, key=lambda c: (-c.ph, -c.dh, proxy(c))),
                            sorted(trials, key=lambda c: (-c.dh, -c.ph, proxy(c))),
                            sorted(trials, key=lambda c: (network_cost[id(c)], proxy(c)))]
                for ranking in rankings:
                    for c in ranking:
                        if id(c) not in offered:
                            offers.append(c)
                            offered.add(id(c))
                            break
                for c in rankings[0]:
                    if id(c) not in offered:
                        offers.append(c)
                        offered.add(id(c))
                best = None
                for c in offers[:cfg.candidate_count]:
                    try:
                        score, predictions = predictor.score(c)
                    except BudgetExhausted:
                        break
                    scores += 1
                    if best is None or score > best[0]:
                        best = (score, c, predictions)
                aged = predictor.now-row['arrival_seconds'] >= cfg.starvation_ms/1000
                # Aging may trade margin for progress, but never sacrifice the
                # primary objective (total predicted passing requests).
                if best and (best[0] >= baseline or aged and best[0][0] >= baseline[0]):
                    predictor.accept(best[1])
                    selected.append(best[1].binding)
                    self.last_candidates[rid] = best[1]
                if budget.exhausted:
                    break
        except BudgetExhausted:
            budget.exhausted = True
            reason = 'state_or_work_budget'
        except PredictionUnavailable as error:
            reason = str(error)
        self.last_report = dict(candidate_scores=scores, pair_checks=pairs, processed_requests=considered,
            score_work=budget.used, work_limit=budget.limit, budget_exhausted=budget.exhausted,
            reason=reason, selected_requests=len(selected),
            prediction_scope='finite_visible_output_horizon', decode_horizon_steps=cfg.prediction_decode_steps)
        self.last_report['execution_model'] = ('bounded_chunk_batch_resource_forecast'
            if predictor.batched_execution else 'fifo_token_rr_summary')
        chosen = {b.request_id for b in selected}
        return BindingDecision(tuple(selected), tuple(r for r in pending_request_ids if r not in chosen))

    def pair_pool(self, row, predictor, limit):
        # Endpoint queries are pure and bounded by the static deployment size.
        predictor.budget.take(len(predictor.placements)*(1+len(row.get('prefix_blocks', ()))))
        hits = {}
        for p in predictor.placements:
            role = predictor.placements[p].role
            enabled = self.config.prefix_cache.prefill_reuse_enabled if role == 'prefill' else self.config.prefix_cache.decode_reuse_enabled
            entries = predictor.cache.lookup(p, row.get('prefix_blocks', ()), predictor.state['observed_at_seconds']) if predictor.cache and enabled else ()
            hits[p] = sum(b['tokens'] for b in row.get('prefix_blocks', ())[:len(entries)])
        pairs = predictor.pairs
        predictor.budget.take(len(pairs)*4)
        rankings = [sorted(pairs, key=lambda x: (predictor.pq[x[0]]+predictor.dload.get(x[1], 0), x)),
                    sorted(pairs, key=lambda x: (-hits[x[0]], -hits[x[1]], x)),
                    sorted(pairs, key=lambda x: (-hits[x[1]], -hits[x[0]], x)),
                    sorted(pairs, key=lambda x: (len(set(k for path in predictor.paths[x] for k in path)), x))]
        result, seen = [], set()
        for index in range(min(len(pairs), limit)):
            for ranking in rankings:
                pair = ranking[index]
                if pair not in seen:
                    seen.add(pair)
                    result.append(pair)
                    if len(result) >= limit:
                        return result
        return result
