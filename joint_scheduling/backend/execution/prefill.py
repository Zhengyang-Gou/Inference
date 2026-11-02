"""PrefillRuntime: explicit runtime component; scheduling order preserved."""
from ..prefill_batching import PrefillBatchingMixin
from ...kv_scheduler import split_layer_kv
from .component import RuntimeComponent


class PrefillRuntime(PrefillBatchingMixin, RuntimeComponent):
    owned_fields = frozenset(['active_prefill_batches', 'pending_prefill_ticks', 'prefill_admission_sequence', 'prefill_batch_stats', 'prefill_batching', 'prefill_chunk_completed_count', 'prefill_chunk_query_tokens', 'prefill_chunk_started_count'])

    def kick_prefill(self, instance):
        if self.prefill_batching:
            self.request_prefill_batch(instance)
            return
        if self.busy[instance] or not self.queues[instance]:
            return
        rid = self.queues[instance][0]
        row = self.requests[rid]
        if row["prefill_chunks"] and row.get("prefill_started"):
            self.queues[instance].popleft()
            self.busy[instance] = True
            self.active_by_instance[instance] = rid
            row["state"] = "prefill"
            self.observer.queue_started(instance, self.backend.now_seconds()-row["queued_at_seconds"])
            self.execute_prefill(row)
            return
        if not self.prepare_prefill(row):
            if row["state"] == "failed":
                self.queues[instance].popleft()
                self.kick_prefill(instance)
            elif row["prefill_chunks"]:
                # Let partial prefills drain their already reserved memory.
                resumable = next((item for item in self.queues[instance]
                                  if self.requests[item].get("prefill_started")), None)
                if resumable is not None:
                    self.queues[instance].remove(resumable)
                    self.queues[instance].appendleft(resumable)
                    self.kick_prefill(instance)
            return
        self.queues[instance].popleft()
        self.busy[instance] = True
        self.active_by_instance[instance] = rid
        row["state"] = "prefill"
        self.queue_started(instance, row)
        row["prefill_started"] = True
        self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
        self.execute_prefill(row)

    def enqueue_cached_prefix(self, row):
        if "prefix_plan" not in row or row.get("cached_prefix_submitted") or self.kv_mode == "whole_ready_immediate":
            return
        row["cached_prefix_submitted"] = True
        plan = row["prefix_plan"]
        for index in range(len(plan.layer_bytes)):
            self.enqueue_blocks(row, plan.transfer_blocks(index, self.config.kv_scheduler.max_block_bytes,
                                                         self.backend.now_seconds(), cached=True))

    def execute_prefill(self, row):
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        chunk = row["prefill_chunks"][row["prefill_chunk_index"]] if row["prefill_chunks"] else None
        if chunk is not None:
            row["prefill_steps"] = chunk["steps"]
            self.prefill_chunk_started_count += 1
            self.emit("prefill_chunk_started", request_id=row["request_id"], chunk_index=row["prefill_chunk_index"],
                      chunk_tokens=chunk["chunk_tokens"], cached_tokens=chunk["cached_tokens"])
        steps = row["prefill_steps"]
        plan = row.get("prefix_plan")
        self.enqueue_cached_prefix(row)

        def execute(index):
            if index == len(steps):
                if chunk is not None:
                    self.prefill_chunk_done(row)
                else:
                    self.prefill_done(row["request_id"])
                return
            sample = steps[index]
            row["prefill_layer_index"] = index
            def ready():
                last_chunk = chunk is None or row["prefill_chunk_index"] == len(row["prefill_chunks"])-1
                if last_chunk:
                    row["ready_layers"] = index+1
                query = chunk["chunk_tokens"] if chunk is not None else row["input_tokens"]
                prefix_bytes = chunk["cached_tokens"]*sample["kv_bytes_per_token"] if chunk is not None else 0
                size = query * sample["kv_bytes_per_token"]
                base, extra = divmod(prefix_bytes+size, len(p.gpu_ids))
                old_base, old_extra = divmod(prefix_bytes, len(p.gpu_ids))
                if plan is not None:
                    start = chunk["cached_tokens"] if chunk is not None else 0
                    plan.materialize_layer(index, start, start+query)
                else:
                    for rank, gpu in enumerate(p.gpu_ids):
                        self.ledger.materialize("gpu-{}".format(gpu), "p:" + row["request_id"], base + int(rank < extra)-old_base-int(rank < old_extra))
                if chunk is not None:
                    self.emit("prefill_chunk_layer_ready", request_id=row["request_id"], layer_id=index,
                              chunk_index=row["prefill_chunk_index"], size_bytes=size, cached_tokens=chunk["cached_tokens"])
                if self.profile.layered and last_chunk:
                    self.emit("layer_ready", request_id=row["request_id"], layer_id=index,
                              size_bytes=row["input_tokens"]*sample["kv_bytes_per_token"], source_gpu_ids=list(p.gpu_ids), destination_gpu_ids=list(d.gpu_ids))
                if row["output_tokens"] > 1 and self.kv_mode != "whole_ready_immediate":
                    blocks = (plan.transfer_blocks(index, self.config.kv_scheduler.max_block_bytes,
                                                   self.backend.now_seconds(), cached=False,
                                                   start_token=chunk["cached_tokens"] if chunk else 0,
                                                   end_token=(chunk["cached_tokens"]+query) if chunk else row["input_tokens"]) if plan is not None else
                              split_layer_kv(row["request_id"], index, p.gpu_ids, d.gpu_ids,
                                             size, self.config.kv_scheduler.max_block_bytes, self.backend.now_seconds(), start_bytes=prefix_bytes))
                    self.enqueue_blocks(row, blocks)
                self.emit("resources", request_id=row["request_id"], domains=self.ledger.snapshot())
                execute(index + 1)
            self.execute_step(row, "prefill", chunk["chunk_tokens"] if chunk is not None else row["input_tokens"], ready, sample,
                              index if self.profile.layered else None,
                              batch_details=dict(chunk_index=row["prefill_chunk_index"],cached_tokens=chunk["cached_tokens"]) if chunk is not None else None)
        execute(0)

    def prefill_chunk_done(self, row):
        chunk = row["prefill_chunks"][row["prefill_chunk_index"]]
        self.prefill_chunk_completed_count += 1
        self.prefill_chunk_query_tokens += chunk["chunk_tokens"]
        row["prefill_computed_tokens"] = chunk["cached_tokens"]+chunk["chunk_tokens"]
        self.emit("prefill_chunk_completed", request_id=row["request_id"], chunk_index=row["prefill_chunk_index"],
                  chunk_tokens=chunk["chunk_tokens"], cached_tokens=chunk["cached_tokens"])
        if row["prefill_chunk_index"] == len(row["prefill_chunks"])-1:
            self.prefill_done(row["request_id"])
            return
        row["prefill_chunk_index"] += 1
        row["prefill_steps"] = row["prefill_chunks"][row["prefill_chunk_index"]]["steps"]
        row["prefill_layer_index"] = 0
        row["state"] = "queued_prefill"
        row["queued_at_seconds"] = self.backend.now_seconds()
        self.busy[row["p"]] = False
        self.active_by_instance.pop(row["p"],None)
        self.queues[row["p"]].append(row["request_id"])
        self.kick_prefill(row["p"])
        self.request_dispatch()

    def prefill_done(self, rid, release_instance=True):
        row = self.requests[rid]
        row["generated"] = 1
        row["prefill_finished"] = True
        row["first_token_at_seconds"] = self.backend.now_seconds()
        row["token_times"].append(self.backend.now_seconds())
        if release_instance:
            self.active_by_instance.pop(row["p"], None)
            self.busy[row["p"]] = False
        self.recorder.token_returned(rid, self.backend.now_seconds(), 1)
        if "prefix_plan" in row:
            row["prefix_plan"].publish("p", self.backend.now_seconds())
        if row["output_tokens"] == 1:
            self.finish_request(rid)
            return
        row["state"] = "kv_transfer"
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        if self.kv_mode == "whole_ready_immediate":
            # Preserve the per-layer sharding layout, but coalesce each rank's
            # data into one logical message only after all prefill has finished.
            if "prefix_plan" in row:
                for src, dst, amount, source, destination in row["prefix_plan"].whole_transfers():
                    self.start_kv_transfer(row, src, dst, amount,
                                           source_object_id=source, destination_object_id=destination)
            else:
                for rank, (src, dst) in enumerate(zip(p.gpu_ids, d.gpu_ids)):
                    amount = row["kv_rank_bytes"][rank]
                    if amount:
                        self.start_kv_transfer(row, src, dst, amount)
        elif self.kv_mode == "whole_ready_chunked":
            for block in row["pending_kv_blocks"]:
                self.start_kv_block(row, block)
            row["pending_kv_blocks"].clear()
        self.release_source_if_submitted(row)
        self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
        self.maybe_start_decode(row)
        if release_instance:
            self.kick_prefill(row["p"])
        self.request_dispatch()

    def kick_all_prefill(self):
        for instance in self.prefills:
            self.kick_prefill(instance)
