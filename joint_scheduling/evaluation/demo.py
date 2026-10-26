"""Deterministic synthetic traces for exercising evaluation, not benchmarking."""

import random
from pathlib import Path

from .recorder import TraceRecorder


def create_demo_trace(path, strategy="synthetic_demo", seed=7, horizon=10.0):
    rng = random.Random(seed)
    events = []
    count = max(1, int(horizon * 12))
    for index in range(count):
        rid = "r{:04d}".format(index)
        arrival = (index + rng.random() * 0.6) * horizon / count
        output_tokens = 1 if index % 11 == 0 else 2 if index % 7 == 0 else rng.randint(3, 12)
        ttft = rng.uniform(0.025, 0.23)
        gaps = [rng.uniform(0.015, 0.14)] + [rng.uniform(0.01, 0.06) for _ in range(max(0, output_tokens - 2))]
        if index % 9 == 0 and len(gaps) > 1:
            gaps[-1] = 0.16
        events.append((arrival, 0, rid, "arrive", output_tokens))
        time = arrival + ttft
        for token_index in range(1, output_tokens + 1):
            if time <= horizon:
                events.append((time, 1, rid, "token", token_index))
            if token_index < output_tokens:
                time += gaps[token_index - 1]
        if time <= horizon:
            events.append((time, 2, rid, "complete", None))
    with TraceRecorder(Path(path), strategy, seed,
                       {"synthetic": True, "purpose": "evaluation pipeline demonstration only"}) as recorder:
        for time, _, rid, kind, value in sorted(events):
            if kind == "arrive":
                recorder.request_arrived(rid, time, 0.15, 0.1, value)
            elif kind == "token":
                recorder.token_returned(rid, time, value)
            else:
                recorder.request_completed(rid, time)
        recorder.finish(horizon)
    return Path(path)
