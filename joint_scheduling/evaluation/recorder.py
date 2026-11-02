"""Append-only JSONL event recording for a simulator's callbacks."""

import json
import math
from pathlib import Path

from ..recording import ESSENTIAL_AUXILIARY_EVENTS


LIFECYCLE_EVENTS = {"run_metadata", "request_arrived", "token_returned", "request_completed", "request_failed", "run_end"}


class TraceRecorder:
    """Record seconds in nondecreasing order; never overwrite an existing trace.

    Call finish() with the actual simulated observation horizon, including idle
    time. Closing a file does not invent a completed observation interval.
    """

    def __init__(self, path, strategy, seed=0, metadata=None, detailed=True):
        if not isinstance(strategy, str) or not strategy.strip():
            raise ValueError("strategy must be a non-empty string")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        payload = {"schema_version": 1, "strategy": strategy, "seed": seed, "metadata": metadata if metadata is not None else {}}
        json.dumps(payload, allow_nan=False)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x", encoding="utf-8")
        self._last_time = 0.0
        self._finished = False
        self.detailed = detailed
        self.recorded_counts, self.omitted_counts = {}, {}
        self._write("run_metadata", 0.0, payload)

    def _write(self, kind, at_seconds, payload):
        if self._finished or self._file.closed:
            raise ValueError("trace is already finished or closed")
        if type(at_seconds) not in (int, float) or not math.isfinite(at_seconds) or at_seconds < self._last_time:
            raise ValueError("event times must be finite, nonnegative and nondecreasing")
        if {"event", "time_seconds"} & set(payload):
            raise ValueError("payload must not override event or time_seconds")
        if self.wants_event(kind):
            event = dict(payload, event=kind, time_seconds=float(at_seconds))
            line = json.dumps(event, ensure_ascii=False, allow_nan=False)
            self._file.write(line + "\n")
            self._file.flush()
            counts = self.recorded_counts
        else:
            counts = self.omitted_counts
        counts[kind] = counts.get(kind, 0) + 1
        self._last_time = at_seconds

    def wants_event(self, kind):
        return self.detailed or kind in LIFECYCLE_EVENTS or kind in ESSENTIAL_AUXILIARY_EVENTS

    def request_arrived(self, request_id, at_seconds, ttft_slo_seconds=None,
                        itl_slo_seconds=None, expected_output_tokens=None):
        self._write("request_arrived", at_seconds, {
            "request_id": request_id, "ttft_slo_seconds": ttft_slo_seconds,
            "itl_slo_seconds": itl_slo_seconds, "expected_output_tokens": expected_output_tokens})

    def token_returned(self, request_id, at_seconds, token_index):
        """token_index is contiguous and 1-based; record user-visible returns."""
        self._write("token_returned", at_seconds, {"request_id": request_id, "token_index": token_index})

    def request_completed(self, request_id, at_seconds):
        self._write("request_completed", at_seconds, {"request_id": request_id})

    def request_failed(self, request_id, at_seconds, reason):
        self._write("request_failed", at_seconds, {"request_id": request_id, "reason": str(reason)})

    def record_event(self, kind, at_seconds, **payload):
        """Record auxiliary binding/layer/transfer/resource observations."""
        if not isinstance(kind, str) or not kind.strip() or kind in LIFECYCLE_EVENTS:
            raise ValueError("use the dedicated methods for lifecycle events")
        self._write(kind, at_seconds, payload)

    def finish(self, observed_until_seconds):
        self._write("run_end", observed_until_seconds, {})
        self._finished = True

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
