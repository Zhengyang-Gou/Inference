"""Finite-window pressure metrics, including unfinished and censored requests."""

import math
from collections import defaultdict


def _number(value):
    return None if value in (None, "") else float(value)


def _true(value):
    return value is True or value == "True"


def summarize_pressure(requests, tokens, start, end, horizon):
    """Use [start,end); backlog is sampled just before each boundary.

    Never infer SLO success from only the requests which finished. An unfinished
    request may already have missed a deadline; otherwise its outcome is unknown.
    The arrival-cohort success interval includes that censoring explicitly.
    """
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end, horizon)) or not 0 <= start < end <= horizon:
        raise ValueError("pressure window requires 0 <= start < end <= horizon")
    rows, token_times = list(requests), defaultdict(list)
    output_count = decode_count = 0
    for token in tokens:
        at = float(token["returned_at_seconds"])
        token_times[token["request_id"]].append(at)
        if start <= at < end:
            output_count += 1
            decode_count += int(token["token_index"]) > 1
    arrivals = completed = good = failed = before_start = before_end = 0
    cohort = passed = missed = unknown = 0
    changes = defaultdict(int)
    for row in rows:
        arrived = float(row["arrived_at_seconds"])
        terminal = _number(row.get("completed_at_seconds"))
        failure = _number(row.get("failed_at_seconds"))
        terminal = terminal if terminal is not None else failure
        before_start += arrived < start and (terminal is None or terminal >= start)
        before_end += arrived < end and (terminal is None or terminal >= end)
        if start <= arrived < end:
            arrivals += 1
            cohort += 1
            times = token_times[row["request_id"]]
            violated = False
            ttft, itl = float(row["ttft_slo_seconds"]), float(row["itl_slo_seconds"])
            if times:
                violated |= times[0]-arrived > ttft+1e-12
                violated |= any(b-a > itl+1e-12 for a, b in zip(times, times[1:]))
            elif horizon-arrived > ttft+1e-12:
                violated = True
            if row["status"] == "completed":
                passed += _true(row["slo_met"])
                missed += not _true(row["slo_met"])
            elif row["status"] == "failed":
                missed += 1
            elif violated or times and horizon-times[-1] > itl+1e-12:
                missed += 1
            else:
                unknown += 1
        done = _number(row.get("completed_at_seconds"))
        if done is not None and start <= done < end:
            completed += 1
            good += _true(row["slo_met"])
        if failure is not None and start <= failure < end:
            failed += 1
        if start <= arrived < end:
            changes[arrived] += 1
        if terminal is not None and start <= terminal < end:
            changes[terminal] -= 1
    assert cohort == passed+missed+unknown
    assert before_end-before_start == arrivals-completed-failed
    active, peak, area, previous = before_start, before_start, 0.0, start
    for at, delta in sorted(changes.items()):
        area += active*(at-previous)
        active += delta
        peak = max(peak, active)
        previous = at
    area += active*(end-previous)
    duration = end-start
    return dict(measurement_start_seconds=start, measurement_end_seconds=end, observed_until_seconds=horizon,
        measured_arrivals=arrivals, measured_completions=completed, measured_good_completions=good,
        measured_failures=failed, offered_rps=arrivals/duration, completion_rps=completed/duration,
        goodput_rps=good/duration, output_tokens_per_second=output_count/duration,
        decode_tokens_per_second=decode_count/duration, backlog_start=before_start, backlog_end=before_end,
        backlog_change=before_end-before_start, backlog_growth_rps=(before_end-before_start)/duration,
        mean_outstanding=area/duration, peak_outstanding=peak, cohort_count=cohort,
        cohort_slo_passed=passed, cohort_slo_missed=missed, cohort_slo_unknown=unknown,
        cohort_slo_fraction_lower=passed/cohort if cohort else None,
        cohort_slo_fraction_upper=(passed+unknown)/cohort if cohort else None)


PRESSURE_STATE_KEYS = ("prefill_queue_length", "decode_queue_length", "kv_inflight_count", "kv_inflight_bytes",
                       "collective_inflight_count", "collective_inflight_bytes")


class PressureStateAccumulator:
    """Exact window integrals without retaining or exporting every state."""

    def __init__(self, start, end):
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
            raise ValueError("pressure states require 0 <= start < end")
        self.start, self.end = start, end
        self.previous, self.last = None, 0.0
        self.areas = dict.fromkeys(PRESSURE_STATE_KEYS, 0.0)
        self.peaks = dict.fromkeys(PRESSURE_STATE_KEYS, 0.0)

    def add(self, state):
        if self.last >= self.end:
            return
        now = float(state["time_seconds"])
        if self.previous is None and now > self.start:
            raise ValueError("pressure states do not cover measurement start")
        if not math.isfinite(now) or now < self.last:
            raise ValueError("pressure state clock moved backwards or is nonfinite")
        if self.previous is not None:
            span = max(0, min(self.end, now)-max(self.start, self.last))
            for key in PRESSURE_STATE_KEYS:
                self.areas[key] += span*self.previous[key]
                if span:
                    self.peaks[key] = max(self.peaks[key], self.previous[key])
        self.previous = {key: float(state[key]) for key in PRESSURE_STATE_KEYS}
        self.last = now

    def finish(self):
        if self.last < self.end:
            raise ValueError("pressure states do not cover measurement end")
        return dict(mean={key: area/(self.end-self.start) for key, area in self.areas.items()},
                    maximum=dict(self.peaks))


def summarize_pressure_states(states, start, end):
    """Integrate event-held queue/inflight gauges over the measurement window."""
    accumulator = PressureStateAccumulator(start, end)
    for state in states:
        accumulator.add(state)
        if accumulator.last >= end:
            break
    return accumulator.finish()
