"""Allowed experiment parameters and side-effect-free override helpers."""
import copy
from dataclasses import fields
from pathlib import Path

from ..binding import BindingConfig
from ..prefix_cache import PrefixCacheConfig


CONTROL_SWEEPS = {"kv_scheduler.max_concurrent_transfers", "kv_scheduler.max_inflight_bytes",
    "kv_scheduler.policy", "kv_scheduler.mlu_medium_threshold", "kv_scheduler.mlu_urgent_threshold",
    "kv_scheduler.mlu_deadline_guard_us", "kv_scheduler.mlu_best_effort_fraction",
    "kv_scheduler.max_resource_inflight_bytes", "kv_scheduler.control_interval_us", "kv_scheduler.collective_guard_us",
    "kv_scheduler.queue_target_bytes", "kv_scheduler.safety_margin_fraction", "kv_scheduler.collective_reserve_fraction",
    "kv_scheduler.pause_on_collective", "kv_scheduler.inflight_budget_mode",
    "kv_scheduler.collective_max_blocking_us", "kv_scheduler.completion_feedback_alpha",
    "kv_scheduler.slo_priority_enabled", "kv_scheduler.collective_protection_enabled",
    "feedback.scheduling_overhead_us"}


EXPLICIT_OVERRIDES = frozenset({
    'feedback.network_ewma_alpha',
    'feedback.network_observation_enabled',
    'feedback.network_observation_interval_us',
    'feedback.observation_delay_us',
    'feedback.state_observation_enabled',
    'kv_scheduler.collective_guard_us',
    'kv_scheduler.control_interval_us',
    'kv_scheduler.max_block_bytes',
    'kv_scheduler.max_concurrent_transfers',
    'kv_scheduler.max_inflight_bytes',
    'kv_scheduler.max_resource_inflight_bytes',
    'kv_scheduler.queue_target_bytes',
    'kv_scheduler.safety_margin_fraction',
    'reservations.capacity_safety_fraction',
    'reservations.reserve_max_output_tokens',
    'runtime.congestion_control',
    'runtime.decode_batch_size',
    'runtime.kv_transfer_mode',
    'runtime.network_priority_enabled',
    'runtime.packet_payload_bytes',
    'runtime.prefill_batch_size',
    'runtime.prefill_batch_token_budget',
    'runtime.prefill_chunk_tokens',
    'runtime.rpc_timeout_seconds',
    'runtime.rpc_submission_batch_size',
    'runtime.switch_buffer_bytes',
    'runtime.time_limit_seconds',
})
SCHEMA_OVERRIDES = frozenset(
    section + "." + field.name
    for section, schema in (("binding", BindingConfig), ("prefix_cache", PrefixCacheConfig))
    for field in fields(schema)
)


def resolve_input(value, parent, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a nonempty path".format(label))
    path = Path(value)
    path = (parent / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise ValueError("{} does not exist: {}".format(label, path))
    return str(path)


def allowed_override(key):
    # Reject inert policy/interface knobs: every accepted sweep affects an
    # implemented part of this baseline or its evaluation/observation output.
    return (key in CONTROL_SWEEPS or key.startswith("topology.") and key.count(".") == 1
            or key.startswith("workload.") and len(key.split(".")) >= 2
            or key in EXPLICIT_OVERRIDES
            or key in SCHEMA_OVERRIDES
            or key.startswith("evaluation.") and key.count(".") == 1 and key != "evaluation.output_dir")


def set_override(root, dotted, value):
    keys = dotted.split(".")
    for key in keys[:-1]:
        if key not in root or not isinstance(root[key], dict):
            raise ValueError("override requires an object at: {}".format(dotted))
        root = root[key]
    root[keys[-1]] = copy.deepcopy(value)
