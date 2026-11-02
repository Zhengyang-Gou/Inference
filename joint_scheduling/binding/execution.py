"""Bounded chunk/batch forecast over one delayed, immutable snapshot.

Local queues follow the executor's rules. Network service uses conservative
resource reservations, aggregating a chunk's layer work at its end; it does not
replay ns-3, KV admission, or assume future cache warming / exact EOS.
"""

import heapq
from collections import deque

from . import RequestPrediction
from ..backend.scheduling import prefill_members


def batched_execution(config):
    return bool(config.runtime.prefill_chunk_tokens or config.runtime.decode_batch_size > 1)


class ExecutionForecast:
    def __init__(self, predictor, candidates):
        self.predictor = predictor
        self.config, self.profile = predictor.config, predictor.profile
        self.state, self.budget = predictor.state, predictor.budget
        self.now = self.state['now_seconds']
        self.deadline = self.now+self.config.binding.prediction_time_limit_seconds
        self.sequence, self.events, self.steps = 0, [], 0
        self.network_work = 0
        self.links = {key: self.now+predictor.debt[key]/predictor.rates[key] for key in predictor.rates}
        self.jobs = {}
        self.queues = {key: deque() for key in predictor.placements}
        self.busy = set()
        self.ticks = set()
        self.admission_sequence = 0
        self.truncated = False
        for c in candidates:
            self.budget.take(1+len(c.work))
            row = c.row
            tokens = list(row.get('token_times', ()))
            cursor = min(c.ph, row['input_tokens']-1)
            job = dict(request_id=c.binding.request_id, input_tokens=row['input_tokens'],
                c=c, tokens=tokens, generated=len(tokens), target=c.horizon,
                bound=row.get('max_output_tokens', self.config.reservations.reserve_max_output_tokens),
                prefill_computed_tokens=max(cursor, row.get('prefill_computed_tokens', cursor)),
                prefill_started=row.get('prefill_started', False),
                prefill_admission_sequence=row.get('prefill_admission_sequence', 0),
                finished=bool(tokens) or row.get('prefill_finished', False),
                kv_fraction=0., kv_at=self.now, d_enqueued=False)
            self.admission_sequence = max(self.admission_sequence, job['prefill_admission_sequence'])
            self.jobs[job['request_id']] = job

    def later(self, at, callback):
        self.budget.take()
        self.sequence += 1
        heapq.heappush(self.events, (max(self.now, at), self.sequence, callback))

    def network(self, work, at, latency=0.):
        self.budget.take(1+len(work))
        if not work:
            return at
        start = max(at, max(self.links[k] for k in work))
        end = start+max(size/self.predictor.rates[k] for k, size in work.items())
        for key in work:
            self.links[key] = end
        self.network_work += sum(work.values())
        return end+latency

    def phase(self, instance, samples, resume=None):
        """Shared compute/ring charged once per batch, with visible progress."""
        self.budget.take(len(samples))
        compute, work, messages = 0., {}, {}
        start = resume.get('prefill_layer_index', 0) if resume and self.predictor.placements[instance].role == 'prefill' else 0
        for index, sample in enumerate(samples):
            if index < start:
                continue
            count = sample['collectives_per_step']
            seconds = sample['compute_seconds']
            if resume and index == start:
                segment = resume.get('segment_index', 0)
                seconds = (resume.get('compute_remaining_seconds', 0.) if resume.get('compute_active') else 0.)
                seconds += max(0, count-segment-1)*sample['compute_seconds']/max(1, count)
                count = max(0, count-segment)
            compute += seconds
            if count:
                size = sample['collective_bytes']
                messages[size] = messages.get(size, 0)+count
        # Uniform layers share a message shape. Aggregate their work without
        # expanding an identical ring path for every layer of every candidate.
        for size, count in messages.items():
            ring = self.predictor.resources.ring_work(self.predictor.placements[instance].gpu_ids, size)
            self.budget.take(len(ring))
            for key, amount in ring.items():
                work[key] = work.get(key, 0)+amount*count
        latency = self.predictor.ring_latency(instance)*sum(messages.values())
        return self.network({k: v for k, v in work.items() if v}, self.now+compute, latency)

    def transfer_ready(self, job, cursor):
        c = job['c']
        if not c.kv or job['bound'] == 1:
            job['kv_fraction'] = 1.
            return
        if not self.config.runtime.kv_transfer_mode.startswith('layer_ready_') and not job['finished']:
            return
        missing = job['input_tokens']-c.dh
        fraction = min(1., max(0., (cursor-c.dh)/max(1, missing)))
        delta = fraction-job['kv_fraction']
        if delta <= 0:
            return
        job['kv_fraction'] = fraction
        work = {k: size*delta for k, size in c.kv.items()}
        # Conservative union paths, one propagation allowance per submission.
        latency = 4*self.config.topology.link_latency_us/1e6
        job['kv_at'] = max(job['kv_at'], self.network(work, self.now, latency))
        if fraction >= 1.:
            self.later(job['kv_at'], lambda: self.maybe_decode(job))

    def maybe_decode(self, job):
        if (not job['finished'] or job['kv_fraction'] < 1. or job['kv_at'] > self.now+1e-12
                or job['d_enqueued'] or job['generated'] >= job['bound']):
            return
        job['d_enqueued'] = True
        instance = job['c'].binding.decode_instance_id
        self.queues[instance].append(job['request_id'])
        self.kick_d(instance)

    def kick_p(self, instance):
        if instance in self.busy or instance in self.ticks or not self.queues[instance]:
            return
        if self.config.runtime.prefill_batch_token_budget:
            self.ticks.add(instance)
            def tick():
                self.ticks.remove(instance)
                self.start_p(instance)
            self.later(self.now+1e-9, tick)
        else:
            self.start_p(instance)

    def start_p(self, instance, members=None, resume=None):
        runtime = self.config.runtime
        if members is None:
            queued = [self.jobs[rid] for rid in self.queues[instance]]
            self.budget.take(len(queued))
            if runtime.prefill_batch_token_budget:
                def admit(job):
                    self.admission_sequence += 1
                    job['prefill_admission_sequence'] = self.admission_sequence
                    job['prefill_started'] = True
                    return True
                members, _ = prefill_members(queued, runtime, admit)
            else:
                job = queued[0]
                cursor = job['prefill_computed_tokens']
                query = min(runtime.prefill_chunk_tokens or job['input_tokens'], job['input_tokens']-cursor)
                members = [dict(request_id=job['request_id'], chunk_tokens=query, cached_tokens=cursor)]
            for member in members:
                self.queues[instance].remove(member['request_id'])
        if not members:
            return
        self.busy.add(instance)
        tp = len(self.predictor.placements[instance].gpu_ids)
        if runtime.prefill_batch_token_budget:
            samples = self.profile.prefill_batch_steps(members, tp)
        elif runtime.prefill_chunk_tokens:
            m = members[0]
            samples = self.profile.prefill_chunk_steps(m['chunk_tokens'], m['cached_tokens'], tp)
        else:
            samples = self.jobs[members[0]['request_id']]['c'].steps
        for member in members:
            job = self.jobs[member['request_id']]
            self.transfer_ready(job, max(job['c'].ph, member['cached_tokens']))
        end = self.phase(instance, samples, resume)
        def complete():
            for member in members:
                job = self.jobs[member['request_id']]
                job['prefill_computed_tokens'] = member['cached_tokens']+member['chunk_tokens']
                if job['prefill_computed_tokens'] >= job['input_tokens']:
                    job['finished'] = True
                    job['tokens'].append(self.now)
                    job['generated'] = 1
                else:
                    self.queues[instance].append(job['request_id'])
                self.transfer_ready(job, job['prefill_computed_tokens'])
                self.maybe_decode(job)
            self.busy.remove(instance)
            self.kick_p(instance)
        self.later(end, complete)

    def kick_d(self, instance, members=None, resume=None):
        if members is None:
            if instance in self.busy or not self.queues[instance]:
                return
            members = [self.queues[instance].popleft()
                       for _ in range(min(len(self.queues[instance]), self.config.runtime.decode_batch_size))]
        self.busy.add(instance)
        self.budget.take(len(members))
        # Beyond its forecast horizon a peer remains a competitor until its
        # visible output bound. Freeze its sampled context at the horizon rather
        # than silently removing it or extrapolating unknown future performance.
        contexts = [self.jobs[r]['input_tokens']+min(self.jobs[r]['generated'], self.jobs[r]['target'])-1
                    for r in members]
        sample = self.profile.lookup_decode_batch(contexts, len(self.predictor.placements[instance].gpu_ids))
        end = self.phase(instance, (sample,), resume)
        def complete():
            for rid in members:
                job = self.jobs[rid]
                job['generated'] += 1
                if len(job['tokens']) < job['target']:
                    job['tokens'].append(self.now)
                if job['generated'] < job['bound']:
                    self.queues[instance].append(rid)
            self.busy.remove(instance)
            self.kick_d(instance)
        self.later(end, complete)

    def start(self):
        placed = set()
        for instance, info in self.state.get('instances', {}).items():
            active = [r for r in info.get('active_request_ids', (info.get('active_request_id'),)) if r in self.jobs]
            role = self.predictor.placements[instance].role
            queued = [r for r in info.get('queue', ()) if r in self.jobs]
            self.budget.take(len(active)+len(queued))
            self.queues[instance].extend(queued)
            placed.update(active+queued)
            if role == 'decode':
                for rid in active+queued:
                    self.jobs[rid]['d_enqueued'] = True
            if active:
                row = self.jobs[active[0]]['c'].row
                if role == 'prefill':
                    members = []
                    for rid in active:
                        job = self.jobs[rid]
                        chunk = job['c'].row.get('active_prefill_chunk')
                        if chunk is None:
                            cursor = job['prefill_computed_tokens']
                            chunk = dict(cached_tokens=cursor, chunk_tokens=job['input_tokens']-cursor)
                        members.append(dict(chunk, request_id=rid))
                    self.start_p(instance, members, row)
                else:
                    self.kick_d(instance, active, row)
        for rid, job in self.jobs.items():
            if job['finished']:
                self.transfer_ready(job, job['input_tokens'])
                self.maybe_decode(job)
            elif rid not in placed:
                row = job['c'].row
                new = row.get('state', 'pending_binding') in ('pending_binding', 'binding_decision')
                at = max(self.now, row.get('committed_at_seconds', self.now))
                if new:
                    at = self.now+self.config.binding.decision_overhead_us/1e6
                def dispatch(job=job):
                    instance = job['c'].binding.prefill_instance_id
                    self.queues[instance].append(job['request_id'])
                    self.kick_p(instance)
                self.later(at, dispatch)
        for instance, placement in self.predictor.placements.items():
            (self.kick_p if placement.role == 'prefill' else self.kick_d)(instance)

    def run(self):
        self.start()
        while self.events and not all(len(j['tokens']) >= j['target'] for j in self.jobs.values()):
            self.budget.take(1+len(self.jobs))
            at, _, callback = heapq.heappop(self.events)
            if at > self.deadline or self.steps >= self.config.binding.prediction_max_events:
                self.truncated = True
                break
            self.now = at
            self.steps += 1
            callback()
        result = {}
        for rid, job in self.jobs.items():
            c, tokens = job['c'], job['tokens']
            complete = len(tokens) >= job['target'] and c.covered
            ttft = tokens[0]-c.row['arrival_seconds'] if tokens else None
            handoff = tokens[1]-tokens[0] if len(tokens) >= 2 else None
            later = max((b-a for a, b in zip(tokens[1:], tokens[2:])), default=None)
            f = c.row.get('ttft_slo_seconds', self.config.evaluation.default_ttft_slo_seconds)
            i = c.row.get('itl_slo_seconds', self.config.evaluation.default_itl_slo_seconds)
            margins = [(f-ttft)/f] if ttft is not None else []
            margins.extend((i-value)/i for value in (handoff, later) if value is not None)
            result[rid] = RequestPrediction(ttft, handoff, later, min(margins) if complete and margins else None,
                complete=complete, reason=None if complete else 'execution_prediction_incomplete',
                prefill_hit_tokens=c.ph, decode_hit_tokens=c.dh, predicted_token_times=tuple(tokens),
                output_token_limit=job['bound'], horizon_output_tokens=job['target'])
        return result
