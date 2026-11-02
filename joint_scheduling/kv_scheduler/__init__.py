"""Layer-ready KV and non-preemptive transmission-admission contracts."""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class KVConfig:
    policy: str = field(default="slo_slack", metadata={"choices": ("slo_slack", "slo_mlu", "ready_immediate")})
    max_block_bytes: int = field(default=1048576, metadata={"min": 1})
    max_concurrent_transfers: int = field(default=8, metadata={"min": 1})
    max_inflight_bytes: int = field(default=8388608, metadata={"min": 1})
    safety_margin_fraction: float = field(default=0.1, metadata={"min": 0, "lt": 1})
    control_interval_us: float = field(default=50.0, metadata={"min": 0.001})
    collective_guard_us: float = field(default=20.0, metadata={"min": 0})
    max_resource_inflight_bytes: int = field(default=1048576, metadata={"min": 1})
    queue_target_bytes: int = field(default=65536, metadata={"min": 1})
    collective_reserve_fraction: float = field(default=0.7, metadata={"min": 0, "lt": 1})
    pause_on_collective: bool = False
    slo_priority_enabled: bool = True
    collective_protection_enabled: bool = True
    inflight_budget_mode: str = field(default="adaptive", metadata={"choices": ("adaptive", "fixed")})
    collective_max_blocking_us: float = field(default=50.0, metadata={"min": 0})
    completion_feedback_alpha: float = field(default=0.3, metadata={"gt": 0})
    decode_admission_limit: int = field(default=0, metadata={"min": 0})
    mlu_medium_threshold: float = field(default=0.125, metadata={"gt": 0, "lt": 1})
    mlu_urgent_threshold: float = field(default=0.5, metadata={"gt": 0, "lt": 1})
    mlu_deadline_guard_us: float = field(default=50.0, metadata={"min": 0})
    mlu_best_effort_fraction: float = field(default=0.02, metadata={"gt": 0, "lt": 1})


@dataclass(frozen=True)
class KVBlock:
    block_id: str
    request_id: str
    layer_id: int
    source_gpu_id: int
    destination_gpu_id: int
    size_bytes: int
    ready_at_seconds: float
    latest_delivery_seconds: Optional[float]
    offset_bytes: int = 0
    source_object_id: str = ""
    destination_object_id: str = ""


@dataclass(frozen=True)
class TransferPermit:
    block_id: str
    allowed_bytes: int
    priority: int = 2


class KVPolicy(Protocol):
    def on_layer_ready(self, blocks: Sequence[KVBlock]) -> None:
        """Register ready blocks without pausing subsequent compute by default."""
        ...

    def admit(self, now_seconds: float, observed_resources: Mapping[str, object]) -> Sequence[TransferPermit]:
        """Admit starts within endpoint/path budgets, including existing in-flight work.

        A permit controls admission, not the actual network rate. Started transfers
        continue; source KV stays pinned until its read dependencies have ended.
        Dynamic multipath requires observable resource budgets, not an assumed
        fixed route known to the application.
        """
        ...


def split_layer_kv(request_id, layer_id, sources, destinations, size_bytes, max_block_bytes, ready_at_seconds, start_bytes=0):
    """Shard one layer across equal TP ranks, then split without losing bytes.

    Offsets are within the layer's rank shard. start_bytes selects a cumulative
    byte range so chunk rounding never exceeds the full layer reservation. Admission budgets are deliberately
    not applied here: the immediate baseline submits every resulting block.
    """
    if not sources or len(sources) != len(destinations):
        raise ValueError("KV sharding requires equal nonempty P/D ranks")
    if type(size_bytes) is not int or size_bytes < 0 or type(max_block_bytes) is not int or max_block_bytes < 1:
        raise ValueError("KV size must be nonnegative and block size positive")
    if type(start_bytes) is not int or start_bytes < 0:
        raise ValueError("KV prefix bytes must be a nonnegative integer")
    base, extra = divmod(start_bytes+size_bytes, len(sources))
    prior_base, prior_extra = divmod(start_bytes, len(sources))
    blocks = []
    for rank, (src, dst) in enumerate(zip(sources, destinations)):
        start = prior_base + int(rank < prior_extra)
        shard = base + int(rank < extra)
        for offset in range(start, shard, max_block_bytes):
            blocks.append(KVBlock("{}:l{}:r{}:b{}".format(request_id, layer_id, rank, offset),
                                  request_id, layer_id, src, dst, min(max_block_bytes, shard - offset),
                                  ready_at_seconds, None, offset))
    return tuple(blocks)
