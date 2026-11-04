"""Delayed packet/queue telemetry; counters are read on the ns-3 time axis."""

import copy
import json
from collections import deque
from ..topology import local_resources


_RATE_FIELDS = tuple((kind, kind + '_bytes', kind + '_bytes_per_second')
                     for kind in ('tx', 'kv', 'collective', 'other'))


class NetworkObserver:
    def __init__(self, backend, topology, config, output, record_samples=True, sample_sink=None):
        self.backend, self.topology, self.config = backend, topology, config
        self.output = output
        self.handle = (output / "network_samples.jsonl").open("x", encoding="utf-8") if record_samples else None
        self.pending, self.visible, self.previous = deque(), None, None
        self.sequence = 0
        self.ewma = {}
        self.peaks = {}
        self.sample_sink = sample_sink
        self._local_resources = local_resources(topology)
        self._raw_previous = {}
        self._zero_rate_ports = set()
        self.initial = self.sample()

    def sample(self):
        raw = self.backend.network_snapshot()
        now = raw["time_ns"] / 1e9
        raw_ports = {row["resource_id"]: row for row in raw["resources"]}
        for key, row in self._local_resources.items():
            raw_ports.setdefault(key, row)
        span = now - self.previous["time_seconds"] if self.previous is not None else 0
        previous = self.previous['resources'] if self.previous is not None else {}
        alpha = self.config.network_ewma_alpha
        complement = 1 - alpha
        ports, raw_previous, zero_rate_ports = {}, {}, set()
        for key, raw_port in raw_ports.items():
            old = previous.get(key)
            if key in self._zero_rate_ports and raw_port == self._raw_previous.get(key):
                # Unchanged counters with zero EWMA stay exactly zero. Copy the
                # completed row so retained snapshots never share mutable ports.
                ports[key] = dict(old)
                raw_previous[key] = self._raw_previous[key]
                zero_rate_ports.add(key)
                continue
            port = ports[key] = dict(raw_port)
            raw_previous[key] = dict(raw_port)
            rates = self.ewma.setdefault(key, {})
            for kind, counter, rate_field in _RATE_FIELDS:
                instant = 0 if span <= 0 or old is None else (port[counter] - old[counter]) / span
                if instant < 0:
                    raise ValueError("network counter moved backwards")
                rates[kind] = alpha * instant + complement * rates.get(kind, instant)
                port[rate_field] = rates[kind]
            port["utilization"] = port["tx_bytes_per_second"] * 8 / port["capacity_bps"]
            port["pfc_pause_delta"] = port["pfc_pause_count"] - old["pfc_pause_count"] if old else 0
            peak = self.peaks.setdefault(key, dict(queue_bytes=0, utilization=0.0))
            peak["queue_bytes"] = max(peak["queue_bytes"], port["queue_bytes"])
            peak["utilization"] = max(peak["utilization"], port["utilization"])
            if not any(rates.values()) and port['pfc_pause_delta'] == 0:
                zero_rate_ports.add(key)
        self._raw_previous = raw_previous
        self._zero_rate_ports = zero_rate_ports
        state = dict(sequence=self.sequence, time_seconds=now,
                     visible_at_seconds=now + self.config.observation_delay_us / 1e6,
                     resources=ports, flows=raw["flows"])
        self.sequence += 1
        self.previous = state
        self.pending.append(state)
        # Sampling continues while no KV needs dispatch. Retire snapshots whose
        # visibility delay elapsed even when latest_observed() is not queried;
        # the latest eligible snapshot and all future snapshots remain intact.
        self._advance_visibility(now)
        if self.handle:
            self.handle.write(json.dumps(state, allow_nan=False) + "\n")
        if self.sample_sink is not None:
            self.sample_sink(copy.deepcopy(state))
        return state

    def _advance_visibility(self, now):
        while self.pending and self.pending[0]["visible_at_seconds"] <= now + 1e-12:
            self.visible = self.pending.popleft()

    def latest_observed(self):
        self._advance_visibility(self.backend.now_seconds())
        return copy.deepcopy(self.visible)

    def close(self):
        if self.handle:
            self.handle.close()
        report = dict(sample_count=self.sequence, last_sample_seconds=self.previous["time_seconds"],
                      observation_delay_seconds=self.config.observation_delay_us/1e6,
                      resources={key: dict(port, peak_sampled_queue_bytes=self.peaks[key]["queue_bytes"],
                                           peak_sampled_utilization=self.peaks[key]["utilization"])
                                 for key, port in self.previous["resources"].items()})
        (self.output / "network_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False)+"\n")


class StaticNetworkObserver:
    """Known topology capacities only; no runtime queue/ACK/counter sampling."""

    def __init__(self, backend, topology):
        raw = backend.network_snapshot()
        resources = {r["resource_id"]: dict(r) for r in raw["resources"]}
        resources.update(local_resources(topology))
        for row in resources.values():
            row.update(queue_bytes=0, utilization=0.0, pfc_pause_delta=0,
                       collective_bytes_per_second=0.0, other_bytes_per_second=0.0)
        self.initial = dict(sequence=None, time_seconds=0.0, visible_at_seconds=0.0,
                            resources=resources, flows=[])

    def latest_observed(self):
        return copy.deepcopy(self.initial)

    def close(self):
        pass
