"""Contracts for one time authority and network-driven dependency completion."""

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass(frozen=True)
class FeedbackConfig:
    time_authority: str = field(default="ns3", metadata={"choices": ("ns3",)})
    network_backend: str = field(default="ns3_alibabacloud", metadata={"choices": ("ns3_alibabacloud",)})
    compute_backend: str = field(default="profiling", metadata={"choices": ("profiling",)})
    compute_profile_path: str = ""
    compute_profile_excludes_communication: bool = True
    observation_delay_us: float = field(default=10.0, metadata={"min": 0})
    scheduling_overhead_us: float = field(default=10.0, metadata={"min": 0})


@dataclass(frozen=True)
class NetworkTransfer:
    transfer_id: str
    kind: str  # kv or collective; the adapter must validate its inputs.
    source_gpu_id: int
    destination_gpu_id: int
    size_bytes: int


@dataclass(frozen=True)
class TransferProgress:
    transfer_id: str
    time_seconds: float
    source_reads_complete: bool
    destination_ready: bool


class SimulationClock(Protocol):
    def now_seconds(self) -> float:
        ...

    def call_at(self, time_seconds: float, callback: Callable[[], None]) -> None:
        """Schedule in the backend's event queue, not a second independent clock."""
        ...


class NetworkBackend(Protocol):
    def submit(self, transfer: NetworkTransfer,
               on_progress: Callable[[TransferProgress], None]) -> None:
        """Submit KV/collective traffic to the same network resources.

        Distinguish source-read completion from destination readiness. Backend
        callback semantics must be verified before either dependency is released.
        Dependent compute waits for its actual communication completion.
        """
        ...
