"""Replay validated token traces and calculate request-level SLO metrics."""

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import EvaluationConfig


LATENCY_FIELDS = ("ttft_seconds", "handoff_seconds", "later_max_itl_seconds", "max_itl_seconds",
                  "mean_tpot_seconds", "e2e_seconds")


class EvaluationError(ValueError):
    pass


def _number(value, name, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise EvaluationError("{} must be a finite {} number".format(name, "positive" if positive else "nonnegative"))
    return float(value)


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError("{} must be a non-empty string".format(name))
    return value


def _within(value, limit):
    return value <= limit or math.isclose(value, limit, rel_tol=1e-9, abs_tol=1e-12)


def quantile(values, q):
    """Linear interpolation at q*(n-1), ignoring no samples (not zero-filling)."""
    if not values:
        return None
    values = sorted(values)
    position = q * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def distribution(values, percentiles):
    return {"count": len(values), "mean": math.fsum(values) / len(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "quantiles": {str(q): quantile(values, q) for q in percentiles}}


@dataclass
class _Request:
    request_id: str
    arrived_at: float
    ttft_slo: float
    itl_slo: float
    expected_tokens: Optional[int]
    tokens: List[float] = field(default_factory=list)
    completed_at: Optional[float] = None
    failed_at: Optional[float] = None
    failure_reason: str = ""

    @property
    def terminal(self):
        return self.completed_at is not None or self.failed_at is not None


@dataclass
class EvaluationResult:
    summary: dict
    requests: list
    tokens: list
    timeseries: list


def _no_constant(value):
    raise EvaluationError("Non-finite JSON constant: {}".format(value))


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("Duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def analyze_trace(path, config=None):
    config = config or EvaluationConfig()
    # Public callers get the same validation as the CLI, including direct dataclass construction.
    from .config import parse_config
    raw_config = asdict(config)
    for key in ("percentiles", "plot_formats", "charts"):
        raw_config[key] = list(raw_config[key])
    config = parse_config(raw_config)
    states, metadata, observed_until = {}, None, None
    previous_time, auxiliary_count = 0.0, 0
    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line, parse_constant=_no_constant, object_pairs_hook=_unique_keys)
                if not isinstance(event, dict):
                    raise EvaluationError("event must be an object")
                kind = _identifier(event.get("event"), "event")
                time = _number(event.get("time_seconds"), "time_seconds")
                if time < previous_time:
                    raise EvaluationError("event time moved backwards")
                if observed_until is not None:
                    raise EvaluationError("events appeared after run_end")
                if metadata is None and kind != "run_metadata":
                    raise EvaluationError("the first event must be run_metadata")
                previous_time = time
                if kind == "run_metadata":
                    if metadata is not None or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
                        raise EvaluationError("duplicate metadata or unsupported schema_version")
                    _identifier(event.get("strategy"), "strategy")
                    if type(event.get("seed")) is not int or not isinstance(event.get("metadata", {}), dict):
                        raise EvaluationError("metadata requires an integer seed and an object metadata field")
                    metadata = {k: v for k, v in event.items() if k not in ("event", "time_seconds")}
                elif kind == "run_end":
                    observed_until = time
                elif kind == "request_arrived":
                    rid = _identifier(event.get("request_id"), "request_id")
                    if rid in states:
                        raise EvaluationError("duplicate request arrival: {}".format(rid))
                    expected = event.get("expected_output_tokens")
                    if expected is not None and (type(expected) is not int or expected < 1):
                        raise EvaluationError("expected_output_tokens must be a positive integer or null")
                    ttft = event.get("ttft_slo_seconds")
                    itl = event.get("itl_slo_seconds")
                    states[rid] = _Request(rid, time,
                        _number(config.default_ttft_slo_seconds if ttft is None else ttft, "ttft_slo_seconds", True),
                        _number(config.default_itl_slo_seconds if itl is None else itl, "itl_slo_seconds", True), expected)
                elif kind in ("token_returned", "request_completed", "request_failed"):
                    rid = _identifier(event.get("request_id"), "request_id")
                    if rid not in states or states[rid].terminal:
                        raise EvaluationError("unknown or already terminated request: {}".format(rid))
                    state = states[rid]
                    if kind == "token_returned":
                        index = event.get("token_index")
                        if type(index) is not int or index != len(state.tokens) + 1:
                            raise EvaluationError("token_index must be contiguous and 1-based")
                        if state.expected_tokens is not None and index > state.expected_tokens:
                            raise EvaluationError("more output tokens than expected")
                        state.tokens.append(time)
                    elif kind == "request_completed":
                        if not state.tokens:
                            raise EvaluationError("a successfully completed request needs at least one returned token")
                        if state.expected_tokens is not None and len(state.tokens) != state.expected_tokens:
                            raise EvaluationError("request completed with missing output tokens")
                        state.completed_at = time
                    else:
                        state.failed_at = time
                        state.failure_reason = str(event.get("reason", ""))
                else:
                    auxiliary_count += 1
            except (ValueError, TypeError, KeyError) as error:
                raise EvaluationError("{}:{}: {}".format(path, line_number, error)) from error
    if metadata is None or observed_until is None:
        raise EvaluationError("trace requires run_metadata and an explicit run_end observation horizon")
    if observed_until < config.window_end_seconds:
        raise EvaluationError("measurement window extends beyond the recorded observation horizon")

    rows, token_rows = [], []
    start, end = config.window_start_seconds, config.window_end_seconds
    for state in states.values():
        times = state.tokens
        gaps = [b - a for a, b in zip(times, times[1:])]
        ttft = times[0] - state.arrived_at if times else None
        handoff = gaps[0] if gaps else None
        later = max(gaps[1:]) if len(gaps) > 1 else None
        checks = {"ttft_slo_met": None if ttft is None else _within(ttft, state.ttft_slo),
                  "handoff_slo_met": None if handoff is None else _within(handoff, state.itl_slo),
                  "later_itl_slo_met": None if later is None else _within(later, state.itl_slo)}
        status = "completed" if state.completed_at is not None else "failed" if state.failed_at is not None else "incomplete"
        if status == "completed":
            slo_met = all(check for check in checks.values() if check is not None)
        elif status == "failed" or any(value is False for value in checks.values()):
            slo_met = False
        else:
            slo_met = None
        margins = [(state.ttft_slo - ttft) / state.ttft_slo] if ttft is not None else []
        margins += [(state.itl_slo - value) / state.itl_slo for value in (handoff, later) if value is not None]
        row = {"request_id": state.request_id, "status": status,
               "arrived_at_seconds": state.arrived_at, "completed_at_seconds": state.completed_at,
               "failed_at_seconds": state.failed_at, "failure_reason": state.failure_reason,
               "output_tokens": len(times), "expected_output_tokens": state.expected_tokens,
               "ttft_slo_seconds": state.ttft_slo, "itl_slo_seconds": state.itl_slo,
               "ttft_seconds": ttft, "handoff_seconds": handoff, "later_max_itl_seconds": later,
               "max_itl_seconds": max(gaps) if gaps else None,
               "mean_tpot_seconds": math.fsum(gaps) / len(gaps) if gaps else None,
               "e2e_seconds": state.completed_at - state.arrived_at if state.completed_at is not None else None,
               "minimum_normalized_slo_margin": min(margins) if margins else None,
               "slo_met": slo_met, "arrival_cohort": start <= state.arrived_at < end,
               "completion_window": state.completed_at is not None and start <= state.completed_at < end,
               **checks}
        rows.append(row)
        for index, time in enumerate(times, 1):
            token_rows.append({"request_id": state.request_id, "token_index": index,
                               "returned_at_seconds": time,
                               "itl_seconds": gaps[index - 2] if index > 1 else None,
                               "phase": "first" if index == 1 else "handoff" if index == 2 else "later"})
    rows.sort(key=lambda row: (row["arrived_at_seconds"], row["request_id"]))
    token_rows.sort(key=lambda row: (row["returned_at_seconds"], row["request_id"], row["token_index"]))
    cohort = [row for row in rows if row["arrival_cohort"]]
    cohort_completed = [row for row in cohort if row["status"] == "completed"]
    completed = [row for row in rows if row["completion_window"]]
    passed = sum(row["slo_met"] is True for row in completed)
    cohort_passed = sum(row["slo_met"] is True for row in cohort_completed)
    duration = end - start
    bin_count = max(1, math.ceil(duration / config.time_bin_seconds - 1e-12))
    bins = [{"start_seconds": start + i * config.time_bin_seconds,
             "end_seconds": min(end, start + (i + 1) * config.time_bin_seconds),
             "completed_count": 0, "slo_met_count": 0} for i in range(bin_count)]
    for row in completed:
        index = min(bin_count - 1, int(math.floor((row["completed_at_seconds"] - start) / config.time_bin_seconds + 1e-12)))
        bins[index]["completed_count"] += 1
        bins[index]["slo_met_count"] += int(row["slo_met"] is True)
    for bucket in bins:
        width = bucket["end_seconds"] - bucket["start_seconds"]
        bucket["completion_throughput_rps"] = bucket["completed_count"] / width
        bucket["slo_goodput_rps"] = bucket["slo_met_count"] / width
    summary = {
        "schema_version": 1, "units": {"time": "seconds", "throughput": "requests/second"},
        "source_trace": str(path.resolve()), "run": metadata,
        "observation_end_seconds": observed_until, "evaluation_config": asdict(config),
        "measurement": {"start_seconds": start, "end_seconds": end, "duration_seconds": duration,
                        "interval": "[start, end)", "latency_population": "arrival_cohort_completed"},
        "completion_window": {"completed_count": len(completed), "slo_met_count": passed,
                              "slo_attainment": passed / len(completed) if completed else None,
                              "completion_throughput_rps": len(completed) / duration,
                              "slo_goodput_rps": passed / duration,
                              "carry_in_completed_count": sum(not row["arrival_cohort"] for row in completed)},
        "arrival_cohort": {"arrived_count": len(cohort), "completed_by_observation_end": len(cohort_completed),
                           "failed_count": sum(row["status"] == "failed" for row in cohort),
                           "incomplete_count": sum(row["status"] == "incomplete" for row in cohort),
                           "slo_met_completed_count": cohort_passed,
                           "completed_and_slo_fraction": cohort_passed / len(cohort) if cohort else None,
                           "slo_attainment_among_completed": cohort_passed / len(cohort_completed) if cohort_completed else None},
        "latencies": {name: distribution([row[name] for row in cohort_completed if row[name] is not None], config.percentiles)
                      for name in LATENCY_FIELDS},
        "total_requests_recorded": len(rows), "auxiliary_event_count": auxiliary_count,
        "comparison_tolerance": {"relative": 1e-9, "absolute_seconds": 1e-12},
    }
    return EvaluationResult(summary, rows, token_rows, bins)
