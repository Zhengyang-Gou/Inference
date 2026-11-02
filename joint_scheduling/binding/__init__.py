"""Joint binding configuration and policy/predictor contracts."""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class BindingConfig:
    policy: str = field(default="joint_lexicographic", metadata={"choices": ("joint_lexicographic", "round_robin", "balanced_random")})
    seed: int = field(default=7, metadata={"min": 0, "lt": 2147483647})
    window_ms: float = field(default=1.0, metadata={"gt": 0})
    early_trigger_margin_ms: float = field(default=0.5, metadata={"min": 0})
    max_wait_ms: float = field(default=10.0, metadata={"gt": 0})
    joint_selection_enabled: bool = False
    candidate_count: int = field(default=4, metadata={"min": 1, "lt": 65})
    max_requests_per_round: int = field(default=16, metadata={"min": 1, "lt": 257})
    max_pending_scan: int = field(default=64, metadata={"min": 1, "lt": 4097})
    max_pair_checks: int = field(default=64, metadata={"min": 1, "lt": 4097})
    max_score_work: int = field(default=16384, metadata={"min": 1})
    max_state_requests: int = field(default=256, metadata={"min": 1})
    max_state_objects: int = field(default=4096, metadata={"min": 1})
    starvation_ms: float = field(default=10.0, metadata={"gt": 0})
    wall_clock_diagnostics: bool = True
    prediction_sample_every: int = field(default=0, metadata={"min": 0})
    window_enabled: bool = False
    prediction_enabled: bool = False
    decision_overhead_us: float = field(default=0.0, metadata={"min": 0})
    prediction_decode_steps: int = field(default=8, metadata={"min": 1, "lt": 257})
    prediction_max_events: int = field(default=100000, metadata={"min": 1})
    prediction_time_limit_seconds: float = field(default=10.0, metadata={"gt": 0})


@dataclass(frozen=True)
class Binding:
    request_id: str
    prefill_instance_id: str
    decode_instance_id: str


@dataclass(frozen=True)
class RequestPrediction:
    ttft_seconds: Optional[float]
    handoff_seconds: Optional[float]
    later_max_itl_seconds: Optional[float]
    minimum_normalized_margin: Optional[float]
    feasible: bool = True
    complete: bool = True
    reason: Optional[str] = None
    prefill_hit_tokens: int = 0
    decode_hit_tokens: int = 0
    predicted_token_times: Tuple[float, ...] = ()
    output_token_limit: int = 0
    horizon_output_tokens: int = 0


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
