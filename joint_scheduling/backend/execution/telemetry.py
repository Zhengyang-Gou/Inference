"""RuntimeTelemetry: explicit runtime component; scheduling order preserved."""
import json
import copy
from .component import RuntimeComponent


class RuntimeTelemetry(RuntimeComponent):
    owned_fields = frozenset(['binding_report'])

    def close(self):
        self.binding_report = self.finish_binding()
        self.write_ep_summary()
        self.write_prefill_batch_summary()
        (self.observer.output.parent / "prefill_chunk_summary.json").write_text(json.dumps(dict(
            chunk_tokens=self.config.runtime.prefill_chunk_tokens,
            started_chunks=self.prefill_chunk_started_count, completed_chunks=self.prefill_chunk_completed_count,
            completed_query_tokens=self.prefill_chunk_query_tokens,
            scheduling="within_token_budget_batch" if self.prefill_batching else "round_robin_at_chunk_boundary" if self.config.runtime.prefill_chunk_tokens else "whole_request_fifo"),indent=2)+"\n")
        (self.observer.output.parent / "decode_batch_summary.json").write_text(json.dumps(dict(
            max_batch_size=self.config.runtime.decode_batch_size,
            started_batches=self.decode_batch_counter, completed_batches=self.decode_batch_completed,
            completed_batch_size_histogram=self.decode_batch_histogram,
            context_model="padded_max_context", timing_model="profile_batch_and_context_interpolation",
            active_batches_at_end={key:list(ids) for key,ids in self.active_decode_batches.items()}), indent=2)+"\n")
        if self.prefix_cache is not None:
            (self.observer.output / "prefix_cache_summary.json").write_text(
                json.dumps(self.prefix_cache.snapshot(), indent=2)+"\n")
        if self.scheduler is not None:
            summary = dict(self.scheduler.summary, pending_blocks=len(self.scheduler.pending),
                           algorithm=self.config.kv_scheduler.policy,
                           tracked_mlu_requests=len(self.scheduler.levels) if self.mlu_enabled else 0,
                           inflight_blocks=len(self.scheduler.inflight), priority=self.scheduler.priority,
                           inflight_budget_mode=self.config.kv_scheduler.inflight_budget_mode,
                           collective_protection_enabled=self.config.kv_scheduler.collective_protection_enabled,
                           network_observation_enabled=self.config.feedback.network_observation_enabled,
                           collective_max_blocking_us=self.config.kv_scheduler.collective_max_blocking_us,
                           decode_admission_limit=self.config.kv_scheduler.decode_admission_limit,
                           decode_owners_remaining=len(self.scheduler.decode_owners),
                           protection=("disabled" if not self.config.kv_scheduler.collective_protection_enabled else
                               "phase byte demand and directed drain envelope; urgent feasible KV bypasses soft EP reserve" if self.mlu_enabled else
                               "collective bandwidth reserve; bandwidth/ACK window bounded by collective drain time; pause on queue/PFC pressure"
                               if self.config.kv_scheduler.inflight_budget_mode == "adaptive" else
                               "collective bandwidth reserve; one-block reserved-resource cap; pause on queue/PFC pressure"),
                           path_budgets="conservative union of all ECMP shortest-path directed edges",
                           blocked_checks_semantics="candidate checks, not time duration")
            (self.observer.output / "kv_scheduler_summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        if self.network_monitor is not None:
            self.network_monitor.close()

    def emit(self, event, **data):
        if self.hooks.event is not None:
            self.hooks.event(event, self.backend.now_ns, copy.deepcopy(data))
        self.recorder.record_event(event, self.backend.now_seconds(), **data)
        self.capture(event)

    def capture(self, reason):
        self.capture_binding()
        if not self.config.feedback.state_observation_enabled:
            return
        now = self.backend.now_seconds()
        instances = {key: {"role": placement.role, "gpu_ids": list(placement.gpu_ids),
            "queue_length": len(self.queues[key]),
            "head_request_id": self.queues[key][0] if self.queues[key] else None,
            "oldest_queue_wait_seconds": now - self.requests[self.queues[key][0]]["queued_at_seconds"] if self.queues[key] else 0.0,
            "execution_busy": self.busy[key], "compute_busy": key in self.compute_active}
            for key, placement in sorted(self.instances.items())}
        self.observer.capture(self.request_counts, instances, self.ledger.snapshot(), reason)

    def queue_started(self, instance, row):
        wait = self.backend.now_seconds() - row["queued_at_seconds"]
        self.observer.queue_started(instance, wait)
        if self.instances[instance].role == "prefill" or row["generated"] == 1:
            kind = "prefill_started" if self.instances[instance].role == "prefill" else "decode_started"
            self.recorder.record_event(kind, self.backend.now_seconds(), request_id=row["request_id"],
                                       instance_id=instance, queue_wait_seconds=wait)
        self.emit("queue_dequeued", request_id=row["request_id"], instance_id=instance, queue_wait_seconds=wait)
