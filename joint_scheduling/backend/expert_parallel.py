"""Experimental, source-batch EP routing over TP teams and live ns-3 flows.

An instance remains one attention/TP replica. Equal TP coordinates across the
instances in an EP group form an EP communicator. Activation channels are
sharded across TP coordinates; a token is routed to top-k distinct experts.
"""

import json
from collections import deque

from .expert_config import ExpertParallelConfig, parse_config, validate_config
from .expert_profile import ExpertProfile, validate_profile


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
        if self.ep_enabled and self.profile.expert.communication_mode == 'token_rank':
            self.ep_stats.update(local_combine_calls=0, local_combine_extra_assignments=0,
                                 modeled_local_combine_seconds=0.)

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
            spans = [(m['chunk_tokens'], m['cached_tokens']) for m in members]
            layers = [layer_id] if layer_id in self.profile.expert.raw['prefill_layer_ids'] else []
        else:
            # Route each new token by its own cursor; regrouping requests into
            # different continuous batches must not change their experts.
            members = self.active_decode_batches.get(instance, (rid,))
            spans = []
            for member in members:
                peer = self.requests[member]
                cursor = peer['input_tokens']+max(0, peer['generated']-1)
                spans.append((1, cursor))
            if self.profile.decode_layered:
                layers = [layer_id] if layer_id in self.profile.expert.raw['prefill_layer_ids'] else []
            else:
                layers = (list(self.profile.expert.raw['prefill_layer_ids'])
                          if self.profile.expert.routing_mode == 'layerwise_periodic' else
                          list(range(self.profile.expert.raw['decode_layers'])))
        def route(layer):
            return self.profile.expert.routing_counts(spans, len(self.ep_groups[instance]), layer)
        def moe(index):
            if index==0 and phase=='decode' and self.profile.decode_layered:
                for member in self.active_decode_batches.get(instance,(rid,)):
                    self.requests[member]['decode_nonexpert_finished']=True
            if index==0 and self.mlu_enabled:
                self.collective_demands.pop('next-dispatch:'+instance,None)
            if index == len(layers):
                finish()
            else:
                assignments, rank_tokens = route(layers[index])
                self.expert_call(rid, instance, phase, layers[index], assignments, lambda: moe(index+1),
                                 rank_tokens=rank_tokens)
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
                    assignments, rank_tokens = route(layers[0])
                    self.ep_phase_demand('next-dispatch:'+instance,'dispatch_pending',
                        self.ep_flows(instance,assignments,rank_tokens=rank_tokens),end+tp)
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

    def ep_flows(self, source, assignments, combine=False, *, rank_tokens=None):
        group = self.ep_groups[source]
        ep = self.profile.expert.raw
        per_rank = ep['num_experts']//len(group)
        width = len(self.instances[source].gpu_ids)
        unit = ep['combine_element_bytes' if combine else 'dispatch_element_bytes']
        deduplicate = self.profile.expert.communication_mode == 'token_rank'
        if deduplicate and (rank_tokens is None or len(rank_tokens) != len(group)):
            raise ValueError('token_rank EP flows require per-target token incidence')
        flows = []
        for i, target in enumerate(group):
            count = rank_tokens[i] if deduplicate else sum(assignments[i*per_rank:(i+1)*per_rank])
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

    def expert_call(self, rid, instance, phase, layer, assignments, complete, *, rank_tokens=None):
        self.ep_counter += 1
        cid = 'ep-{}'.format(self.ep_counter)
        self.ep_stats['started_calls'] += 1
        self.ep_stats['routed_assignments'] += sum(assignments)
        self.ep_calls[cid] = dict(request_id=rid, source_instance_id=instance, phase=phase, layer_id=layer)
        self.emit('ep_call_started', ep_call_id=cid, expert_assignments=assignments, **self.ep_calls[cid])
        dispatch = self.ep_flows(instance, assignments, rank_tokens=rank_tokens)
        combine = self.ep_flows(instance, assignments, True, rank_tokens=rank_tokens)
        deduplicate = self.profile.expert.communication_mode == 'token_rank'
        def payload_count(target, count):
            return rank_tokens[self.ep_groups[instance].index(target)] if deduplicate else count
        def combine_cost(target, count):
            return ((count-payload_count(target, count))*self.profile.expert.local_combine_seconds
                    if deduplicate else 0.)
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
                        collective_bytes=payload_count(target,count)*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes'],
                        collectives_per_step=self.profile.expert.raw['expert_tp_collectives_per_call']))
                    self.ep_calls[cid]['expert_ready_estimates'][target]=self.backend.now_seconds()+self.gpu_queue_seconds(target)+seconds+combine_cost(target,count)+local_tp
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
                            size = payload_count(target,count)*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes']
                            self.allreduce(rid, self.instances[target].gpu_ids, size, lambda: reduce(index+1))
                    if deduplicate:
                        tokens = payload_count(target,count)
                        cost = combine_cost(target,count)
                        self.ep_lane_active[target]['tp_seconds'] -= cost
                        self.ep_stats['local_combine_calls'] += 1
                        self.ep_stats['local_combine_extra_assignments'] += count-tokens
                        self.ep_stats['modeled_local_combine_seconds'] += cost
                        if cost:
                            self.compute_active.add(target)
                            self.ep_lane_active[target].update(stage='local_combine',
                                compute_end_seconds=self.backend.now_seconds()+cost)
                        self.emit('ep_local_combine_started', request_id=rid, ep_call_id=cid,
                                  instance_id=target, token_count=tokens, extra_assignments=count-tokens,
                                  modeled_compute_seconds=cost)
                        def combined():
                            if cost:
                                self.compute_active.remove(target)
                            self.emit('ep_local_combine_completed', request_id=rid, ep_call_id=cid,
                                      instance_id=target, token_count=tokens)
                            reduce(0)
                        if cost:
                            self.backend.call_at(self.backend.now_seconds()+cost, combined)
                        else:
                            combined()
                    else:
                        reduce(0)
                local_tp=self.collective_seconds(self.instances[target].gpu_ids,dict(
                    collective_bytes=payload_count(target,count)*self.profile.expert.raw['hidden_size']*self.profile.expert.raw['combine_element_bytes'],
                    collectives_per_step=self.profile.expert.raw['expert_tp_collectives_per_call'])) if self.scheduler is not None else 0.
                def started(end,target=target,local_tp=local_tp,count=count):
                    if self.mlu_enabled:
                        self.ep_calls[cid]['expert_ready_estimates'][target]=end+combine_cost(target,count)+local_tp
                        self.ep_phase_demand(cid,'combine_pending',combine,max(self.ep_calls[cid]['expert_ready_estimates'].values()))
                self.gpu_task(target, rid, seconds, 'expert', after_compute, on_start=started,
                              ep_call_id=cid, assignments=count,tp_seconds=local_tp+combine_cost(target,count))
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

    def ep_decode_spans(self, rows):
        return [(1, r['input_tokens']+max(0,r['generated']-1)) for r in rows]

    def ep_estimate_seconds(self, instance, tokens, layers=1, *, spans=None, layer_ids=None):
        """Isolated-call resource heuristic; excludes concurrent expert queue waiting."""
        if not self.ep_enabled or not tokens or not layers:
            return 0.
        ep = self.profile.expert
        if ep.routing_mode == 'layerwise_periodic' or ep.communication_mode == 'token_rank':
            group = self.ep_groups[instance]
            spans = [(tokens, 0)] if spans is None else spans
            layer_ids = list(ep.raw['prefill_layer_ids'])[:layers] if layer_ids is None else layer_ids
            total = 0.
            for layer in layer_ids:
                assignments, rank_tokens = ep.routing_counts(spans, len(group), layer)
                work = {}
                for src, dst, size in (self.ep_flows(instance, assignments, rank_tokens=rank_tokens)+
                                      self.ep_flows(instance, assignments, True, rank_tokens=rank_tokens)):
                    for key in self.scheduler.resources.for_gpus(src, dst):
                        work[key] = work.get(key,0)+size
                network = max((size/(self.scheduler.resources.capacities[key]*(1-self.config.kv_scheduler.safety_margin_fraction))
                               for key,size in work.items()),default=0.)
                width = ep.raw['num_experts']//len(group)
                compute = []
                for index,target in enumerate(group):
                    counts = assignments[index*width:(index+1)*width]
                    n = sum(counts)
                    if not n:
                        continue
                    payload = rank_tokens[index] if ep.communication_mode == 'token_rank' else n
                    merge = (n-payload)*ep.local_combine_seconds
                    tp = self.collective_seconds(self.instances[target].gpu_ids,dict(
                        collective_bytes=payload*ep.raw['hidden_size']*ep.raw['combine_element_bytes'],
                        collectives_per_step=ep.raw['expert_tp_collectives_per_call']))
                    compute.append(ep.compute_seconds(self.instances[instance].role,counts)+merge+tp)
                total += network+max(compute,default=0.)+8*self.config.topology.link_latency_us/1e6
            return total
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
                             'no_activation_memory_accounting', 'no_joint_binding_prediction']+
                             ([] if self.profile.decode_layered else ['decode_dense_step_then_aggregated_MoE_layers']))
            if self.profile.decode_layered:
                value['decode_execution']='layerwise_nonexpert_compute_then_corresponding_MoE'
            if self.config.expert_parallel.rank_unit == 'gpu':
                value.update(rank_unit='gpu', phase_ep_sizes={role: next(len(g) for g in self.config.expert_parallel.groups
                    if self.instances[g[0]].role == role) for role in ('prefill', 'decode')})
            ep = self.profile.expert
            if ep.routing_mode == 'layerwise_periodic' or ep.communication_mode == 'token_rank':
                value.update(routing=ep.routing_mode, routing_seed=ep.routing_seed,
                    communication_mode=ep.communication_mode,
                    decode_moe_layer_ids=(list(ep.raw['prefill_layer_ids']) if ep.routing_mode == 'layerwise_periodic'
                                          else list(range(ep.raw['decode_layers']))),
                    local_combine_seconds_per_extra_assignment=ep.local_combine_seconds)
            (self.observer.output.parent/'expert_parallel_summary.json').write_text(json.dumps(value, indent=2)+'\n')
