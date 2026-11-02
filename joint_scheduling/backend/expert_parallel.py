"""Experimental, source-batch EP routing over TP teams and live ns-3 flows.

An instance remains one attention/TP replica. Equal TP coordinates across the
instances in an EP group form an EP communicator. Activation channels are
sharded across TP coordinates; a token is routed to top-k distinct experts.
"""

import json
from collections import deque
from dataclasses import dataclass, field

from ..validation import ConfigError, integer, read_settings, require_mapping


@dataclass(frozen=True)
class ExpertParallelConfig:
    enabled: bool = False
    groups: tuple = field(default=(), metadata={"internal": True})
    rank_unit: str = field(default='tp_team', metadata={'choices': ('tp_team', 'gpu')})


def parse_config(raw):
    raw = require_mapping(raw, "expert_parallel")
    groups = raw.pop("groups", [])
    settings = read_settings(ExpertParallelConfig, raw, "expert_parallel")
    if not isinstance(groups, list) or any(not isinstance(g, list) or len(g) < 2 or
            any(not isinstance(i, str) or not i.strip() for i in g) for g in groups):
        raise ConfigError("expert_parallel.groups must contain lists of at least two instance IDs")
    if bool(groups) != settings.enabled:
        raise ConfigError("expert_parallel.enabled requires groups; disabled EP must have no groups")
    return ExpertParallelConfig(settings.enabled, tuple(tuple(g) for g in groups), settings.rank_unit)


