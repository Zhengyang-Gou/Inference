"""Track modeled execution and communication, without advancing simulation time."""

import copy
import csv
import json
import math
from collections import deque
from pathlib import Path

from ..evaluation.pressure import PressureStateAccumulator


CSV_FIELDS = ("time_seconds", "visible_at_seconds", "arrived_count", "completed_count", "failed_count",
              "incomplete_count", "prefill_queue_length", "decode_queue_length", "compute_busy_instances",
              "execution_busy_instances", "kv_inflight_count", "kv_inflight_bytes", "collective_inflight_count",
              "collective_inflight_bytes", "occupied_bytes", "reserved_bytes", "available_bytes")


def _json(path, payload):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


class StateObserver:
    """Publish immutable snapshots, visible only after observation_delay_us.

    latest_observed() reads the supplied simulation clock; callers cannot query
    an arbitrary future timestamp. Pending snapshots are released lazily, so
    monitoring adds no ns-3 events and does not change baseline execution.
    In-flight bytes are whole submitted logical messages awaiting completion,
    including local messages; they are not remaining wire bytes or NIC counters.
    """

    def __init__(self, output, clock, observation_delay_us=0.0, detailed=True, measurement_window=None):
        if (type(observation_delay_us) not in (int, float) or not math.isfinite(observation_delay_us)
                or not 0 <= observation_delay_us * 1000 < 2**63):
            raise ValueError("observation delay must fit the nonnegative nanosecond clock")
        self.output, self.clock = Path(output), clock
        self.output.mkdir(parents=True, exist_ok=False)
        self.delay_ns = round(observation_delay_us * 1000)
        self._trace = (self.output / "state.jsonl").open("x", encoding="utf-8") if detailed else None
        self._csv_handle = (self.output / "state.csv").open("x", encoding="utf-8", newline="") if detailed else None
        self._csv = csv.DictWriter(self._csv_handle, fieldnames=CSV_FIELDS) if detailed else None
        if self._csv:
            self._csv.writeheader()
        self.measurement = PressureStateAccumulator(*measurement_window) if measurement_window is not None else None
        self.pending, self.visible, self.previous = deque(), None, None
        self.sequence = 0
        self.active = {}
        self.communication = {kind: {"started_count": 0, "completed_count": 0, "started_bytes": 0,
                                    "completed_bytes": 0, "peak_inflight_count": 0,
                                    "peak_inflight_bytes": 0, "inflight_byte_seconds": 0.0}
                              for kind in ("kv", "collective")}
        self.instances, self.memory = {}, {}

    def queue_started(self, instance, wait_seconds):
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise ValueError("queue wait must be finite and nonnegative")
        row = self._instance(instance)
        row["queue_starts"] += 1
        row["queue_wait_seconds"] += wait_seconds
        row["max_queue_wait_seconds"] = max(row["max_queue_wait_seconds"], wait_seconds)

    def _instance(self, instance):
        return self.instances.setdefault(instance, {"compute_busy_seconds": 0.0, "execution_busy_seconds": 0.0,
            "queue_length_seconds": 0.0, "max_queue_length": 0, "queue_starts": 0,
            "queue_wait_seconds": 0.0, "max_queue_wait_seconds": 0.0})

    def transfer_started(self, transfer, scope):
        if transfer.transfer_id in self.active:
            raise ValueError("duplicate observed transfer")
        self.active[transfer.transfer_id] = {"transfer_id": transfer.transfer_id, "kind": transfer.kind,
            "source_gpu_id": transfer.source_gpu_id, "destination_gpu_id": transfer.destination_gpu_id,
            "size_bytes": transfer.size_bytes, "scope": scope, "started_at_seconds": self.clock.now_seconds()}
        row = self.communication[transfer.kind]
        row["started_count"] += 1
        row["started_bytes"] += transfer.size_bytes

    def transfer_completed(self, transfer_id):
        transfer = self.active.pop(transfer_id)
        row = self.communication[transfer["kind"]]
        row["completed_count"] += 1
        row["completed_bytes"] += transfer["size_bytes"]

    def _integrate(self, time):
        if self.previous is None:
            return
        span = time - self.previous["time_seconds"]
        if span < 0:
            raise ValueError("observation clock moved backwards")
        for instance, state in self.previous["instances"].items():
            row = self._instance(instance)
            row["compute_busy_seconds"] += span * int(state["compute_busy"])
            row["execution_busy_seconds"] += span * int(state["execution_busy"])
            row["queue_length_seconds"] += span * state["queue_length"]
        for kind in self.communication:
            self.communication[kind]["inflight_byte_seconds"] += span * self.previous["communication"][kind]["inflight_bytes"]

    def _release(self, now_ns):
        while self.pending and self.pending[0][0] <= now_ns:
            _, self.visible = self.pending.popleft()

    def latest_observed(self):
        self._release(round(self.clock.now_seconds() * 1e9))
        return copy.deepcopy(self.visible)

    def capture(self, request_counts, instances, resources, reason):
        time = self.clock.now_seconds()
        self._integrate(time)
        communication = {}
        for kind, totals in self.communication.items():
            active = [item for item in self.active.values() if item["kind"] == kind]
            count, size = len(active), sum(item["size_bytes"] for item in active)
            totals["peak_inflight_count"] = max(totals["peak_inflight_count"], count)
            totals["peak_inflight_bytes"] = max(totals["peak_inflight_bytes"], size)
            communication[kind] = dict(totals, inflight_count=count, inflight_bytes=size)
        for instance, state in instances.items():
            row = self._instance(instance)
            row["max_queue_length"] = max(row["max_queue_length"], state["queue_length"])
        for domain, state in resources.items():
            peaks = self.memory.setdefault(domain, {"peak_occupied_bytes": 0, "peak_reserved_bytes": 0,
                                                    "peak_committed_bytes": 0})
            peaks["peak_occupied_bytes"] = max(peaks["peak_occupied_bytes"], state["occupied_bytes"])
            peaks["peak_reserved_bytes"] = max(peaks["peak_reserved_bytes"], state["reserved_bytes"])
            peaks["peak_committed_bytes"] = max(peaks["peak_committed_bytes"], state["occupied_bytes"] + state["reserved_bytes"])
        snapshot = copy.deepcopy({"schema_version": 1, "sequence": self.sequence, "reason": reason,
            "time_seconds": time, "visible_at_seconds": (round(time * 1e9) + self.delay_ns) / 1e9,
            "requests": request_counts, "instances": instances, "resources": resources,
            "communication": communication, "active_transfers": sorted(self.active.values(), key=lambda item: item["transfer_id"])})
        self.sequence += 1
        if self._trace:
            self._trace.write(json.dumps(snapshot, ensure_ascii=False, allow_nan=False) + "\n")
        row = {"time_seconds": time, "visible_at_seconds": snapshot["visible_at_seconds"], **request_counts}
        row.update(prefill_queue_length=sum(s["queue_length"] for s in instances.values() if s["role"] == "prefill"),
                   decode_queue_length=sum(s["queue_length"] for s in instances.values() if s["role"] == "decode"),
                   compute_busy_instances=sum(s["compute_busy"] for s in instances.values()),
                   execution_busy_instances=sum(s["execution_busy"] for s in instances.values()))
        for kind in communication:
            for key in ("count", "bytes"):
                row[kind + "_inflight_" + key] = communication[kind]["inflight_" + key]
        for key in ("occupied_bytes", "reserved_bytes", "available_bytes"):
            row[key] = sum(state[key] for state in resources.values())
        if self._csv:
            self._csv.writerow(row)
        if self.measurement:
            self.measurement.add(row)
        self.previous = snapshot
        self.pending.append((round(time * 1e9) + self.delay_ns, snapshot))
        self._release(round(time * 1e9))

    def finish(self):
        time = self.clock.now_seconds()
        self._integrate(time)
        summaries = {}
        for instance, state in self.instances.items():
            summaries[instance] = dict(state,
                compute_utilization=state["compute_busy_seconds"] / time if time else 0.0,
                execution_utilization=state["execution_busy_seconds"] / time if time else 0.0,
                mean_queue_length=state["queue_length_seconds"] / time if time else 0.0,
                mean_queue_wait_seconds=state["queue_wait_seconds"] / state["queue_starts"] if state["queue_starts"] else None)
        report = {"schema_version": 1, "observed_until_seconds": time, "snapshot_count": self.sequence,
                  "observation_delay_seconds": self.delay_ns / 1e9, "instances": summaries,
                  "communication": self.communication, "memory": self.memory,
                  "requests": self.previous["requests"] if self.previous else {},
                  "semantics": {"compute_utilization": "modeled compute busy time / full simulation horizon",
                    "execution_utilization": "instance step busy time, including its collective waits / horizon",
                    "inflight_bytes": "whole logical messages awaiting ACK completion; includes local traffic",
                    "queue_wait": "completed queue waits per prefill request or decode token step; pending waits remain in state",
                    "network_telemetry": "application transfers only; no per-link occupancy or measured NIC utilization"}}
        if self.measurement:
            report["measurement_window"] = dict(start_seconds=self.measurement.start, end_seconds=self.measurement.end,
                                               **self.measurement.finish())
        _json(self.output / "summary.json", report)
        if self._trace:
            _json(self.output / "observed_state.json", {"queried_at_seconds": time, "state": self.latest_observed()})
        if summaries:
            with (self.output / "instances.csv").open("x", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["instance_id"] + list(next(iter(summaries.values()))))
                writer.writeheader()
                writer.writerows(dict(instance_id=key, **value) for key, value in sorted(summaries.items()))
        for handle in (self._trace, self._csv_handle):
            if handle:
                handle.flush()
        return report

    def close(self):
        for handle in (self._trace, self._csv_handle):
            if handle:
                handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DisabledStateObserver:
    """Keep the output location for independent telemetry, without state logs."""

    def __init__(self, output, clock, observation_delay_us=0.0, detailed=True, measurement_window=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)

    def capture(self, *args, **kwargs):
        pass

    def queue_started(self, *args):
        pass

    def transfer_started(self, *args):
        pass

    def transfer_completed(self, *args):
        pass

    def finish(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass
