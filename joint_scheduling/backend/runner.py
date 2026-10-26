"""Profile-driven baseline over live SimAI/ns-3 networking.

This is an explicit simple baseline, not a reproduction of Vidur/Sarathi's
full batching engine. Endpoint selection is round-robin; prefill is FIFO;
decode runs one request-token step at a time in round-robin order.
"""

import hashlib
import json
import math
from collections import deque
from dataclasses import asdict
from pathlib import Path

from .build import require_backend
from .client import Ns3Backend
from .runtime import ComputeProfile, load_workload
from ..evaluation import TraceRecorder, analyze_trace
from ..evaluation.export import export_result
from ..feedback import NetworkTransfer
from ..reservations import PhysicalObjectReservation
from ..reservations.ledger import CapacityLedger
from ..observation import StateObserver


class _Baseline:
    def __init__(self, config, backend, recorder, profile, requests, observer):
        self.config, self.backend, self.recorder, self.profile = config, backend, recorder, profile
        self.observer = observer
        self.compute_active = set()
        self.request_counts = {"arrived_count": 0, "completed_count": 0, "failed_count": 0, "incomplete_count": 0}
        self.instances = {placement.instance_id: placement for placement in config.topology.placements}
        self.prefills = [p.instance_id for p in config.topology.placements if p.role == "prefill"]
        self.decodes = [p.instance_id for p in config.topology.placements if p.role == "decode"]
        self.queues = {key: deque() for key in self.instances}
        self.busy = {key: False for key in self.instances}
        capacity = int(config.topology.gpu_memory_gib * 2**30 * (1 - config.reservations.capacity_safety_fraction)) - profile.weights_bytes_per_gpu
        if capacity < 0:
            raise ValueError("model weights exceed usable GPU capacity")
        self.ledger = CapacityLedger({"gpu-{}".format(gpu): capacity for gpu in range(config.topology.num_gpus)})
        self.requests = {}
        self.transfer_counter = 0
        for index, item in enumerate(requests):
            row = dict(item, p=self.prefills[index % len(self.prefills)], d=self.decodes[index % len(self.decodes)],
                       generated=0, state="not_arrived")
            p, d = self.instances[row["p"]], self.instances[row["d"]]
            if len(p.gpu_ids) != len(d.gpu_ids):
                raise ValueError("this baseline requires equal P/D TP sizes; KV resharding is not implemented")
            profile.lookup("prefill", row["input_tokens"], len(p.gpu_ids))
            if row["output_tokens"] > 1:
                profile.lookup("decode", row["input_tokens"], len(d.gpu_ids))
                profile.lookup("decode", row["input_tokens"] + row["output_tokens"] - 2, len(d.gpu_ids))
            self.requests[row["request_id"]] = row

    def emit(self, event, **data):
        self.recorder.record_event(event, self.backend.now_seconds(), **data)
        self.capture(event)

    def capture(self, reason):
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
        self.emit("queue_dequeued", request_id=row["request_id"], instance_id=instance, queue_wait_seconds=wait)

    def _objects(self, row):
        rid = row["request_id"]
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        source_size = math.ceil(row["input_tokens"] * self.profile.kv_bytes_per_token / len(p.gpu_ids))
        target_size = math.ceil((row["input_tokens"] + row["output_tokens"]) * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        objects = [PhysicalObjectReservation("p:" + rid, "gpu-{}".format(gpu), source_size, (rid,)) for gpu in p.gpu_ids]
        if row["output_tokens"] > 1:
            objects += [PhysicalObjectReservation("d:" + rid, "gpu-{}".format(gpu), target_size, (rid,)) for gpu in d.gpu_ids]
        return objects

    def arrive(self, rid):
        row = self.requests[rid]
        self.request_counts["arrived_count"] += 1
        self.request_counts["incomplete_count"] += 1
        self.recorder.request_arrived(rid, self.backend.now_seconds(), row.get("ttft_slo_seconds"),
                                      row.get("itl_slo_seconds"), row["output_tokens"])
        row["state"] = "queued_prefill"
        row["queued_at_seconds"] = self.backend.now_seconds()
        if any(obj.total_size_bytes > self.ledger.capacities[obj.memory_domain_id] for obj in self._objects(row)):
            row["state"] = "failed"
            self.request_counts["failed_count"] += 1
            self.request_counts["incomplete_count"] -= 1
            self.recorder.request_failed(rid, self.backend.now_seconds(), "request exceeds GPU KV capacity even when run alone")
            self.capture("request_failed")
            return
        self.queues[row["p"]].append(rid)
        self.emit("binding_committed", request_id=rid, prefill_instance_id=row["p"], decode_instance_id=row["d"])
        self.kick_prefill(row["p"])

    def kick_prefill(self, instance):
        if self.busy[instance] or not self.queues[instance]:
            return
        rid = self.queues[instance][0]
        row = self.requests[rid]
        objects = self._objects(row)
        if not self.ledger.try_commit(rid, objects):
            return
        self.queues[instance].popleft()
        self.busy[instance] = True
        row["state"] = "prefill"
        self.queue_started(instance, row)
        for obj in objects:
            if obj.object_id.startswith("p:"):
                self.ledger.materialize(obj.memory_domain_id, obj.object_id, obj.total_size_bytes)
        self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
        self.execute_step(row, "prefill", row["input_tokens"], lambda: self.prefill_done(rid))

    def execute_step(self, row, phase, tokens, complete):
        instance = self.instances[row["p"] if phase == "prefill" else row["d"]]
        sample = self.profile.lookup(phase, tokens, len(instance.gpu_ids))
        remaining = sample["collectives_per_step"]
        compute_part = sample["compute_seconds"] / max(1, remaining)
        self.emit("compute_step_started", request_id=row["request_id"], phase=phase, instance_id=instance.instance_id,
                  tokens=tokens, modeled_compute_seconds=sample["compute_seconds"])

        def segment(index):
            def compute_finished():
                if instance.instance_id in self.compute_active:
                    self.compute_active.remove(instance.instance_id)
                    self.emit("compute_segment_completed", request_id=row["request_id"], instance_id=instance.instance_id)
                if index < remaining:
                    self.allreduce(row["request_id"], instance.gpu_ids, sample["collective_bytes"],
                                   lambda: segment(index + 1))
                else:
                    self.emit("compute_step_completed", request_id=row["request_id"], phase=phase, instance_id=instance.instance_id)
                    complete()
            if index < remaining or remaining == 0:
                self.compute_active.add(instance.instance_id)
                self.emit("compute_segment_started", request_id=row["request_id"], instance_id=instance.instance_id,
                          modeled_compute_seconds=compute_part)
                self.backend.call_at(self.backend.now_seconds() + compute_part, compute_finished)
            else:
                compute_finished()
        segment(0)

    def transfer(self, rid, kind, src, dst, size, complete):
        self.transfer_counter += 1
        transfer = NetworkTransfer("transfer-{}".format(self.transfer_counter), kind, src, dst, size)
        local = src // self.config.topology.gpus_per_node == dst // self.config.topology.gpus_per_node
        self.observer.transfer_started(transfer, "local" if local else "inter_node")
        self.emit("transfer_started", request_id=rid, transfer_id=transfer.transfer_id, transfer_kind=kind,
                  source_gpu_id=src, destination_gpu_id=dst, size_bytes=size)
        def finished(progress):
            self.observer.transfer_completed(transfer.transfer_id)
            self.emit("transfer_completed", request_id=rid, transfer_id=transfer.transfer_id, transfer_kind=kind,
                      source_reads_complete=progress.source_reads_complete, destination_ready=progress.destination_ready)
            complete()
        self.backend.submit(transfer, finished)

    def allreduce(self, rid, ranks, size, complete):
        if len(ranks) == 1:
            complete()
            return
        rounds = 2 * (len(ranks) - 1)
        chunk = math.ceil(size / len(ranks))
        def step(index):
            if index == rounds:
                complete()
                return
            pending = [len(ranks)]
            def done():
                pending[0] -= 1
                if pending[0] == 0:
                    step(index + 1)
            for rank, src in enumerate(ranks):
                self.transfer(rid, "collective", src, ranks[(rank + 1) % len(ranks)], chunk, done)
        step(0)

    def prefill_done(self, rid):
        row = self.requests[rid]
        row["generated"] = 1
        self.busy[row["p"]] = False
        self.recorder.token_returned(rid, self.backend.now_seconds(), 1)
        if row["output_tokens"] == 1:
            self.finish_request(rid)
            return
        row["state"] = "kv_transfer"
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        for gpu in p.gpu_ids:
            domain = "gpu-{}".format(gpu)
            self.ledger.pin(domain, "p:" + rid)
            self.ledger.release_reference(domain, "p:" + rid, rid)
        pending = [len(p.gpu_ids)]
        def received():
            pending[0] -= 1
            if pending[0]:
                return
            for gpu in p.gpu_ids:
                self.ledger.unpin("gpu-{}".format(gpu), "p:" + rid)
            amount = math.ceil(row["input_tokens"] * self.profile.kv_bytes_per_token / len(d.gpu_ids))
            for gpu in d.gpu_ids:
                self.ledger.materialize("gpu-{}".format(gpu), "d:" + rid, amount)
            row["state"] = "queued_decode"
            row["queued_at_seconds"] = self.backend.now_seconds()
            self.queues[row["d"]].append(rid)
            self.emit("kv_ready", request_id=rid)
            self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
            self.kick_decode(row["d"])
            self.kick_all_prefill()
        total = row["input_tokens"] * self.profile.kv_bytes_per_token
        base, extra = divmod(total, len(p.gpu_ids))
        for rank, (src, dst) in enumerate(zip(p.gpu_ids, d.gpu_ids)):
            amount = base + int(rank < extra)
            if amount:
                self.transfer(rid, "kv", src, dst, amount, received)
            else:
                received()
        self.kick_prefill(row["p"])

    def kick_all_prefill(self):
        for instance in self.prefills:
            self.kick_prefill(instance)

    def kick_decode(self, instance):
        if self.busy[instance] or not self.queues[instance]:
            return
        rid = self.queues[instance].popleft()
        row = self.requests[rid]
        row["state"] = "decode"
        self.busy[instance] = True
        self.queue_started(instance, row)
        self.execute_step(row, "decode", row["input_tokens"] + row["generated"] - 1,
                          lambda: self.decode_done(rid))

    def decode_done(self, rid):
        row = self.requests[rid]
        row["generated"] += 1
        d = self.instances[row["d"]]
        previous_size = math.ceil((row["input_tokens"] + row["generated"] - 2) * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        current_size = math.ceil((row["input_tokens"] + row["generated"] - 1) * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        for gpu in d.gpu_ids:
            self.ledger.materialize("gpu-{}".format(gpu), "d:" + rid, current_size - previous_size)
        self.recorder.token_returned(rid, self.backend.now_seconds(), row["generated"])
        self.busy[row["d"]] = False
        if row["generated"] == row["output_tokens"]:
            self.finish_request(rid)
        else:
            row["state"] = "queued_decode"
            row["queued_at_seconds"] = self.backend.now_seconds()
            self.queues[row["d"]].append(rid)
            self.emit("decode_requeued", request_id=rid, instance_id=row["d"])
        self.kick_decode(row["d"])

    def finish_request(self, rid):
        row = self.requests[rid]
        row["state"] = "completed"
        self.request_counts["completed_count"] += 1
        self.request_counts["incomplete_count"] -= 1
        self.recorder.request_completed(rid, self.backend.now_seconds())
        self.ledger.release_request(rid)
        self.emit("resources", request_id=rid, domains=self.ledger.snapshot())
        self.kick_all_prefill()


def run_baseline(config, output_dir, workload_path=None, profile_path=None, make_plots=False):
    require_backend()
    if profile_path is None and not config.feedback.compute_profile_path:
        raise ValueError("set feedback.compute_profile_path or pass --profile; no synthetic fallback is automatic")
    profile_path = Path(profile_path) if profile_path is not None else config.profile_path()
    if workload_path is None:
        if not config.runtime.workload_path:
            raise ValueError("set runtime.workload_path or pass --workload")
        workload_path = Path(config.runtime.workload_path)
        if not workload_path.is_absolute():
            workload_path = config.config_path.parent / workload_path
    requests, workload_provenance = load_workload(workload_path)
    profile = ComputeProfile(profile_path)
    if config.runtime.time_limit_seconds < config.evaluation.window_end_seconds:
        raise ValueError("runtime time limit must cover evaluation.window_end_seconds")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    profile_copy = output / "compute_profile.json"
    profile_copy.write_bytes(profile.path.read_bytes())
    (output / "workload.json").write_bytes(Path(workload_path).read_bytes())
    snapshot = asdict(config)
    snapshot["config_path"] = str(config.config_path)
    snapshot["effective_baseline"] = {"endpoint_selection": "round_robin", "prefill": "fifo",
                                      "decode": "one_request_token_step_round_robin", "kv": "whole_ready_immediate",
                                      "collectives": "gpu_rank_ring_allreduce", "native_vidur_batching": False}
    (output / "resolved_config.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    with Ns3Backend(config.topology, output / "network", config.runtime.seed,
                    config.runtime.congestion_control, config.runtime.packet_payload_bytes,
                    config.runtime.switch_buffer_bytes, config.runtime.rpc_timeout_seconds) as backend:
        metadata = {"synthetic": profile.provenance["synthetic"] or workload_provenance["synthetic"],
                    "backend": backend.metadata, "compute_profile_provenance": profile.provenance,
                    "workload_provenance": workload_provenance, "effective_baseline": snapshot["effective_baseline"],
                    "compute_profile_sha256": hashlib.sha256(profile_copy.read_bytes()).hexdigest()}
        with TraceRecorder(output / "events.jsonl", "round_robin_fifo_ns3", config.runtime.seed, metadata) as recorder, \
                StateObserver(output / "observation", backend, config.feedback.observation_delay_us) as observer:
            baseline = _Baseline(config, backend, recorder, profile, requests, observer)
            baseline.capture("initial")
            for row in requests:
                if row["arrival_seconds"] <= config.runtime.time_limit_seconds:
                    backend.call_at(row["arrival_seconds"], lambda rid=row["request_id"]: baseline.arrive(rid))
            backend.run_until(config.runtime.time_limit_seconds)
            baseline.capture("simulation_end")
            observer.finish()
            recorder.finish(backend.now_seconds())
            (output / "final_resources.json").write_text(json.dumps(baseline.ledger.snapshot(), indent=2) + "\n")
    result = analyze_trace(output / "events.jsonl", config.evaluation)
    export_result(result, output / "result", snapshot)
    if make_plots:
        from ..evaluation.plots import plot_results
        plot_results([output / "result"], output / "figures", config.evaluation)
        from ..observation.plots import plot_observation
        plot_observation(output / "observation")
    return result