def validate_config(config):
    ep = config.expert_parallel
    if not ep.enabled:
        if ep.groups:
            raise ConfigError("Disabled EP must have no groups")
        return
    placements = {p.instance_id: p for p in config.topology.placements}
    seen = set()
    for group in ep.groups:
        if len(group) < 2 or any(i not in placements or i in seen for i in group) or len(set(group)) != len(group):
            raise ConfigError("EP groups must contain distinct, known instances exactly once")
        peers = [placements[i] for i in group]
        if len({p.role for p in peers}) != 1 or len({len(p.gpu_ids) for p in peers}) != 1:
            raise ConfigError("Each EP group requires one role and equal TP widths")
        seen.update(group)
    if seen != set(placements):
        raise ConfigError("EP groups must cover every P/D instance exactly once")
    for role in ('prefill', 'decode'):
        if len({len(g) for g in ep.groups if placements[g[0]].role == role}) != 1:
            raise ConfigError('EP groups within each phase must have equal size')
    if ep.rank_unit == 'gpu' and any(len(p.gpu_ids) != 1 for p in placements.values()):
        raise ConfigError('GPU-rank EP requires TP1 for every attention instance')
    for p in placements.values():
        if len({g // config.topology.gpus_per_node for g in p.gpu_ids}) != 1:
            raise ConfigError("EP mode requires every TP instance to stay within one node")
    if config.binding.joint_selection_enabled or config.binding.prediction_enabled:
        raise ConfigError("Experimental EP does not yet support joint binding or binding prediction; disable both explicitly")


class ExpertProfile:
    def __init__(self, raw, layers):
        from .runtime import number
        raw = require_mapping(raw, "profile.expert_parallel")
        keys = {"activation_layout", "dense_compute_excludes_experts", "num_experts", "top_k", "hidden_size", "dispatch_element_bytes",
                "combine_element_bytes", "routing_pattern", "prefill_layer_ids", "decode_layers",
                "expert_compute_seconds_per_assignment", "expert_tp_collectives_per_call"}
        optional={"expert_compute_seconds_per_active_expert"}
        if not keys <= set(raw) or set(raw)-keys-optional or raw["activation_layout"] != "tp_sharded" or raw['dense_compute_excludes_experts'] is not True:
            raise ValueError("EP profile requires explicit TP-sharded activations and all EP fields")
        self.raw = raw
        for key in ("num_experts", "top_k", "hidden_size", "dispatch_element_bytes", "combine_element_bytes", "decode_layers"):
            number(raw[key], "EP " + key, True, True)
        number(raw["expert_tp_collectives_per_call"], "EP expert TP collectives", integral=True)
        if raw["top_k"] > raw["num_experts"]:
            raise ValueError("EP top_k must not exceed num_experts")
        pattern = raw["routing_pattern"]
        if not isinstance(pattern, list) or not pattern:
            raise ValueError("EP routing_pattern must be a nonempty periodic token-to-expert table")
        for experts in pattern:
            if not isinstance(experts, list) or len(experts) != raw["top_k"]:
                raise ValueError("Each EP routing row must contain top_k distinct expert IDs")
            for expert in experts:
                integer(expert, "expert ID")
                if expert >= raw["num_experts"]:
                    raise ValueError("EP expert ID is outside num_experts")
            if len(set(experts)) != len(experts):
                raise ValueError("EP top-k experts must be distinct for each token")
        ids = raw["prefill_layer_ids"]
        if not isinstance(ids, list) or not ids:
            raise ValueError("EP prefill_layer_ids must be nonempty")
        for i in ids:
            integer(i, "EP prefill layer")
            if i >= layers:
                raise ValueError("EP prefill layer is outside the compute profile")
        if len(set(ids)) != len(ids) or raw["decode_layers"] != len(ids):
            raise ValueError("EP layer IDs must be unique and agree with decode_layers")
        timing = require_mapping(raw["expert_compute_seconds_per_assignment"], "EP expert timing")
        if set(timing) != {"prefill", "decode"}:
            raise ValueError("EP expert timing requires prefill and decode")
        for phase, seconds in timing.items():
            number(seconds, "EP expert timing " + phase, True)
        resident=raw.get('expert_compute_seconds_per_active_expert',{'prefill':0.,'decode':0.})
        if not isinstance(resident,dict) or set(resident)!={'prefill','decode'}:
            raise ValueError('EP active expert timing requires prefill and decode')
        for phase,seconds in resident.items():
            number(seconds,'EP active expert timing '+phase)

    def compute_seconds(self,phase,counts):
        """Assignment work plus the weights/launch cost of experts actually touched."""
        return (sum(counts)*self.raw['expert_compute_seconds_per_assignment'][phase]+
                sum(n>0 for n in counts)*self.raw.get('expert_compute_seconds_per_active_expert',{}).get(phase,0.))

    def counts(self, tokens, offset=0):
        """O(period * top-k), even for long prompts; exact integer conservation."""
        pattern = self.raw["routing_pattern"]
        rounds, tail = divmod(tokens, len(pattern))
        counts = [0] * self.raw["num_experts"]
        for i, experts in enumerate(pattern):
            n = rounds + int((i-offset) % len(pattern) < tail)
            for expert in experts:
                counts[expert] += n
        return counts

    def destinations(self, tokens, group, offset=0):
        counts = self.counts(tokens, offset)
        per_rank = self.raw["num_experts"] // len(group)
        return {instance: sum(counts[i*per_rank:(i+1)*per_rank]) for i, instance in enumerate(group)}


def validate_profile(config, profile):
    validate_config(config)  # Also validate programmatically replaced configurations.
    if config.expert_parallel.enabled != (profile.expert is not None):
        raise ValueError("EP config and compute profile must both enable EP; dense timing must exclude expert compute")
    if profile.expert is None:
        return
    for group in config.expert_parallel.groups:
        if profile.expert.raw["num_experts"] % len(group):
            raise ValueError("num_experts must be divisible by each EP group size")
        role = next(p.role for p in config.topology.placements if p.instance_id == group[0])
        declared=profile.provenance.get('expert_parallel_sizes', {}).get(role,
            profile.provenance.get('expert_parallel_size'))
        if declared is not None and declared != len(group):
            raise ValueError('EP group size does not match the compute profile expert_parallel_size')
    for p in config.topology.placements:
        if len(p.gpu_ids) != profile.phases[p.role]["tp_size"]:
            raise ValueError("EP TP width does not match compute profile")
        if profile.expert.raw["hidden_size"] < len(p.gpu_ids):
            raise ValueError("EP hidden_size must cover every TP channel shard")


class ExpertParallelMixin:
    def init_expert_parallel(self):
        self.ep_enabled = self.config.expert_parallel.enabled
        self.ep_groups = {i: group for group in self.config.expert_parallel.groups for i in group}
        self.ep_lanes = {i: deque() for i in self.instances}
        self.ep_lane_active = {}
        self.ep_calls = {}
        self.ep_counter = 0
        self.ep_stats = dict(started_calls=0, completed_calls=0, dispatch_bytes=0, combine_bytes=0,
            inter_node_dispatch_bytes=0, inter_node_combine_bytes=0, routed_assignments=0,
            expert_assignments={i: 0 for i in self.instances}, modeled_expert_compute_seconds=0.,
            compute_queue_wait_seconds=0., peak_compute_queue=0)

    def gpu_task(self, instance, rid, seconds, kind, after_compute, on_start=None, **details):
        """One FIFO compute/TP lane per TP team; release after its local TP barrier."""
        self.ep_lanes[instance].append((rid, seconds, kind, after_compute, on_start, details, self.backend.now_seconds()))
        self.ep_stats['peak_compute_queue'] = max(self.ep_stats['peak_compute_queue'], len(self.ep_lanes[instance]))
        self.kick_gpu_task(instance)

    def kick_gpu_task(self, instance):
        if instance in self.ep_lane_active or not self.ep_lanes[instance]:
            return
        rid, seconds, kind, after_compute, on_start, details, queued = self.ep_lanes[instance].popleft()
        self.ep_lane_active[instance] = dict(request_id=rid, task_kind=kind, **details)
        self.ep_lane_active[instance].update(compute_end_seconds=self.backend.now_seconds()+seconds, stage='compute')
        self.ep_stats['compute_queue_wait_seconds'] += self.backend.now_seconds()-queued
        self.compute_active.add(instance)
        if on_start is not None:
            on_start(self.backend.now_seconds()+seconds)
        self.emit('compute_segment_started', request_id=rid, instance_id=instance,
                  task_kind=kind, modeled_compute_seconds=seconds, **details)
        def done():
            self.compute_active.remove(instance)
            self.ep_lane_active[instance]['stage']='tp'
            self.emit('compute_segment_completed', request_id=rid, instance_id=instance, task_kind=kind, **details)
            def release():
                self.ep_lane_active.pop(instance)
                self.kick_gpu_task(instance)
            after_compute(release)
        self.backend.call_at(self.backend.now_seconds()+seconds, done)

    def execute_ep_step(self, row, phase, tokens, complete, sample, layer_id, details):
        instance = row['p'] if phase == 'prefill' else row['d']
        rid = row['request_id']
        count = sample['collectives_per_step']
        self.emit('compute_step_started', request_id=rid, phase=phase, instance_id=instance,
                  tokens=tokens, modeled_compute_seconds=sample['compute_seconds'], **details)
        def finish():
            self.emit('compute_step_completed', request_id=rid, phase=phase, instance_id=instance, **details)
            complete()
        if phase == 'prefill':
            query = details.get('total_query_tokens', tokens)
            cached = min(row['prefix_plan'].hit_tokens('p'), row['input_tokens']-1) if row.get('prefix_plan') else 0
            if 'scheduled_tokens' not in details and not self.config.runtime.prefill_chunk_tokens:
                query -= cached
            # Cursor-stable routing: cached history is never dispatched again.
            members = (self.active_prefill_batches[instance]['members'] if instance in self.active_prefill_batches else
                       [dict(chunk_tokens=query, cached_tokens=details.get('cached_tokens', max(cached, row.get('prefill_computed_tokens', 0))))])
            assignments = [0]*self.profile.expert.raw['num_experts']
            for member in members:
                for i, n in enumerate(self.profile.expert.counts(member['chunk_tokens'], member['cached_tokens'])):
                    assignments[i] += n
            layers = [layer_id] if layer_id in self.profile.expert.raw['prefill_layer_ids'] else []
        else:
            # Route each new token by its own cursor; regrouping requests into
            # different continuous batches must not change their experts.
            members = self.active_decode_batches.get(instance, (rid,))
            assignments = [0]*self.profile.expert.raw['num_experts']
            for member in members:
                peer = self.requests[member]
                cursor = peer['input_tokens']+max(0, peer['generated']-1)
                for i, n in enumerate(self.profile.expert.counts(1, cursor)):
                    assignments[i] += n
            layers = list(range(self.profile.expert.raw['decode_layers']))
        def moe(index):
            if index==0 and self.mlu_enabled:
                self.collective_demands.pop('next-dispatch:'+instance,None)
            if index == len(layers):
                finish()
            else:
                self.expert_call(rid, instance, phase, layers[index], assignments, lambda: moe(index+1))
        def segment(index):
            row['segment_index'] = index
            def started(end):
                peers = ([m['request_id'] for m in self.active_prefill_batches[instance]['members']]
                         if phase == 'prefill' and instance in self.active_prefill_batches else
                         self.active_decode_batches.get(instance, (rid,)) if phase == 'decode' else (rid,))
                for peer in peers:
                    self.requests[peer]['compute_segment_end'] = end
                    self.requests[peer]['segment_index'] = index
                if self.mlu_enabled and layers and index==max(1,count)-1:
                    tp=self.collective_seconds(self.instances[instance].gpu_ids,dict(sample,collectives_per_step=int(count>0)))
                    self.ep_phase_demand('next-dispatch:'+instance,'dispatch_pending',
                        self.ep_flows(instance,assignments),end+tp)
            def after_compute(release):
                def next_segment():
                    release()
                    if index+1 < max(1, count):
                        segment(index+1)
                    else:
                        moe(0)
                if count:
                    self.allreduce(rid, self.instances[instance].gpu_ids, sample['collective_bytes'], next_segment)
                else:
                    next_segment()
            tp_seconds=self.collective_seconds(self.instances[instance].gpu_ids,
                dict(sample,collectives_per_step=1)) if self.scheduler is not None and count else 0.
            self.gpu_task(instance, rid, sample['compute_seconds']/max(1, count), 'dense', after_compute,
                          on_start=started,tp_seconds=tp_seconds)
        segment(0)

    def ep_flows(self, source, assignments, combine=False):
        group = self.ep_groups[source]
        ep = self.profile.expert.raw
        per_rank = ep['num_experts']//len(group)
        width = len(self.instances[source].gpu_ids)
        unit = ep['combine_element_bytes' if combine else 'dispatch_element_bytes']
        flows = []
        for i, target in enumerate(group):
            count = sum(assignments[i*per_rank:(i+1)*per_rank])
            for rank, src in enumerate(self.instances[source].gpu_ids):
                dst = self.instances[target].gpu_ids[rank]
                # Shard channels before multiplying by assignment count.
                channels = ep['hidden_size']//width + int(rank < ep['hidden_size'] % width)
                if count and src != dst:
                    flows.append((dst if combine else src, src if combine else dst, count*channels*unit))
        return flows

    def ep_exchange(self, rid, call_id, stage, flows, complete):
        self.collective_counter += 1
        cid = 'collective-{}'.format(self.collective_counter)
        started = self.backend.now_seconds()
        total = sum(size for _, _, size in flows)
        remote = sum(size for src, dst, size in flows if src//self.config.topology.gpus_per_node != dst//self.config.topology.gpus_per_node)
        if self.mlu_enabled:
            self.ep_calls[call_id]['stage']=stage
            self.ep_phase_demand(call_id,stage,flows,self.backend.now_seconds())
        self.ep_stats[stage+'_bytes'] += total
        self.ep_stats['inter_node_'+stage+'_bytes'] += remote
        self.emit('collective_started', request_id=rid, collective_id=cid, collective_type='ep_'+stage,
                  ep_call_id=call_id, size_bytes=total, flow_count=len(flows))
        pending = [len(flows)]
        def done():
            pending[0] -= 1
            if pending[0] == 0:
                if self.mlu_enabled:
                    self.collective_demands.pop(call_id,None)
                self.emit('collective_completed', request_id=rid, collective_id=cid, collective_type='ep_'+stage,
                          ep_call_id=call_id, duration_seconds=self.backend.now_seconds()-started)
                complete()
        if not flows:
            pending[0] = 1
            done()
        for src, dst, size in flows:
            self.transfer(rid, 'collective', src, dst, size, done, collective_type='ep_'+stage, ep_call_id=call_id)

    def expert_call(self, rid, instance, phase, layer, assignments, complete):
        self.ep_counter += 1
        cid = 'ep-{}'.format(self.ep_counter)
        self.ep_stats['started_calls'] += 1
        self.ep_stats['routed_assignments'] += sum(assignments)
        self.ep_calls[cid] = dict(request_id=rid, source_instance_id=instance, phase=phase, layer_id=layer)
        self.emit('ep_call_started', ep_call_id=cid, expert_assignments=assignments, **self.ep_calls[cid])
        dispatch, combine = self.ep_flows(instance, assignments), self.ep_flows(instance, assignments, True)
        if self.scheduler is not None and not self.mlu_enabled:
            resources = {key for src, dst, _ in dispatch+combine for key in self.scheduler.resources.for_gpus(src, dst)}
            self.collective_demands[cid] = dict(resources=tuple(sorted(resources)), starts_at_seconds=self.backend.now_seconds())
            self.request_dispatch()
        def finished():
            self.collective_demands.pop(cid, None)
            self.ep_stats['completed_calls'] += 1
            self.emit('ep_call_completed', ep_call_id=cid, **self.ep_calls[cid])
            self.ep_calls.pop(cid)
            complete()
            self.request_dispatch()
        def dispatched():
            group = self.ep_groups[instance]
            per_rank = self.profile.expert.raw['num_experts']//len(group)
            destinations = [(target,assignments[i*per_rank:(i+1)*per_rank]) for i,target in enumerate(group)]
            destinations = [(target,counts) for target,counts in destinations if sum(counts)]
            if self.mlu_enabled:
                self.ep_calls[cid]['stage']='expert'
                self.ep_calls[cid]['expert_ready_estimates']={}
                for target,counts in destinations:
                    count=sum(counts)
                    seconds=self.profile.expert.compute_seconds(phase,counts)
                    local_tp=self.collective_seconds(self.instances[target].gpu_ids,dict(
                        collective_bytes=count*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes'],
                        collectives_per_step=self.profile.expert.raw['expert_tp_collectives_per_call']))
                    self.ep_calls[cid]['expert_ready_estimates'][target]=self.backend.now_seconds()+self.gpu_queue_seconds(target)+seconds+local_tp
                self.ep_phase_demand(cid,'combine_pending',combine,max(self.ep_calls[cid]['expert_ready_estimates'].values()))
            pending = [len(destinations)]
            def expert_done():
                pending[0] -= 1
                if not pending[0]:
                    self.ep_exchange(rid, cid, 'combine', combine, finished)
            for target,counts in destinations:
                count=sum(counts)
                seconds=self.profile.expert.compute_seconds(phase,counts)
                self.ep_stats['expert_assignments'][target] += count
                self.ep_stats['modeled_expert_compute_seconds'] += seconds
                def after_compute(release, target=target, count=count):
                    def reduced():
                        release()
                        if self.mlu_enabled:
                            self.ep_calls[cid]['expert_ready_estimates'][target]=self.backend.now_seconds()
                            self.ep_phase_demand(cid,'combine_pending',combine,max(self.ep_calls[cid]['expert_ready_estimates'].values()))
                        self.emit('ep_expert_completed', request_id=rid, ep_call_id=cid, instance_id=target, assignments=count)
                        expert_done()
                    def reduce(index):
                        if index == self.profile.expert.raw['expert_tp_collectives_per_call']:
                            reduced()
                        else:
                            size = count*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes']
                            self.allreduce(rid, self.instances[target].gpu_ids, size, lambda: reduce(index+1))
                    reduce(0)
                local_tp=self.collective_seconds(self.instances[target].gpu_ids,dict(
                    collective_bytes=count*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes'],
                    collectives_per_step=self.profile.expert.raw['expert_tp_collectives_per_call'])) if self.scheduler is not None else 0.
                def started(end,target=target,local_tp=local_tp):
                    if self.mlu_enabled:
                        self.ep_calls[cid]['expert_ready_estimates'][target]=end+local_tp
                        self.ep_phase_demand(cid,'combine_pending',combine,max(self.ep_calls[cid]['expert_ready_estimates'].values()))
                self.gpu_task(target, rid, seconds, 'expert', after_compute, on_start=started,
                              ep_call_id=cid, assignments=count,tp_seconds=local_tp)
        self.ep_exchange(rid, cid, 'dispatch', dispatch, dispatched)

    def gpu_queue_seconds(self,instance):
        now=self.backend.now_seconds();active=self.ep_lane_active.get(instance)
        seconds=max(0.,active['compute_end_seconds']-now)+active.get('tp_seconds',0.) if active else 0.
        return seconds+sum(task[1]+task[5].get('tp_seconds',0.) for task in self.ep_lanes[instance])

    def ep_phase_demand(self,cid,stage,flows,starts):
        if self.scheduler is None:return
        work={}
        for src,dst,size in flows:
            for key in self.scheduler.resources.for_gpus(src,dst):work[key]=work.get(key,0)+size
        duration=max((size/(self.scheduler.resources.capacities[k]*(1-self.config.kv_scheduler.safety_margin_fraction))
                      for k,size in work.items()),default=0.)+4*self.config.topology.link_latency_us/1e6
        self.collective_demands[cid]=dict(resources=tuple(sorted(work)),resource_bytes=work,
            starts_at_seconds=starts,duration_seconds=duration,stage=stage,ep_call_id=cid)
        self.request_dispatch()

    def ep_estimate_seconds(self, instance, tokens, layers=1):
        """Isolated-call resource heuristic; excludes concurrent expert queue waiting."""
        if not self.ep_enabled or not tokens or not layers:
            return 0.
        ep = self.profile.expert
        assignments = ep.counts(tokens)
        work = {}
        for src, dst, size in self.ep_flows(instance, assignments)+self.ep_flows(instance, assignments, True):
            for key in self.scheduler.resources.for_gpus(src, dst):
                work[key] = work.get(key, 0)+size
        seconds = max((size/(self.scheduler.resources.capacities[k]*(1-self.config.kv_scheduler.safety_margin_fraction))
                       for k, size in work.items()), default=0.)
        group = self.ep_groups[instance]
        destinations = ep.destinations(tokens, group)
        per_rank=ep.raw['num_experts']//len(group)
        expert=max(ep.compute_seconds(self.instances[instance].role,assignments[i*per_rank:(i+1)*per_rank]) for i in range(len(group)))
        local_tp = max((self.collective_seconds(self.instances[i].gpu_ids, dict(
            collective_bytes=n*ep.raw['hidden_size']*ep.raw['combine_element_bytes'],
            collectives_per_step=ep.raw['expert_tp_collectives_per_call'])) for i, n in destinations.items() if n), default=0.)
        return layers*(seconds+expert+local_tp+8*self.config.topology.link_latency_us/1e6)

    def write_ep_summary(self):
        if self.ep_enabled:
            value = dict(self.ep_stats, enabled=True, groups=self.config.expert_parallel.groups,
                active_calls_at_end=self.ep_calls, active_gpu_tasks_at_end=self.ep_lane_active,
                queued_gpu_tasks_at_end={i: len(q) for i, q in self.ep_lanes.items() if q},
                planned_phase_demands_at_end=dict(self.collective_demands) if self.mlu_enabled else {},
                execution='asynchronous_source_batch_dispatch_expert_fifo_combine',
                activation_layout='tp_sharded', routing='explicit_periodic_top_k',
                protection=(('phase_aware_MLU' if self.mlu_enabled else 'active_EP_call_union_resources_reserved_through_combine')
                            if self.scheduler is not None and self.config.kv_scheduler.collective_protection_enabled else 'disabled'),
                limitations=['no_global_EP_batch_barrier', 'affine_assignment_and_active_expert_compute_profile',
                             'no_activation_memory_accounting', 'no_joint_binding_prediction',
                             'decode_dense_step_then_aggregated_MoE_layers'])
            if self.config.expert_parallel.rank_unit == 'gpu':
                value.update(rank_unit='gpu', phase_ep_sizes={role: next(len(g) for g in self.config.expert_parallel.groups
                    if self.instances[g[0]].role == role) for role in ('prefill', 'decode')})
            (self.observer.output.parent/'expert_parallel_summary.json').write_text(json.dumps(value, indent=2)+'\n')
