"""Layer-ready KV and non-preemptive transmission-admission contracts."""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class KVConfig:
    policy: str = field(default="slo_slack", metadata={"choices": ("slo_slack", "ready_immediate")})
    max_block_bytes: int = field(default=1048576, metadata={"min": 1})
    max_concurrent_transfers: int = field(default=8, metadata={"min": 1})
    max_inflight_bytes: int = field(default=8388608, metadata={"min": 1})
    safety_margin_fraction: float = field(default=0.1, metadata={"min": 0, "lt": 1})


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


@dataclass(frozen=True)
class TransferPermit:
    block_id: str
    allowed_bytes: int


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
