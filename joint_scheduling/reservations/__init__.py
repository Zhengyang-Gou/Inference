"""Physical-object reservation and lifetime-management contracts."""

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class ReservationsConfig:
    reserve_max_output_tokens: int = field(default=1024, metadata={"min": 1})
    capacity_safety_fraction: float = field(default=0.05, metadata={"min": 0, "lt": 1})
    shared_prefix_accounting: str = field(default="physical_object", metadata={"choices": ("physical_object",)})
    source_release: str = field(default="after_dependencies", metadata={"choices": ("after_dependencies",)})


@dataclass(frozen=True)
class PhysicalObjectReservation:
    object_id: str
    memory_domain_id: str
    total_size_bytes: int
    request_ids: Tuple[str, ...]


class ReservationLedger(Protocol):
    def try_commit(self, plan_id: str, objects: Sequence[PhysicalObjectReservation]) -> bool:
        """Atomically reserve a plan or leave the ledger unchanged.

        Deduplicate by (memory_domain_id, object_id), including existing objects.
        Total occupied + unmaterialized reservations must remain within capacity
        after model weights, fixed buffers and safety margins are deducted.
        """
        ...

    def materialize(self, memory_domain_id: str, object_id: str, size_bytes: int) -> None:
        """Convert equal bytes from reservation to occupancy without double count."""
        ...

    def release_request(self, request_id: str) -> None:
        """Remove useless reservations/references; preserve dependencies and cache refs."""
        ...

    def snapshot(self) -> Mapping[str, object]:
        ...
