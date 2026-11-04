"""ComputeExecutor: explicit runtime component; scheduling order preserved."""
from .component import RuntimeComponent


class ComputeExecutor(RuntimeComponent):
    owned_fields = frozenset(['compute_active'])

    def init_compute_state(self):
        self.compute_active = set()

    def execute_step(self, row, phase, tokens, complete, sample=None, layer_id=None, batch_details=None):
        instance = self.instances[row["p"] if phase == "prefill" else row["d"]]
        sample = sample if sample is not None else self.profile.lookup(phase, tokens, len(instance.gpu_ids))
        details = {"layer_id": layer_id} if layer_id is not None else {}
        details.update(batch_details or {})
        if self.ep_enabled:
            self.execute_ep_step(row, phase, tokens, complete, sample, layer_id, details)
            return
        remaining = sample["collectives_per_step"]
        compute_part = sample["compute_seconds"] / max(1, remaining)
        self.emit("compute_step_started", request_id=row["request_id"], phase=phase, instance_id=instance.instance_id,
                  tokens=tokens, modeled_compute_seconds=sample["compute_seconds"], **details)

        def segment(index):
            row["segment_index"] = index
            def compute_finished():
                if instance.instance_id in self.compute_active:
                    self.compute_active.remove(instance.instance_id)
                    self.emit("compute_segment_completed", request_id=row["request_id"], instance_id=instance.instance_id)
                if index < remaining:
                    def collective_done():
                        self.collective_demands.pop(instance.instance_id, None)
                        segment(index+1)
                        self.request_dispatch()
                    self.allreduce(row["request_id"], instance.gpu_ids, sample["collective_bytes"], collective_done)
                else:
                    self.emit("compute_step_completed", request_id=row["request_id"], phase=phase, instance_id=instance.instance_id, **details)
                    complete()
            if index < remaining or remaining == 0:
                self.compute_active.add(instance.instance_id)
                row["compute_segment_end"] = self.backend.now_seconds()+compute_part
                if phase == "decode":
                    for rid in self.active_decode_batches.get(instance.instance_id, ()):
                        self.requests[rid]["compute_segment_end"] = row["compute_segment_end"]
                        self.requests[rid]["segment_index"] = index
                if phase == "prefill" and instance.instance_id in self.active_prefill_batches:
                    for member in self.active_prefill_batches[instance.instance_id]['members']:
                        peer=self.requests[member['request_id']]
                        peer['compute_segment_end']=row['compute_segment_end'];peer['segment_index']=index
                if self.scheduler is not None and index < remaining:
                    work = self.scheduler.resources.ring_work(instance.gpu_ids, sample["collective_bytes"])
                    self.collective_demands[instance.instance_id] = dict(resources=tuple(work),
                        starts_at_seconds=row["compute_segment_end"])
                    self.request_dispatch()
                self.emit("compute_segment_started", request_id=row["request_id"], instance_id=instance.instance_id,
                          modeled_compute_seconds=compute_part)
                self.backend.call_at(self.backend.now_seconds() + compute_part, compute_finished)
            else:
                compute_finished()
        segment(0)
