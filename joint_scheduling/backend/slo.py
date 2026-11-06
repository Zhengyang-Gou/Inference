"""Causal handoff budgets for MLU; bounded local queues, never exact EOS.

This is a local scheduling heuristic, not a complete MoE engine prediction.
Controller-owned submitted tasks are visible immediately; network ACK/queue
observations enter only after their configured observation delay.
"""
from dataclasses import replace


class MLUBudgetMixin:
    def expert_backlog_seconds(self, instance):
        if not self.ep_enabled:
            return 0.
        group=self.ep_groups[instance]
        identity=('ep_backlog',group)
        if identity in self.slo_budget_cache:
            return self.slo_budget_cache[identity]
        now=self.backend.now_seconds();largest=0.
        for target in group:
            active=self.ep_lane_active.get(target)
            seconds=0.
            if active:
                seconds=max(0.,active.get('compute_end_seconds',now)-now)
                seconds+=active.get('tp_seconds',0.)
            seconds+=sum(task[1]+task[5].get('tp_seconds',0.) for task in self.ep_lanes[target])
            largest=max(largest,seconds)
        self.slo_budget_cache[identity]=largest
        return largest

    def mlu_decode_cost(self,instance,rows):
        placement=self.instances[instance]
        sample=self.profile.lookup_decode_batch([r['input_tokens']+max(0,r['generated']-1) for r in rows],len(placement.gpu_ids))
        seconds=sample['compute_seconds']+self.collective_seconds(placement.gpu_ids,sample)
        if self.ep_enabled:
            seconds+=self.ep_estimate_seconds(instance,len(rows),self.profile.expert.raw['decode_layers'],
                                             spans=self.ep_decode_spans(rows))
        return seconds

    def mlu_budget(self,row,base):
        now=self.backend.now_seconds();d=row['d'];size=self.config.runtime.decode_batch_size
        first=base.predicted_first_token_seconds
        if not row['prefill_finished']:
            first+=self.expert_backlog_seconds(row['p'])
        production=now if row['prefill_finished'] else first
        kv_ready=max(production,now+base.remaining_transfer_seconds)
        active_ids=self.active_decode_batches.get(d,())
        queued_ids=list(self.queues[d])
        # At KV arrival the other active members may have rejoined the RR queue.
        # Keep one visible round ahead unless their declared output cap proves
        # no further step is possible. Do not read simulated output_tokens/EOS.
        ahead=queued_ids+[rid for rid in active_ids if self.requests[rid]['generated']+1<
            self.requests[rid].get('max_output_tokens',self.config.reservations.reserve_max_output_tokens)]
        round_cost=sum(self.mlu_decode_cost(d,[self.requests[rid] for rid in ahead[i:i+size]])
                       for i in range(0,len(ahead),size))
        active_cost=self.mlu_decode_cost(d,[self.requests[rid] for rid in active_ids]) if active_ids else 0.
        if active_ids and self.profile.decode_layered:
            active_cost=self.layered_decode_remaining_seconds(d)
        elif active_ids:
            # Known dense progress can be subtracted; the rest remains an
            # isolated conservative estimate, including communication.
            leader=self.requests[active_ids[0]]
            sample=self.profile.lookup_decode_batch([self.requests[rid]['input_tokens']+max(0,self.requests[rid]['generated']-1)
                for rid in active_ids],len(self.instances[d].gpu_ids))
            count=sample['collectives_per_step'];segment=leader.get('segment_index',0)
            part=sample['compute_seconds']/max(1,count)
            queued=any(task[0]==active_ids[0] and task[2]=='dense' for task in self.ep_lanes[d]) if self.ep_enabled else False
            compute=(part if queued else max(0.,leader.get('compute_segment_end',now)-now))+max(0,count-segment-1)*part
            active_cost=min(active_cost,compute+self.collective_seconds(self.instances[d].gpu_ids,
                dict(sample,collectives_per_step=max(0,count-segment)))+
                (self.ep_estimate_seconds(d,len(active_ids),self.profile.expert.raw['decode_layers'],
                    spans=self.ep_decode_spans([self.requests[rid] for rid in active_ids])) if self.ep_enabled else 0.))
        expert_wait=self.expert_backlog_seconds(d)
        elapsed=max(0.,kv_ready-now)
        wait=max(0.,active_cost-elapsed)+max(round_cost,max(0.,expert_wait-elapsed))
        next_rows=[row]+[self.requests[rid] for rid in queued_ids[:size-1]]
        step=self.mlu_decode_cost(d,next_rows)
        anchor=row['first_token_at_seconds'] if row['prefill_finished'] else min(first,base.ttft_deadline_seconds)
        deadline=anchor+row.get('itl_slo_seconds',self.config.evaluation.default_itl_slo_seconds)-wait-step
        slack=deadline-kv_ready
        return replace(base,predicted_first_token_seconds=first,latest_kv_delivery_seconds=deadline,
            slack_seconds=slack,normalized_slack=slack/row.get('itl_slo_seconds',self.config.evaluation.default_itl_slo_seconds),
            ttft_feasible=first<=base.ttft_deadline_seconds+1e-12,production_ready_seconds=production,
            decode_wait_seconds=wait,decode_step_seconds=step,expert_queue_wait_seconds=expert_wait)
