"""Disk retention policy, independent of simulation and controller visibility."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecordingConfig:
    mode: str = field(default="essential", metadata={"choices": ("essential", "debug")})

    @property
    def detailed(self):
        return self.mode == "debug"


# Request-level milestones needed to explain queuing and P/D handoff. Lifecycle
# and every returned token are always retained by TraceRecorder itself.
ESSENTIAL_AUXILIARY_EVENTS = frozenset({
    "binding_queued", "binding_committed", "prefill_started", "decode_started",
    "prefix_cache_lookup", "kv_all_received", "kv_ready",
})
