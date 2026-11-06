"""DecodeRuntime: explicit runtime component; scheduling order preserved."""
import math
from .component import RuntimeComponent


class DecodeRuntime(RuntimeComponent):
    owned_fields = frozenset(['active_decode_batches', 'active_decode_steps', 'decode_batch_completed', 'decode_batch_counter', 'decode_batch_histogram'])

    def init_decode_state(self):
        self.active_decode_batches = {}
        self.active_decode_steps = {}
        self.decode_batch_counter = 0
        self.decode_batch_completed = 0
        self.decode_batch_histogram = {}

    def maybe_start_decode(self, row):
        expected = row["input_tokens"] * self.profile.kv_bytes_per_token
        if not row["prefill_finished"] or row["kv_received_bytes"] != expected or row["decode_enqueued"]:
            return
        row["decode_enqueued"] = True
        row["state"] = "queued_decode"
        row["queued_at_seconds"] = self.backend.now_seconds()
        self.queues[row["d"]].append(row["request_id"])
        self.emit("kv_ready", request_id=row["request_id"])
        self.kick_decode(row["d"])

    def kick_decode(self, instance):
        if self.busy[instance] or not self.queues[instance]:
            return
        members = tuple(self.queues[instance].popleft()
                        for _ in range(min(len(self.queues[instance]), self.config.runtime.decode_batch_size)))
        self.busy[instance] = True
        self.active_by_instance[instance] = members[0]
        self.active_decode_batches[instance] = members
        for rid in members:
            row = self.requests[rid]
            row["state"] = "decode"
            self.queue_started(instance, row)
        contexts = [self.requests[rid]["input_tokens"]+self.requests[rid]["generated"]-1 for rid in members]
        sample = self.profile.lookup_decode_batch(contexts, len(self.instances[instance].gpu_ids))
        self.decode_batch_counter += 1
        details = dict(batch_id="decode-batch-{}".format(self.decode_batch_counter),
                       request_ids=list(members), batch_size=len(members), context_tokens=contexts)
        self.emit("decode_batch_started", instance_id=instance, **details)
        def complete():
            # Keep the instance busy until every member has returned its token.
            for rid in members:
                self.decode_done(rid)
            self.busy[instance] = False
            self.active_by_instance.pop(instance, None)
            self.active_decode_batches.pop(instance, None)
            self.active_decode_steps.pop(instance,None)
            self.decode_batch_completed += 1
            self.decode_batch_histogram[len(members)] = self.decode_batch_histogram.get(len(members), 0)+1
            self.emit("decode_batch_completed", instance_id=instance, **details)
            self.kick_decode(instance)
        if self.profile.decode_layered:
            steps=self.profile.decode_batch_steps(contexts,len(self.instances[instance].gpu_ids))
            self.active_decode_steps[instance]=dict(steps=steps,index=0)
            def layer(index):
                if index==len(steps):
                    complete();return
                self.active_decode_steps[instance]['index']=index
                for rid in members:
                    self.requests[rid]['decode_layer_index']=index
                    self.requests[rid]['decode_nonexpert_finished']=False
                self.execute_step(self.requests[members[0]],'decode',max(contexts),lambda:layer(index+1),
                    sample=steps[index],layer_id=steps[index]['layer_id'],batch_details=details)
            layer(0)
        else:
            self.execute_step(self.requests[members[0]], "decode", max(contexts), complete,
                              sample=sample, batch_details=details)

    def decode_done(self, rid):
        """Return one member token; the batch callback owns instance rescheduling."""
        row = self.requests[rid]
        row["generated"] += 1
        d = self.instances[row["d"]]
        previous_size = math.ceil((row["input_tokens"] + row["generated"] - 2) * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        current_size = math.ceil((row["input_tokens"] + row["generated"] - 1) * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        for gpu in d.gpu_ids:
            identity = row["prefix_plan"].growth_id if "prefix_plan" in row else "d:" + rid
            self.ledger.materialize("gpu-{}".format(gpu), identity, current_size - previous_size)
        self.recorder.token_returned(rid, self.backend.now_seconds(), row["generated"])
        row["token_times"].append(self.backend.now_seconds())
        if row["generated"] == row["output_tokens"]:
            self.finish_request(rid)
        else:
            row["state"] = "queued_decode"
            row["queued_at_seconds"] = self.backend.now_seconds()
            self.queues[row["d"]].append(rid)
            self.emit("decode_requeued", request_id=rid, instance_id=row["d"])
