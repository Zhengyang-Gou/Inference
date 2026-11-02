"""P-only token-budget batching modeled after the vLLM V1 FCFS scheduler."""
import json
from ..kv_scheduler import split_layer_kv
from .scheduling import prefill_members


class PrefillBatchingMixin:
    def init_prefill_batching(self):
        self.prefill_batching=bool(self.config.runtime.prefill_batch_token_budget)
        self.active_prefill_batches={}
        self.pending_prefill_ticks=set()
        self.prefill_admission_sequence=0
        self.prefill_batch_stats=dict(started_batches=0,completed_batches=0,
            completed_query_tokens=0,batch_size_histogram={},total_token_histogram={},
            memory_deferred_admissions=0,peak_running_requests={},by_instance={})

    def request_prefill_batch(self,instance):
        if self.busy[instance] or not self.queues[instance] or instance in self.pending_prefill_ticks:
            return
        # One nanosecond tick coalesces callbacks at the same simulation instant;
        # it never looks at or waits for a later request arrival.
        at=self.backend.now_seconds()+1e-9
        if at>self.config.runtime.time_limit_seconds:return
        self.pending_prefill_ticks.add(instance)
        def schedule():
            self.pending_prefill_ticks.discard(instance)
            self.start_prefill_batch(instance)
        self.backend.call_at(at,schedule)

    def start_prefill_batch(self,instance):
        if self.busy[instance] or not self.queues[instance]:return
        runtime=self.config.runtime
        queued=[self.requests[rid] for rid in self.queues[instance]]
        running_count=sum(bool(r.get('prefill_started')) for r in queued)
        def admit(row):
            nonlocal running_count
            if not self.prepare_prefill(row):
                return False
            self.prefill_admission_sequence+=1
            row['prefill_admission_sequence']=self.prefill_admission_sequence
            row['prefill_started']=True
            running_count+=1
            return True
        members,deferred=prefill_members(queued,runtime,admit)
        if deferred:self.prefill_batch_stats['memory_deferred_admissions']+=1
        failed=[row['request_id'] for row in queued if row['state']=='failed']
        for rid in failed:self.queues[instance].remove(rid)
        if not members:
            if failed:self.request_prefill_batch(instance)
            return
        if len(members)>runtime.prefill_batch_size:raise RuntimeError('prefill request cap exceeded')
        steps=self.profile.prefill_batch_steps(members,len(self.instances[instance].gpu_ids))
        stats=self.prefill_batch_stats;stats['started_batches']+=1
        batch=dict(batch_id='prefill-batch-{}'.format(stats['started_batches']),members=members,
                   steps=steps,started_at_seconds=self.backend.now_seconds(),layer_index=0)
        self.active_prefill_batches[instance]=batch
        self.busy[instance]=True;self.active_by_instance[instance]=members[0]['request_id']
        peak=stats['peak_running_requests'];peak[instance]=max(peak.get(instance,0),running_count)
        for member in members:
            row=self.requests[member['request_id']]
            self.queues[instance].remove(row['request_id'])
            row['state']='prefill';row['prefill_steps']=steps
            self.enqueue_cached_prefix(row)
            if member['new_admission']:self.queue_started(instance,row)
            else:self.observer.queue_started(instance,self.backend.now_seconds()-row['queued_at_seconds'])
            self.prefill_chunk_started_count+=1
            self.emit('prefill_chunk_started',request_id=row['request_id'],chunk_index=row['prefill_chunk_index'],
                chunk_tokens=member['chunk_tokens'],cached_tokens=member['cached_tokens'],batch_id=batch['batch_id'])
        details=dict(batch_id=batch['batch_id'],request_ids=[m['request_id'] for m in members],
            batch_size=len(members),total_query_tokens=sum(m['chunk_tokens'] for m in members),
            scheduled_tokens={m['request_id']:m['chunk_tokens'] for m in members},
            cached_tokens={m['request_id']:m['cached_tokens'] for m in members})
        batch['details']=details
        self.emit('prefill_batch_started',instance_id=instance,**details)
        self.execute_prefill_batch_layer(instance,0)

    def execute_prefill_batch_layer(self,instance,index):
        batch=self.active_prefill_batches[instance]
        if index==len(batch['steps']):
            self.finish_prefill_batch(instance);return
        batch['layer_index']=index
        for member in batch['members']:self.requests[member['request_id']]['prefill_layer_index']=index
        sample=batch['steps'][index]
        def ready():
            for member in batch['members']:self.materialize_prefill_batch_member(member,index,sample)
            self.execute_prefill_batch_layer(instance,index+1)
        leader=self.requests[batch['members'][0]['request_id']]
        self.execute_step(leader,'prefill',batch['details']['total_query_tokens'],ready,
                          sample=sample,layer_id=index,batch_details=batch['details'])

    def materialize_prefill_batch_member(self,member,index,sample):
        row=self.requests[member['request_id']];p=self.instances[row['p']];d=self.instances[row['d']]
        prefix=member['cached_tokens']*sample['kv_bytes_per_token']
        size=member['chunk_tokens']*sample['kv_bytes_per_token']
        old,oldextra=divmod(prefix,len(p.gpu_ids));end,extra=divmod(prefix+size,len(p.gpu_ids))
        plan=row.get('prefix_plan')
        if plan is not None:
            plan.materialize_layer(index,member['cached_tokens'],member['cached_tokens']+member['chunk_tokens'])
        else:
            for rank,gpu in enumerate(p.gpu_ids):
                self.ledger.materialize('gpu-{}'.format(gpu),'p:'+row['request_id'],end+int(rank<extra)-old-int(rank<oldextra))
        final=member['cached_tokens']+member['chunk_tokens']==row['input_tokens']
        if final:
            row['ready_layers']=index+1
            self.emit('layer_ready',request_id=row['request_id'],layer_id=index,
                size_bytes=row['input_tokens']*sample['kv_bytes_per_token'],source_gpu_ids=list(p.gpu_ids),destination_gpu_ids=list(d.gpu_ids))
        self.emit('prefill_chunk_layer_ready',request_id=row['request_id'],layer_id=index,
            chunk_index=row['prefill_chunk_index'],size_bytes=size,cached_tokens=member['cached_tokens'])
        if row['output_tokens']>1 and self.kv_mode!='whole_ready_immediate':
            blocks=(plan.transfer_blocks(index,self.config.kv_scheduler.max_block_bytes,
                self.backend.now_seconds(),cached=False,start_token=member['cached_tokens'],
                end_token=member['cached_tokens']+member['chunk_tokens']) if plan is not None else
                split_layer_kv(row['request_id'],index,p.gpu_ids,d.gpu_ids,size,
                    self.config.kv_scheduler.max_block_bytes,self.backend.now_seconds(),start_bytes=prefix))
            self.enqueue_blocks(row,blocks)
        self.emit('resources',request_id=row['request_id'],domains=self.ledger.snapshot())

    def finish_prefill_batch(self,instance):
        batch=self.active_prefill_batches[instance];stats=self.prefill_batch_stats
        # No next P batch may start while member completion callbacks run.
        for member in batch['members']:
            row=self.requests[member['request_id']]
            row['prefill_computed_tokens']+=member['chunk_tokens']
            self.prefill_chunk_completed_count+=1
            self.prefill_chunk_query_tokens+=member['chunk_tokens']
            self.emit('prefill_chunk_completed',request_id=row['request_id'],chunk_index=row['prefill_chunk_index'],
                chunk_tokens=member['chunk_tokens'],cached_tokens=member['cached_tokens'],batch_id=batch['batch_id'])
            row['prefill_chunk_index']+=1
            if row['prefill_computed_tokens']==row['input_tokens']:
                self.prefill_done(row['request_id'],release_instance=False)
            else:
                row['state']='queued_prefill';row['queued_at_seconds']=self.backend.now_seconds()
                self.queues[instance].append(row['request_id'])
        stats['completed_batches']+=1;total=batch['details']['total_query_tokens']
        stats['completed_query_tokens']+=total
        for key,value in (('batch_size_histogram',len(batch['members'])),('total_token_histogram',total)):
            stats[key][value]=stats[key].get(value,0)+1
        row=stats['by_instance'].setdefault(instance,dict(completed_batches=0,query_tokens=0,modeled_compute_seconds=0,
                                                         busy_seconds=0,communication_wait_seconds=0))
        compute=sum(step['compute_seconds'] for step in batch['steps'])
        elapsed=self.backend.now_seconds()-batch['started_at_seconds']
        row['completed_batches']+=1;row['query_tokens']+=total
        row['modeled_compute_seconds']+=compute;row['busy_seconds']+=elapsed
        row['communication_wait_seconds']+=max(0,elapsed-compute)
        self.busy[instance]=False;self.active_by_instance.pop(instance,None)
        self.active_prefill_batches.pop(instance)
        self.emit('prefill_batch_completed',instance_id=instance,**batch['details'])
        self.kick_prefill(instance);self.request_dispatch()

    def batched_prefill_remaining_seconds(self,row):
        """Heuristic: current common batch shared once, then future solo chunks.

        Used in the existing conservative SLO estimate, not as a scheduling oracle.
        Only current queue members, processed-token positions and profiles are read.
        """
        ranks=self.instances[row['p']].gpu_ids;runtime=self.config.runtime
        cached=row['prefill_computed_tokens'];total=0
        active=self.active_prefill_batches.get(row['p'])
        if active and row['state']=='prefill':
            member=next(m for m in active['members'] if m['request_id']==row['request_id'])
            cached+=member['chunk_tokens'];index=active['layer_index'];sample=active['steps'][index]
            count=sample['collectives_per_step'];segment=row.get('segment_index',0)
            current=max(0,row.get('compute_segment_end',0)-self.backend.now_seconds())
            current+=max(0,count-segment-1)*sample['compute_seconds']/max(1,count)
            current+=self.collective_seconds(ranks,dict(sample,collectives_per_step=max(0,count-segment)))
            current+=sum(s['compute_seconds']+self.collective_seconds(ranks,s) for s in active['steps'][index+1:])
            total+=current/len(active['members'])
        cap=min(runtime.prefill_chunk_tokens,runtime.prefill_batch_token_budget)
        while cached<row['input_tokens']:
            query=min(cap,row['input_tokens']-cached)
            steps=self.profile.prefill_batch_steps([dict(chunk_tokens=query,cached_tokens=cached)],len(ranks))
            total+=sum(s['compute_seconds']+self.collective_seconds(ranks,s) for s in steps)
            cached+=query
        return total

    def write_prefill_batch_summary(self):
        value=dict(self.prefill_batch_stats,enabled=self.prefill_batching,
            max_requests=self.config.runtime.prefill_batch_size,token_budget=self.config.runtime.prefill_batch_token_budget,
            per_request_chunk_cap=self.config.runtime.prefill_chunk_tokens,scheduling='running_first_waiting_fcfs' if self.prefill_batching else 'legacy_executor',
            timing='packed_total_query_linear_plus_sum_independent_attention',
            coalescing_tick_ns=1,active_batches_at_end={key:batch['details'] for key,batch in self.active_prefill_batches.items()},
            pending_scheduling_ticks=sorted(self.pending_prefill_ticks),
            utilization_interpretation='Modeled compute/busy/communication durations; not measured GPU utilization. Active cut-off batches are listed and excluded from completed aggregates.')
        (self.observer.output.parent/'prefill_batch_summary.json').write_text(json.dumps(value,indent=2)+'\n')
