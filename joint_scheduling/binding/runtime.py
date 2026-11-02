"""Window dispatch, delayed snapshots, bounded joint selection and diagnostics."""

import copy
import json
import time
from dataclasses import replace
from itertools import islice
from collections import deque
from dataclasses import asdict

from . import Binding
from .predictor import WholePlanPredictor
from .window import BindingWindow
from .fast import FastPlanPredictor, IncrementalJointPolicy
from .static import endpoint_policy


class BindingRuntime:
    def init_binding(self):
        self.joint_enabled = (self.config.binding.joint_selection_enabled and
                              self.config.binding.policy == "joint_lexicographic")
        self.binding_active = set()
        self.binding_rounds, self.binding_wall = [], []
        self.binding_snapshot_wall = []
        self.binding_snapshot_ns = 0
        self.binding_commit_sequence = 0
        self.binding_cursor = 0
        self.binding_known = {}
        self.prediction_history, self.prediction_visible = deque(), None
        self.prediction_records = []
        self.prediction_call_count = 0
        self.predictor = WholePlanPredictor(self.config, self.profile) if self.config.binding.prediction_enabled else None
        self.fast_predictor = FastPlanPredictor(self.config, self.profile) if self.joint_enabled else None
        self.joint_policy = IncrementalJointPolicy(self.config, self.fast_predictor) if self.joint_enabled else None
        self.binding_model = self.fast_predictor or self.predictor
        # Without collection windows, joint selection dispatches at arrival;
        # deferred work still retries at the next regular interval.
        self.binding_window = (BindingWindow(self.config.binding, self.backend, self.bind_window,
            self.config.runtime.time_limit_seconds,
            on_schedule=self.schedule_binding_snapshot if self.joint_enabled else None)
            if self.config.binding.window_enabled or self.joint_enabled else None)

    def _request_spec(self, row):
        result = dict(input_tokens=row["input_tokens"], arrival_seconds=row["arrived_at_seconds"],
                      max_output_tokens=row.get("max_output_tokens", self.config.reservations.reserve_max_output_tokens),
                      prefix_blocks=copy.deepcopy(row.get("prefix_blocks", [])))
        for key in ("ttft_slo_seconds", "itl_slo_seconds"):
            if key in row:
                result[key] = row[key]
        return result

    def schedule_binding_snapshot(self, at, generation):
        # Collect one coherent sample before the decision, then publish it only
        # after the configured delay. Avoid copying all state on every token,
        # layer or network event. Superseded timers do no work.
        when = max(self.backend.now_seconds(), at-self.config.feedback.observation_delay_us/1e6)
        def sample():
            if self.binding_window.generation == generation:
                self.capture_binding(force=True)
        self.backend.call_at(when, sample)

    def capture_binding(self, force=False):
        if not self.binding_model or self.joint_enabled and not force:
            return
        started = time.perf_counter_ns()
        now = self.backend.now_seconds()
        # Bundle the latest collected network sample with the delayed state.
        # It becomes visible no later than this bundle, avoiding a second delay.
        network = (getattr(self.network_monitor, "previous", None) or self.network_monitor.latest_observed()
                   if self.network_monitor else None)
        rows = {}
        overflow = self.joint_enabled and (len(self.binding_active) > self.config.binding.max_state_requests or
                                          len(self.ledger.objects) > self.config.binding.max_state_objects)
        active = () if overflow else sorted(self.binding_active)
        for rid in active:
            row = self.requests[rid]
            if row["state"] in ("not_arrived", "completed", "failed"):
                continue
            spec = self._request_spec(row)
            plan = row.get("prefix_plan")
            p, d = row["p"], row["d"]
            active_instance = p if row["state"] == "prefill" else d
            received = []
            if d:
                for rank, gpu in enumerate(self.instances[d].gpu_ids):
                    cached = sum(objects[rank].total_size_bytes for objects in
                                 plan.by_role["d"][:len(plan.hits["d"])]) if plan and plan.by_role["d"] else 0
                    received.append(max(0, row["kv_received_by_gpu"].get(gpu, 0)-cached))
            spec.update(state=row["state"], p=p, d=d, token_times=list(row["token_times"]),
                prefill_finished=row["prefill_finished"], prefill_layer_index=row.get("prefill_layer_index", 0),
                ready_layers=row["ready_layers"], segment_index=row.get("segment_index", 0),
                compute_active=active_instance in self.compute_active,
                compute_remaining_seconds=max(0, row.get("compute_segment_end", now)-now),
                received_transfer_bytes_by_rank=received, resources_committed=rid in self.ledger.plans)
            if plan:
                spec.update(prefill_hit_tokens=plan.hit_tokens("p"), decode_hit_tokens=plan.hit_tokens("d"))
            if self.config.runtime.prefill_chunk_tokens:
                chunk = (row["prefill_chunks"][row["prefill_chunk_index"]]
                         if row["prefill_chunks"] and row["state"] == "prefill" else None)
                batch = self.active_prefill_batches.get(p)
                if batch and row["state"] == "prefill":
                    chunk = next(m for m in batch["members"] if m["request_id"] == rid)
                spec.update(prefill_computed_tokens=row["prefill_computed_tokens"],
                            prefill_started=row.get("prefill_started", False),
                            prefill_admission_sequence=row.get("prefill_admission_sequence", 0),
                            active_prefill_chunk=(dict(chunk_tokens=chunk["chunk_tokens"],
                                                       cached_tokens=chunk["cached_tokens"]) if chunk else None))
            rows[rid] = spec
        state = dict(overflow=overflow, controller_sequence=self.binding_commit_sequence, now_seconds=now, observed_at_seconds=now,
            visible_at_seconds=now+self.config.feedback.observation_delay_us/1e6,
            requests=rows, instances={key: dict(queue=list(self.queues[key]),
                active_request_id=self.active_by_instance.get(key),
                active_request_ids=(tuple(m["request_id"] for m in self.active_prefill_batches[key]["members"])
                    if key in self.active_prefill_batches else self.active_decode_batches.get(key,
                        (self.active_by_instance[key],) if key in self.active_by_instance else ())))
                    for key in self.instances},
            network=network, ledger=self.ledger,
            cache_entries=self.prefix_cache.entries if self.prefix_cache else {})
        if self.joint_enabled:
            # Detached objects and entries; trial queries never touch live LRU,
            # pins or references. Historical reservation signatures are unused.
            state["ledger"] = self.ledger.clone() if not overflow else self.fast_predictor.empty_state()["ledger"]
            state["cache_entries"] = {k: replace(v) for k, v in state["cache_entries"].items()} if not overflow else {}
            if self.prediction_history and self.prediction_history[-1]["observed_at_seconds"] == now:
                self.prediction_history[-1] = state
            else:
                self.prediction_history.append(state)
        else:
            self.prediction_history.append(copy.deepcopy(state))
        elapsed = time.perf_counter_ns()-started
        self.binding_snapshot_ns += elapsed
        if self.config.binding.wall_clock_diagnostics:
            self.binding_snapshot_wall.append(elapsed/1000)
        self._release_predictions()

    def _release_predictions(self):
        now = self.backend.now_seconds()
        while self.prediction_history and self.prediction_history[0]["visible_at_seconds"] <= now+1e-12:
            self.prediction_visible = self.prediction_history.popleft()
        if self.joint_enabled and self.prediction_visible:
            sampled_seq = self.prediction_visible.get("controller_sequence", 0)
            self.binding_known = {rid: known for rid, known in self.binding_known.items()
                if rid in self.prediction_visible["requests"] or known.get("controller_sequence", 0) > sampled_seq}

    def prediction_state(self, ids):
        self._release_predictions()
        now = self.backend.now_seconds()
        if self.prediction_visible:
            state = (dict(self.prediction_visible, requests=dict(self.prediction_visible["requests"]))
                     if self.joint_enabled else copy.deepcopy(self.prediction_visible))
        else:
            state = self.binding_model.empty_state(now)
        state["now_seconds"] = now
        # Dispatches are controller-owned facts even before telemetry arrives.
        # Replay these known commitments from their dispatch time, not from
        # hidden current execution progress or future completion timestamps.
        for rid, known in self.binding_known.items():
            previous = state["requests"].get(rid)
            newer = (known.get("controller_sequence", 0) > state.get("controller_sequence", 0) if self.joint_enabled else
                     known["committed_at_seconds"] > state["observed_at_seconds"])
            if newer and (previous is None or
                    not previous.get("p") or previous.get("state") in ("pending_binding", "binding_decision")):
                state["requests"][rid] = copy.deepcopy(known)
        for rid in ids:
            state["requests"][rid] = dict(self._request_spec(self.requests[rid]), state="pending_binding", p=None, d=None)
        return state

    def record_binding_prediction(self, bindings):
        if not self.predictor:
            return
        predictions = self.predictor.predict(bindings, self.prediction_state([b.request_id for b in bindings]))
        self.prediction_call_count += 1
        payload = dict(plan=[asdict(b) for b in bindings], predictions={rid: asdict(p) for rid, p in predictions.items()},
                       **self.predictor.last_report)
        self.recorder.record_event("binding_prediction", self.backend.now_seconds(), **payload)
        for b in bindings:
            self.prediction_records.append((b.request_id, predictions[b.request_id]))

    def bind_window(self, ids, reason):
        started = time.perf_counter_ns()
        self.binding_phase = {}
        snapshot_ns, self.binding_snapshot_ns = self.binding_snapshot_ns, 0
        if self.joint_enabled:
            handled = self.bind_joint(ids, reason)
        else:
            handled = self.bind_round_robin(ids, reason)
        self.binding_snapshot_ns = 0  # captures inside this callback are already in round_us
        if self.config.binding.wall_clock_diagnostics:
            wall = (time.perf_counter_ns()-started)/1000
            self.binding_wall.append(dict(round_us=wall, snapshot_capture_us=snapshot_ns/1000,
                                          inclusive_us=wall+snapshot_ns/1000, **self.binding_phase))
        return handled

    def bind_joint(self, ids, reason):
        now = self.backend.now_seconds()
        cfg = self.config.binding
        self.emit("binding_window_triggered", request_ids=list(ids), reason=reason)
        if now+cfg.decision_overhead_us/1e6 > self.config.runtime.time_limit_seconds:
            return ()
        self._release_predictions()
        observed_count = len(self.prediction_visible["requests"]) if self.prediction_visible else 0
        scan = tuple(islice(ids, min(cfg.max_pending_scan, max(0, cfg.max_state_requests-observed_count))))
        phase = time.perf_counter_ns()
        state = self.prediction_state(scan)
        self.binding_phase["state_processing_us"] = (time.perf_counter_ns()-phase)/1000
        phase = time.perf_counter_ns()
        decision = self.joint_policy.select(scan, state)
        self.binding_phase["selection_us"] = (time.perf_counter_ns()-phase)/1000
        bindings = decision.bindings
        report = dict(self.joint_policy.last_report, observed_at_seconds=state["observed_at_seconds"],
                      visible_at_seconds=state["visible_at_seconds"], modeled_request_ids=sorted(state["requests"]))
        phase = time.perf_counter_ns()
        committed = self.reserve_binding_batch(bindings)
        self.binding_phase["reservation_us"] = (time.perf_counter_ns()-phase)/1000
        report["reservation_failed"] = bool(bindings) and not committed
        report["committed_requests"] = len(bindings) if committed else 0
        self.binding_rounds.append(report)
        self.recorder.record_event("binding_selection", now, **report,
                                  plan=[asdict(b) for b in bindings] if committed else [])
        if not committed:
            return ()
        # Full replay is explicitly opt-in and sampled outside candidate search.
        if cfg.prediction_enabled and cfg.prediction_sample_every and len(self.binding_rounds) % cfg.prediction_sample_every == 0:
            self.record_binding_prediction(bindings)
        at = now+cfg.decision_overhead_us/1e6
        self.binding_commit_sequence += 1
        for b in bindings:
            row = self.requests[b.request_id]
            row["state"] = "binding_decision"
            self.binding_known[b.request_id] = dict(self._request_spec(row), p=b.prefill_instance_id,
                d=b.decode_instance_id, state="queued_prefill", token_times=[], committed_at_seconds=at,
                resources_committed=False, reservation_objects=tuple(row["binding_objects"]),
                controller_sequence=self.binding_commit_sequence,
                **(dict(prefill_hit_tokens=row["prefix_plan"].hit_tokens("p"),
                        decode_hit_tokens=row["prefix_plan"].hit_tokens("d")) if "prefix_plan" in row else {}))
        def apply():
            for b in bindings:
                self.enqueue_bound(self.requests[b.request_id], b.prefill_instance_id, b.decode_instance_id, kick=False)
            self.kick_all_prefill()
        if at > now:
            self.backend.call_at(at, apply)
        else:
            apply()
        return tuple(b.request_id for b in bindings)

    def reserve_binding_batch(self, bindings):
        if not bindings:
            return True
        candidates = [self.joint_policy.last_candidates[b.request_id] for b in bindings]
        # A delayed ready hit may have been evicted since sampling. Never
        # resurrect it or substitute a newly warmed (unobserved) prefix.
        for c in candidates:
            if c.layout:
                for entries in c.layout.hits.values():
                    for entry in entries:
                        live = self.prefix_cache.entries.get((entry.instance_id, entry.key))
                        if live is None or live.objects != entry.objects or any(
                            self.ledger.materialized(o.memory_domain_id, o.object_id) != o.total_size_bytes for o in entry.objects):
                            return False
        if not self.ledger.try_commit_batch((c.binding.request_id, c.objects) for c in candidates):
            return False
        for c in candidates:
            row = self.requests[c.binding.request_id]
            self.binding_active.add(c.binding.request_id)
            row.update(p=c.binding.prefill_instance_id, d=c.binding.decode_instance_id,
                       binding_objects=c.objects, pre_reserved=True, prefill_steps=c.steps)
            row["kv_received_by_gpu"] = {g: 0 for g in self.instances[row["d"]].gpu_ids}
            if c.layout:
                c.layout.hits = {role: tuple(self.prefix_cache.entries[e.instance_id, e.key] for e in entries)
                                 for role, entries in c.layout.hits.items()}
                c.layout.cache, c.layout.row = self.prefix_cache, row
                row["prefix_plan"] = c.layout
        return True

    def bind_round_robin(self, ids, reason):
        now = self.backend.now_seconds()
        delay = self.config.binding.decision_overhead_us/1e6
        self.emit("binding_window_triggered", request_ids=list(ids), reason=reason)
        if now+delay > self.config.runtime.time_limit_seconds:
            return ()
        bindings = []
        for rid in ids:
            index = self.binding_cursor
            self.binding_cursor += 1
            bindings.append(Binding(rid, self.prefills[index % len(self.prefills)], self.decodes[index % len(self.decodes)]))
        self.record_binding_prediction(bindings)
        for binding in bindings:
            if self.predictor:
                self.binding_known[binding.request_id] = dict(self._request_spec(self.requests[binding.request_id]),
                    p=binding.prefill_instance_id, d=binding.decode_instance_id, state="queued_prefill", token_times=[],
                    committed_at_seconds=now+delay, resources_committed=False)
        for rid in ids:
            self.requests[rid]["state"] = "binding_decision"
        def apply():
            for binding in bindings:
                self.enqueue_bound(self.requests[binding.request_id], binding.prefill_instance_id, binding.decode_instance_id)
        if delay:
            self.backend.call_at(now+delay, apply)
        else:
            apply()
        return ids

    def bind_immediate(self, row):
        binding = Binding(row["request_id"], row["p"], row["d"])
        self.record_binding_prediction([binding])
        row["state"] = "binding_decision"
        at = self.backend.now_seconds()+self.config.binding.decision_overhead_us/1e6
        if self.predictor:
            self.binding_known[row["request_id"]] = dict(self._request_spec(row), p=binding.prefill_instance_id,
                d=binding.decode_instance_id, state="queued_prefill", token_times=[], committed_at_seconds=at,
                resources_committed=False)
        if at > self.config.runtime.time_limit_seconds:
            return
        if at == self.backend.now_seconds():
            self.enqueue_bound(row, binding.prefill_instance_id, binding.decode_instance_id)
        else:
            self.backend.call_at(at, lambda: self.enqueue_bound(row, binding.prefill_instance_id, binding.decode_instance_id))

    def finish_binding(self):
        if not (self.binding_window or self.binding_model or self.config.binding.decision_overhead_us):
            return None
        errors = {name: [] for name in ("ttft_seconds", "handoff_seconds", "later_max_itl_seconds")}
        for rid, prediction in self.prediction_records:
            times = self.requests[rid]["token_times"]
            if times:
                actual = times[0]-self.requests[rid]["arrived_at_seconds"]
                if prediction.ttft_seconds is not None:
                    errors["ttft_seconds"].append(abs(prediction.ttft_seconds-actual))
            if len(times) >= 2 and prediction.handoff_seconds is not None:
                errors["handoff_seconds"].append(abs(prediction.handoff_seconds-(times[1]-times[0])))
            horizon = len(prediction.predicted_token_times)
            if horizon >= 3 and len(times) >= horizon and prediction.later_max_itl_seconds is not None:
                actual = max(b-a for a, b in zip(times[1:horizon-1], times[2:horizon]))
                errors["later_max_itl_seconds"].append(abs(prediction.later_max_itl_seconds-actual))
        report = dict(endpoint_selection=endpoint_policy(self.config.binding), joint_optimization=self.joint_enabled,
            window=self.binding_window.snapshot() if self.binding_window else None,
            prediction_calls=self.prediction_call_count, predicted_candidates=len(self.prediction_records),
            prediction_errors={key: dict(samples=len(values), mean_absolute_error_seconds=sum(values)/len(values) if values else None,
                max_absolute_error_seconds=max(values) if values else None) for key, values in errors.items()},
            pending_decisions=[rid for rid, row in self.requests.items() if row["state"] == "binding_decision"],
            reservation_timing="atomic_binding_batch" if self.joint_enabled else "at_prefill_start",
            prediction_use="sampled_diagnostics" if self.joint_enabled else "diagnostics_only",
            rounds=len(self.binding_rounds), candidate_scores=sum(r["candidate_scores"] for r in self.binding_rounds),
            budget_exhaustions=sum(r["budget_exhausted"] for r in self.binding_rounds),
            reservation_failures=sum(r["reservation_failed"] for r in self.binding_rounds),
            finite_prediction_scope=True)
        if self.config.binding.wall_clock_diagnostics:
            def percentiles(values):
                values = sorted(values)
                def at(q):
                    if not values:
                        return None
                    i = (len(values)-1)*q
                    lo, hi = math.floor(i), math.ceil(i)
                    return values[lo]+(values[hi]-values[lo])*(i-lo)
                return dict(samples=len(values), p50_us=at(.5), p95_us=at(.95), p99_us=at(.99))
            import math
            timing = {k: percentiles([r[k] for r in self.binding_wall]) for k in ("round_us", "inclusive_us", "snapshot_capture_us")}
            for key in ("state_processing_us", "selection_us", "reservation_us"):
                timing[key] = percentiles([r[key] for r in self.binding_wall if key in r])
            timing.update(snapshot_event_us=percentiles(self.binding_snapshot_wall),
                          clock="perf_counter_ns", affects_simulation=False,
                          simulated_decision_overhead_us=self.config.binding.decision_overhead_us,
                          initial_target_p99_us=50, target_is_guarantee=False)
            (self.observer.output / "binding_timing.json").write_text(json.dumps(dict(timing, rounds=self.binding_wall), indent=2)+"\n")
        waits = [r["bound_at_seconds"]-r["arrived_at_seconds"] for r in self.requests.values() if "bound_at_seconds" in r]
        report["binding_wait"] = dict(samples=len(waits), mean_seconds=sum(waits)/len(waits) if waits else None,
                                      max_seconds=max(waits) if waits else None)
        (self.observer.output / "binding_summary.json").write_text(json.dumps(report, indent=2)+"\n")
        return report
