"""Joint binding configuration and policy/predictor contracts."""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class BindingConfig:
    policy: str = field(default="joint_lexicographic", metadata={"choices": ("joint_lexicographic", "round_robin")})
    window_ms: float = field(default=1.0, metadata={"gt": 0})
    early_trigger_margin_ms: float = field(default=0.5, metadata={"min": 0})
    max_wait_ms: float = field(default=10.0, metadata={"gt": 0})


@dataclass(frozen=True)
class Binding:
    request_id: str
    prefill_instance_id: str
    decode_instance_id: str


@dataclass(frozen=True)
class RequestPrediction:
    ttft_seconds: float
    handoff_seconds: Optional[float]
    later_max_itl_seconds: Optional[float]
    minimum_normalized_margin: float


@dataclass(frozen=True)
class BindingDecision:
    bindings: Tuple[Binding, ...]
    deferred_request_ids: Tuple[str, ...]


class PlanPredictor(Protocol):
    def predict(self, plan: Sequence[Binding], observed_state: Mapping[str, object]) -> Mapping[str, RequestPrediction]:
        """Evaluate the whole tentative plan, including effects on existing requests.

        TTFT includes elapsed waiting. Predictions must not use future arrivals
        or double-count full KV transfer time in TTFT.
        """
        ...


class BindingPolicy(Protocol):
    def select(self, pending_request_ids: Sequence[str], observed_state: Mapping[str, object],
               predictor: PlanPredictor) -> BindingDecision:
        """Choose unique feasible bindings or defer; commit reservations atomically."""
        ...
