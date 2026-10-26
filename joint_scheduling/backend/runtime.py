"""Validated baseline inputs and compute-only profile interpolation."""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..validation import ConfigError, read_settings, require_mapping


@dataclass(frozen=True)
class RuntimeConfig:
    seed: int = field(default=7, metadata={"min": 0, "lt": 2147483647})
    workload_path: str = ""
    time_limit_seconds: float = field(default=10.0, metadata={"gt": 0})
    congestion_control: str = field(default="dcqcn", metadata={"choices": ("dcqcn", "none")})
    packet_payload_bytes: int = field(default=1000, metadata={"min": 64, "lt": 9001})
    switch_buffer_bytes: int = field(default=16777216, metadata={"min": 1048576, "lt": 4294967296})
    rpc_timeout_seconds: float = field(default=60.0, metadata={"gt": 0})


def parse_config(raw):
    config = read_settings(RuntimeConfig, raw, "runtime")
    if config.time_limit_seconds * 1e9 >= 2**63:
        raise ConfigError("runtime.time_limit_seconds exceeds the ns-3 nanosecond clock range")
    return config


def read_json(path):
    def bad_constant(value):
        raise ValueError("nonfinite JSON value: " + value)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=bad_constant, object_pairs_hook=unique)


def number(value, name, positive=False, integral=False):
    valid_type = type(value) is int if integral else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("{} must be a finite {}{}".format(name, "positive " if positive else "nonnegative ", "integer" if integral else "number"))
    return value


class ComputeProfile:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.raw = require_mapping(read_json(path), "profile")
        if self.raw.get("schema_version") != 1:
            raise ValueError("unsupported compute profile schema")
        self.provenance = require_mapping(self.raw.get("provenance"), "profile.provenance")
        if type(self.provenance.get("synthetic")) is not bool or not isinstance(self.provenance.get("description"), str):
            raise ValueError("profile provenance must explicitly declare synthetic and description")
        if self.raw.get("network_communication_excluded") is not True:
            raise ValueError("compute profile must exclude explicitly simulated communication")
        self.kv_bytes_per_token = number(self.raw.get("kv_bytes_per_token"), "kv_bytes_per_token", True, True)
        self.weights_bytes_per_gpu = number(self.raw.get("weights_bytes_per_gpu"), "weights_bytes_per_gpu", integral=True)
        self.phases = require_mapping(self.raw.get("phases"), "profile.phases")
        for phase in ("prefill", "decode"):
            values = require_mapping(self.phases.get(phase), "profile.phases." + phase)
            number(values.get("tp_size"), "tp_size", True, True)
            number(values.get("collectives_per_step"), "collectives_per_step", integral=True)
            points = values.get("points")
            if not isinstance(points, list) or not points:
                raise ValueError("profile phases need at least one point")
            previous = 0
            for point in points:
                point = require_mapping(point, "profile point")
                if set(point) != {"tokens", "compute_seconds", "collective_bytes"}:
                    raise ValueError("profile points require tokens, compute_seconds and collective_bytes")
                number(point["tokens"], "tokens", True, True)
                number(point["compute_seconds"], "compute_seconds", True)
                number(point["collective_bytes"], "collective_bytes", integral=True)
                if point["tokens"] <= previous:
                    raise ValueError("profile points must have unique increasing token counts")
                if values["collectives_per_step"] and point["collective_bytes"] == 0:
                    raise ValueError("enabled collectives must have a positive message size")
                previous = point["tokens"]

    def lookup(self, phase, tokens, tp_size):
        config = self.phases[phase]
        if tp_size != config["tp_size"]:
            raise ValueError("{} TP size does not match profile".format(phase))
        points = config["points"]
        if tokens < points[0]["tokens"] or tokens > points[-1]["tokens"]:
            raise ValueError("{} token count {} is outside calibrated profile coverage [{}, {}]".format(
                phase, tokens, points[0]["tokens"], points[-1]["tokens"]))
        left = right = points[0]
        for point in points:
            if point["tokens"] <= tokens:
                left = point
            if point["tokens"] >= tokens:
                right = point
                break
        fraction = 0 if left is right else (tokens - left["tokens"]) / (right["tokens"] - left["tokens"])
        return {"compute_seconds": left["compute_seconds"] + fraction * (right["compute_seconds"] - left["compute_seconds"]),
                "collective_bytes": math.ceil(left["collective_bytes"] + fraction * (right["collective_bytes"] - left["collective_bytes"])),
                "collectives_per_step": config["collectives_per_step"]}


def load_workload(path):
    raw = require_mapping(read_json(path), "workload")
    if raw.get("schema_version") != 1 or not isinstance(raw.get("requests"), list):
        raise ValueError("workload needs schema_version=1 and a requests list")
    provenance = require_mapping(raw.get("provenance"), "workload.provenance")
    if type(provenance.get("synthetic")) is not bool:
        raise ValueError("workload provenance must explicitly declare synthetic")
    requests, ids = [], set()
    allowed = {"request_id", "arrival_seconds", "input_tokens", "output_tokens", "ttft_slo_seconds", "itl_slo_seconds"}
    for item in raw["requests"]:
        item = require_mapping(item, "workload request")
        if set(item) - allowed or not {"request_id", "arrival_seconds", "input_tokens", "output_tokens"} <= set(item):
            raise ValueError("invalid workload request fields")
        rid = item["request_id"]
        if not isinstance(rid, str) or not rid.strip() or rid in ids:
            raise ValueError("request IDs must be non-empty and unique")
        ids.add(rid)
        number(item["arrival_seconds"], "arrival_seconds")
        number(item["input_tokens"], "input_tokens", True, True)
        number(item["output_tokens"], "output_tokens", True, True)
        for name in ("ttft_slo_seconds", "itl_slo_seconds"):
            if name in item:
                number(item[name], name, True)
        requests.append(item)
    return sorted(requests, key=lambda item: (item["arrival_seconds"], item["request_id"])), provenance
