"""Contracts for one time authority and network-driven dependency completion."""

from dataclasses import dataclass, field
from typing import Callable, Protocol

# The only valid (time_authority, network_backend) pairs. The default stays the
# ns-3 packet-level backend; flowSim is an explicitly selected fluid model.
NS3_PAIR = ("ns3", "ns3_alibabacloud")
FLOWSIM_PAIR = ("flowsim", "flowsim")
BACKEND_PAIRS = (NS3_PAIR, FLOWSIM_PAIR)


@dataclass(frozen=True)
class FeedbackConfig:
    time_authority: str = field(default="ns3", metadata={"choices": ("ns3", "flowsim")})
    network_backend: str = field(default="ns3_alibabacloud", metadata={"choices": ("ns3_alibabacloud", "flowsim")})
    compute_backend: str = field(default="profiling", metadata={"choices": ("profiling",)})
    compute_profile_path: str = ""
    compute_profile_excludes_communication: bool = True
    observation_delay_us: float = field(default=10.0, metadata={"min": 0})
    scheduling_overhead_us: float = field(default=10.0, metadata={"min": 0})
    network_observation_enabled: bool = False
    state_observation_enabled: bool = True
    network_observation_interval_us: float = field(default=100.0, metadata={"min": 0.001})
    network_ewma_alpha: float = field(default=0.3, metadata={"gt": 0})


@dataclass(frozen=True)
class NetworkTransfer:
    transfer_id: str
    kind: str  # kv or collective; the adapter must validate its inputs.
    source_gpu_id: int
    destination_gpu_id: int
    size_bytes: int
    priority: int = 2  # 0 urgent KV, 1 collective, 2 normal KV, 3 loose KV.


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
    def network_snapshot(self) -> object:
        """Read current classified counters, queues and ACK progress without advancing time.

        Online consumers must apply observation delay before using this snapshot.
        """
        ...

    def submit(self, transfer: NetworkTransfer,
               on_progress: Callable[[TransferProgress], None]) -> None:
        """Submit KV/collective traffic to the same network resources.

        Distinguish source-read completion from destination readiness. Backend
        callback semantics must be verified before either dependency is released.
        Dependent compute waits for its actual communication completion.
        """
        ...
