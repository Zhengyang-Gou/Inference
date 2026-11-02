"""RequestLifecycle: explicit runtime component; scheduling order preserved."""
from .component import RuntimeComponent


class RequestLifecycle(RuntimeComponent):
    owned_fields = frozenset(['request_counts', 'requests'])

    def fail_request(self, row, reason):
        row['state'] = 'failed'
        self.request_counts['failed_count'] += 1
        self.request_counts['incomplete_count'] -= 1
        self.recorder.request_failed(row['request_id'], self.backend.now_seconds(), reason)

    def arrive(self, rid):
        row = self.requests[rid]
        row["arrived_at_seconds"] = self.backend.now_seconds()
        self.request_counts["arrived_count"] += 1
        self.request_counts["incomplete_count"] += 1
        self.recorder.request_arrived(rid, self.backend.now_seconds(), row.get("ttft_slo_seconds"),
                                      row.get("itl_slo_seconds"), row["output_tokens"])
        row["state"] = "queued_prefill"
        row["queued_at_seconds"] = self.backend.now_seconds()
        if self.binding_window or self.binding_model or self.config.binding.decision_overhead_us:
            row["state"] = "pending_binding"
            self.emit("binding_queued", request_id=rid)
            if self.binding_window:
                slo = row.get("ttft_slo_seconds", self.config.evaluation.default_ttft_slo_seconds)
                if self.joint_enabled and not self.config.binding.window_enabled:
                    self.capture_binding(force=True)
                    if rid not in self.bind_window((rid,), "arrival"):
                        self.binding_window.add_request(rid, row["arrived_at_seconds"]+slo, deferred=True)
                else:
                    self.binding_window.add_request(rid, row["arrived_at_seconds"]+slo)
            else:
                self.bind_immediate(row)
            return
        self.enqueue_bound(row, row["p"], row["d"])

    def enqueue_bound(self, row, prefill, decode, kick=True):
        rid = row["request_id"]
        self.binding_active.add(rid)
        row.update(p=prefill, d=decode, state="queued_prefill", queued_at_seconds=self.backend.now_seconds())
        if any(obj.total_size_bytes > self.ledger.capacities[obj.memory_domain_id] for obj in self._objects(row)):
            self.fail_request(row, "request exceeds GPU KV capacity even when run alone")
            self.capture("request_failed")
            return
        self.queues[row["p"]].append(rid)
        row["bound_at_seconds"] = self.backend.now_seconds()
        if self.binding_model:
            self.binding_known[rid] = dict(self._request_spec(row), p=prefill, d=decode, state="queued_prefill",
                token_times=[], committed_at_seconds=self.backend.now_seconds(), resources_committed=False,
                reservation_objects=tuple(self._objects(row)) if row.get("pre_reserved") else (),
                controller_sequence=self.binding_commit_sequence,
                **(dict(prefill_hit_tokens=row["prefix_plan"].hit_tokens("p"), decode_hit_tokens=row["prefix_plan"].hit_tokens("d"))
                   if row.get("pre_reserved") and "prefix_plan" in row else {}))
        details = (dict(binding_wait_seconds=self.backend.now_seconds()-row["arrived_at_seconds"],
                        window_enabled=self.config.binding.window_enabled,
                        decision_overhead_us=self.config.binding.decision_overhead_us)
                   if self.binding_window or self.predictor or self.config.binding.decision_overhead_us else {})
        self.emit("binding_committed", request_id=rid, prefill_instance_id=row["p"], decode_instance_id=row["d"], **details)
        if kick:
            self.kick_prefill(row["p"])

    def finish_request(self, rid):
        row = self.requests[rid]
        row["state"] = "completed"
        self.binding_active.discard(rid)
        self.request_counts["completed_count"] += 1
        self.request_counts["incomplete_count"] -= 1
        self.recorder.request_completed(rid, self.backend.now_seconds())
        self.ledger.release_request(rid)
        if self.scheduler is not None:
            self.scheduler.request_completed(rid)
            if self.mlu_enabled or self.config.kv_scheduler.decode_admission_limit:
                self.request_dispatch()
        self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
        self.kick_all_prefill()
