"""KVRuntime: explicit runtime component; scheduling order preserved."""
from dataclasses import asdict
from .component import RuntimeComponent


class KVRuntime(RuntimeComponent):
    owned_fields = frozenset(['dispatch_at', 'dispatch_generation', 'kv_mode', 'mlu_enabled', 'network_monitor', 'scheduler'])

    def schedule_network_sample(self):
        at = self.backend.now_seconds() + self.config.feedback.network_observation_interval_us / 1e6
        if at <= self.config.runtime.time_limit_seconds:
            def sample():
                self.network_monitor.sample()
                self.request_dispatch(self.config.feedback.observation_delay_us / 1e6
                                      + self.config.feedback.scheduling_overhead_us / 1e6)
                self.schedule_network_sample()
            self.backend.call_at(at, sample)

    def request_dispatch(self, delay=None):
        if self.scheduler is None or not self.scheduler.pending:
            return
        overhead = self.config.feedback.scheduling_overhead_us / 1e6
        at = self.backend.now_seconds() + max(1e-9, overhead, delay if delay is not None else overhead)
        if at > self.config.runtime.time_limit_seconds or self.dispatch_at is not None and self.dispatch_at <= at:
            return
        self.dispatch_generation += 1
        generation = self.dispatch_generation
        self.dispatch_at = at
        def dispatch():
            if generation != self.dispatch_generation:
                return
            self.dispatch_at = None
            network = self.network_monitor.latest_observed()
            demands = list(self.collective_demands.values())
            now = self.backend.now_seconds()
            self.scheduler.refresh(now, network, demands)
            self.slo_budget_cache = {}
            budgets = {rid: self.request_budget(self.requests[rid], network)
                       for rid in sorted({block.request_id for block in self.scheduler.pending.values()})}
            permits = self.scheduler.admit(now, dict(network=network, collectives=demands, requests=budgets))
            self.recorder.record_event("kv_scheduling_decision", now,
                request_budgets={rid: asdict(budget) for rid, budget in budgets.items()}, **self.scheduler.last_decision)
            for permit in permits:
                block = self.scheduler.inflight[permit.block_id]
                self.start_kv_block(self.requests[block.request_id], block, permit.priority)
            if self.scheduler.pending:
                self.request_dispatch(min(self.config.kv_scheduler.control_interval_us / 1e6,
                                          self.scheduler.next_credit_delay()))
        self.backend.call_at(at, dispatch)

    def enqueue_blocks(self, row, blocks):
        if self.scheduler is not None:
            self.scheduler.on_layer_ready(blocks)
            self.request_dispatch()
        elif self.kv_mode == "layer_ready_immediate":
            for block in blocks:
                self.start_kv_block(row, block)
        else:
            row["pending_kv_blocks"].extend(blocks)

    def start_kv_block(self, row, block, priority=2):
        self.start_kv_transfer(row, block.source_gpu_id, block.destination_gpu_id, block.size_bytes,
                               block_id=block.block_id, layer_id=block.layer_id, priority=priority,
                               offset_bytes=block.offset_bytes, ready_at_seconds=block.ready_at_seconds,
                               **(dict(source_object_id=block.source_object_id, destination_object_id=block.destination_object_id)
                                  if block.source_object_id else {}))

    def start_kv_transfer(self, row, src, dst, size, **details):
        rid = row["request_id"]
        domain = "gpu-{}".format(src)
        source_object = details.get("source_object_id", "p:" + rid)
        destination_object = details.get("destination_object_id", "d:" + rid)
        self.ledger.pin(domain, source_object)
        row["kv_unsent_bytes"] -= size
        def received():
            row["kv_active_transfers"].pop(transfer_id, None)
            if self.scheduler is not None:
                self.scheduler.completed(details["block_id"], self.backend.now_seconds())
            self.ledger.unpin(domain, source_object)
            self.ledger.materialize("gpu-{}".format(dst), destination_object, size)
            row["kv_received_bytes"] += size
            row["kv_received_by_gpu"][dst] += size
            if row["kv_received_bytes"] == row["input_tokens"] * self.profile.kv_bytes_per_token:
                if "prefix_plan" in row:
                    row["prefix_plan"].publish("d", self.backend.now_seconds())
                self.emit("kv_all_received", request_id=rid)
            self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
            self.maybe_start_decode(row)
            self.kick_all_prefill()
            self.request_dispatch()
        transfer_id = self.transfer(rid, "kv", src, dst, size, received, **details)
        row["kv_active_transfers"][transfer_id] = (dst, size)
        self.release_source_if_submitted(row)
