"""Validated baseline inputs and compute-only profile interpolation."""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..validation import ConfigError, read_settings, require_mapping


IMMEDIATE_KV_MODES = ("whole_ready_immediate", "whole_ready_chunked", "layer_ready_immediate")
CONTROLLED_KV_MODES = ("layer_ready_fifo", "layer_ready_slo")
KV_TRANSFER_MODES = IMMEDIATE_KV_MODES + CONTROLLED_KV_MODES


@dataclass(frozen=True)
class RuntimeConfig:
    seed: int = field(default=7, metadata={"min": 0, "lt": 2147483647})
    workload_path: str = ""
    time_limit_seconds: float = field(default=10.0, metadata={"gt": 0})
    congestion_control: str = field(default="dcqcn", metadata={"choices": ("dcqcn", "none")})
    packet_payload_bytes: int = field(default=1000, metadata={"min": 64, "lt": 9001})
    switch_buffer_bytes: int = field(default=16777216, metadata={"min": 1048576, "lt": 4294967296})
    rpc_timeout_seconds: float = field(default=60.0, metadata={"gt": 0})
    prefill_batch_size: int = field(default=1, metadata={"min": 1})
    prefill_batch_token_budget: int = field(default=0, metadata={"min": 0})
    prefill_chunk_tokens: int = field(default=0, metadata={"min": 0})
    decode_batch_size: int = field(default=1, metadata={"min": 1})
    network_priority_enabled: bool = False
    kv_transfer_mode: str = field(default="whole_ready_immediate", metadata={"choices": KV_TRANSFER_MODES})


def parse_config(raw):
    config = read_settings(RuntimeConfig, raw, "runtime")
    if config.prefill_batch_token_budget and not config.prefill_chunk_tokens:
        raise ConfigError("prefill batching requires a positive prefill_chunk_tokens cap")
    if config.prefill_batch_size > 1 and not config.prefill_batch_token_budget:
        raise ConfigError("prefill_batch_size > 1 requires prefill_batch_token_budget")
    if config.time_limit_seconds * 1e9 >= 2**63:
        raise ConfigError("runtime.time_limit_seconds exceeds the ns-3 nanosecond clock range")
    return config


# Stable imports for existing profiles, CLI and external callers.
from .inputs import read_json, number, load_workload, validate_requests
from .compute_profile import ComputeProfile
