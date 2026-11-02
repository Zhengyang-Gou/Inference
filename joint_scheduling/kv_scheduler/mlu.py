"""Nuska-inspired KV promotion, with conservative directed-resource admission.

MLU uses safe schedulable capacity, not the request's own throttled throughput.
Controllable EP/KV load is accounted by arbitration and per-resource windows.
No packet-level route oracle or future ACK/EOS is used.
"""
import math

from . import TransferPermit
from .adaptive import AdaptiveKVPolicy


def validate_settings(config):
    if config.kv_scheduler.mlu_medium_threshold>=config.kv_scheduler.mlu_urgent_threshold:
        raise ValueError('MLU thresholds must increase from medium to urgent')
    if config.kv_scheduler.policy=='slo_mlu':
        if config.runtime.kv_transfer_mode!='layer_ready_slo' or not config.kv_scheduler.slo_priority_enabled:
            raise ValueError('slo_mlu requires layer_ready_slo and slo_priority_enabled')
        if config.binding.joint_selection_enabled or config.binding.prediction_enabled:
            raise ValueError('slo_mlu requires fixed/round-robin binding with prediction disabled')


class MLUKVPolicy(AdaptiveKVPolicy):
    def __init__(self, *args, observation_delay_seconds=0., **kwargs):
        super().__init__(*args, **kwargs)
        self.observation_delay_seconds=observation_delay_seconds
        self.levels, self.inflight_priority, self.inflight_feasible = {}, {}, {}
        self.class_bytes = {key: [0]*4 for key in self.states}
        for state in self.states.values():
            state.update(ordinary_tokens=0., best_effort_tokens=0.)
        self.summary.update(promotions=0, admitted_by_priority={str(i):0 for i in (0,2,3)},
            admitted_best_effort_bytes=0, admitted_best_effort_borrowed_bytes=0,
            overload_decisions=0, peak_tracked_requests=0)

    def refresh(self, now, network, collectives):
        if now < self.last_time-1e-12:
            raise ValueError('KV budget clock moved backwards')
        if network is not None and network['visible_at_seconds'] > now+1e-12:
            raise ValueError('network observation is not visible yet')
        elapsed = max(0.,now-self.last_time)
        for key, state in self.states.items():
            hard = self.config.max_resource_inflight_bytes
            state['tokens'] = min(hard,state['tokens']+state['rate']*elapsed)
            state['ordinary_tokens'] = min(hard,state['ordinary_tokens']+state.get('ordinary_rate',0.)*elapsed)
            state['best_effort_tokens'] = min(max(self.config.max_block_bytes,hard*self.config.mlu_best_effort_fraction),
                state['best_effort_tokens']+state['rate']*self.config.mlu_best_effort_fraction*elapsed)
            port = network['resources'].get(key) if network else None
            capacity = self.resources.capacities[key]*(1-self.config.safety_margin_fraction)
            background = port.get('other_bytes_per_second',0.) if port else 0.
            base = max(0.,capacity-background)
            queue = port.get('queue_bytes',0) if port else 0
            reaction=self.decision_delay_seconds+self.observation_delay_seconds
            guard = self.config.collective_guard_us/1e6+reaction+(state['inflight_bytes']+queue+self.config.max_block_bytes)/max(base,1.)
            demands = [d for d in collectives if key in d['resources'] and self.config.collective_protection_enabled]
            starts = [d['starts_at_seconds'] for d in demands]
            reserved = any(start <= now+guard for start in starts)
            # Reserve the expected EP phase's actual byte demand, not a fixed
            # fraction through its intervening expert computation.
            ep_rate = sum(d.get('resource_bytes',{}).get(key,0)/max(d.get('duration_seconds',guard),1e-9)
                          for d in demands if d['starts_at_seconds']<=now+guard)
            if reserved and not ep_rate:  # legacy TP demand without byte metadata
                ep_rate = base
            ordinary = max(0.,base-ep_rate)/(1+queue/self.config.queue_target_bytes)
            protected = reserved and (self.config.pause_on_collective or queue>=self.config.queue_target_bytes or
                                      bool(port and port.get('pfc_pause_delta',0)>0))
            if protected:
                ordinary = 0.
            horizon = max(self.config.control_interval_us/1e6,state['completion_latency_seconds'] or 0.)+reaction
            pipeline = hard if self.config.inflight_budget_mode=='fixed' else min(hard,max(self.config.max_block_bytes,math.ceil(base*horizon)))
            ordinary_limit = (pipeline if self.config.inflight_budget_mode=='fixed' else
                              min(pipeline,max(self.config.max_block_bytes,math.ceil(ordinary*horizon)))) if ordinary else 0
            drain = None
            if starts:
                drain_time = max(0.,min(starts)-now-reaction)+self.config.collective_max_blocking_us/1e6
                drain = max(0,math.floor(base*drain_time-queue))
                ordinary_limit = min(ordinary_limit,drain)
            if port is None:
                base=ordinary=0.;pipeline=ordinary_limit=0
            state['tokens'] = min(state['tokens'],pipeline)
            state['ordinary_tokens'] = min(state['ordinary_tokens'],ordinary_limit)
            state.update(rate=base,estimated_rate=base,ordinary_rate=ordinary,queue_bytes=queue,
                protected=protected,collective_reserved=reserved,inflight_limit_bytes=pipeline,
                ordinary_inflight_limit_bytes=ordinary_limit,pipeline_limit_bytes=pipeline,
                collective_limit_bytes=drain,next_collective_seconds=min(starts) if starts else None,
                utilization=port.get('utilization',0.) if port else 0.,class_inflight_bytes=list(self.class_bytes[key]))
        self.last_time=now

    def _classify(self, now, budgets):
        requests, required = {}, {}
        guard = self.config.mlu_deadline_guard_us/1e6+self.decision_delay_seconds+self.observation_delay_seconds
        for rid,budget in sorted(budgets.items()):
            seconds=budget.latest_kv_delivery_seconds-now-guard
            mlu={key:amount/(seconds*self.states[key]['estimated_rate']) if seconds>0 and self.states[key]['estimated_rate']>0 else None
                 for key,amount in sorted(budget.remaining_resource_bytes.items()) if amount>0}
            maximum=max((v for v in mlu.values() if v is not None),default=0.)
            finite=all(v is not None for v in mlu.values())
            feasible=(budget.ttft_feasible and seconds>0 and finite and maximum<=1.+1e-12 and
                      budget.production_ready_seconds<=budget.latest_kv_delivery_seconds-guard)
            level=0 if maximum>=self.config.mlu_urgent_threshold else 2 if maximum>=self.config.mlu_medium_threshold else 3
            previous=self.levels.get(rid,3)
            if feasible:
                level=min(previous,level)
                if level<previous:self.summary['promotions']+=1
                self.levels[rid]=level
                for key,amount in budget.remaining_resource_bytes.items():
                    required[key]=required.get(key,0.)+amount/seconds
            else:
                level=3  # retain best-effort work, rather than promote an infinite MLU
            requests[rid]=dict(mlu=maximum if finite and seconds>0 else None,mlu_by_resource=mlu,
                previous_priority=previous,priority=level,feasible=feasible,
                reason='deadline_promotion' if feasible and level<previous else 'feasible' if feasible else 'infeasible_best_effort',
                deadline_seconds=budget.latest_kv_delivery_seconds,remaining_window_seconds=seconds,
                remaining_kv_bytes=budget.remaining_kv_bytes,production_ready_seconds=budget.production_ready_seconds,
                decode_wait_seconds=budget.decode_wait_seconds,decode_step_seconds=budget.decode_step_seconds,
                expert_queue_wait_seconds=budget.expert_queue_wait_seconds)
        self.summary['peak_tracked_requests']=max(self.summary['peak_tracked_requests'],len(self.levels))
        return requests,required

    def admit(self,now_seconds,observed_resources):
        network=observed_resources['network'];budgets=observed_resources['requests']
        self.refresh(now_seconds,network,observed_resources['collectives'])
        requests,required=self._classify(now_seconds,budgets)
        overloaded={k:v for k,v in required.items() if v>self.states[k]['estimated_rate']+1e-9}
        if overloaded:self.summary['overload_decisions']+=1
        competing={k for rid,row in requests.items() if row['feasible'] for k,size in budgets[rid].remaining_resource_bytes.items() if size>0}
        for identity,block in self.inflight.items():
            if self.inflight_feasible[identity]:
                competing.update(self.resources.for_gpus(block.source_gpu_id,block.destination_gpu_id))
        def order(block):
            row=requests[block.request_id];budget=budgets[block.request_id]
            return (0 if row['feasible'] else 1,row['priority'],
                -row['mlu'] if row['feasible'] else budget.arrived_at_seconds,
                budget.latest_kv_delivery_seconds,block.ready_at_seconds,block.layer_id,block.offset_bytes,block.block_id)
        total=sum(b.size_bytes for b in self.inflight.values());permits=[];blocked={}
        for block in sorted(self.pending.values(),key=order):
            row=requests[block.request_id];budget=budgets[block.request_id]
            keys=self.resources.for_gpus(block.source_gpu_id,block.destination_gpu_id)
            urgent=row['feasible'] and row['priority']==0
            borrow=(not row['feasible'] and all(k not in competing and not self.states[k]['collective_reserved'] for k in keys))
            tail=self.config.decode_admission_limit>0 and block.layer_id==budget.final_layer_id
            if self.config.decode_admission_limit and (not budget.decode_instance_id or budget.final_layer_id<0):
                raise ValueError('decode admission requires destination and final-layer metadata')
            reason=None
            if network is None:reason='observation_not_visible'
            elif block.ready_at_seconds>now_seconds+1e-12:reason='not_ready'
            elif tail and block.request_id not in self.decode_owners and sum(x==budget.decode_instance_id for x in self.decode_owners.values())>=self.config.decode_admission_limit:
                reason='decode_admission'
            elif len(self.inflight)>=self.config.max_concurrent_transfers or total+block.size_bytes>self.config.max_inflight_bytes:
                reason='global_inflight_limit'
            elif any(self.states[k]['inflight_bytes']+block.size_bytes>self.states[k]['inflight_limit_bytes'] for k in keys):
                reason='resource_inflight_limit'
            elif not urgent and any(self.states[k]['inflight_bytes']+block.size_bytes>self.states[k]['ordinary_inflight_limit_bytes'] for k in keys):
                reason=('collective_protection' if any(self.states[k]['collective_reserved'] or
                    self.states[k]['collective_limit_bytes'] is not None and self.states[k]['inflight_bytes']+block.size_bytes>
                    self.states[k]['collective_limit_bytes'] for k in keys) else 'ordinary_resource_inflight_limit')
            elif any(self.states[k]['tokens']+1e-7<block.size_bytes for k in keys):reason='bandwidth_tokens'
            elif not urgent and any(self.states[k]['ordinary_tokens']+1e-7<block.size_bytes for k in keys):reason='ordinary_bandwidth_tokens'
            elif not row['feasible'] and not borrow and any(self.states[k]['best_effort_tokens']+1e-7<block.size_bytes for k in keys):reason='best_effort_quota'
            if reason:
                blocked[reason]=blocked.get(reason,0)+1;continue
            if tail and block.request_id not in self.decode_owners:
                self.decode_owners[block.request_id]=budget.decode_instance_id
                self.summary['decode_slots_acquired']+=1
                count=sum(x==budget.decode_instance_id for x in self.decode_owners.values())
                peaks=self.summary['peak_decode_owners'];peaks[budget.decode_instance_id]=max(peaks.get(budget.decode_instance_id,0),count)
            for k in keys:
                state=self.states[k];state['tokens']=max(0.,state['tokens']-block.size_bytes)
                state['ordinary_tokens']=max(0.,state['ordinary_tokens']-block.size_bytes)
                if not row['feasible'] and not borrow:state['best_effort_tokens']=max(0.,state['best_effort_tokens']-block.size_bytes)
                state['inflight_bytes']+=block.size_bytes
                self.class_bytes[k][row['priority']]+=block.size_bytes
                state['class_inflight_bytes']=list(self.class_bytes[k])
            self.inflight[block.block_id]=block;self.started_at[block.block_id]=now_seconds
            self.inflight_priority[block.block_id]=row['priority'];self.pending.pop(block.block_id)
            self.inflight_feasible[block.block_id]=row['feasible']
            total+=block.size_bytes;permits.append(TransferPermit(block.block_id,block.size_bytes,row['priority']))
            self.summary['admitted_by_priority'][str(row['priority'])]+=block.size_bytes
            if not row['feasible']:self.summary['admitted_best_effort_bytes']+=block.size_bytes
            if borrow:self.summary['admitted_best_effort_borrowed_bytes']+=block.size_bytes
        self.last_decision=dict(blocked=blocked,admitted_blocks=[p.block_id for p in permits],
            observed_sequence=network['sequence'] if network else None,observed_at_seconds=network['time_seconds'] if network else None,
            mlu_requests=requests,required_rate_by_resource=required,overloaded_resources=overloaded,
            resources={k:dict(v) for k,v in self.states.items()})
        self.summary['decisions']+=1;self.summary['admitted_blocks']+=len(permits)
        self.summary['admitted_bytes']+=sum(p.allowed_bytes for p in permits)
        self.summary['peak_inflight_bytes']=max(self.summary['peak_inflight_bytes'],total)
        for reason,count in blocked.items():self.summary['blocked_checks'][reason]=self.summary['blocked_checks'].get(reason,0)+count
        return tuple(permits)

    def completed(self,block_id,now_seconds=None):
        block=self.inflight[block_id];priority=self.inflight_priority[block_id]
        super().completed(block_id,now_seconds)
        self.inflight_priority.pop(block_id)
        self.inflight_feasible.pop(block_id)
        for key in self.resources.for_gpus(block.source_gpu_id,block.destination_gpu_id):
            self.class_bytes[key][priority]-=block.size_bytes
            self.states[key]['class_inflight_bytes']=list(self.class_bytes[key])

    def request_completed(self,request_id):
        super().request_completed(request_id)
        self.levels.pop(request_id,None)
