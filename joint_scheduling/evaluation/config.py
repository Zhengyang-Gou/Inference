"""Evaluation settings. All measurements use seconds; plots show milliseconds."""

import math
from dataclasses import dataclass, field, replace
from typing import Tuple

from ..validation import ConfigError, read_settings, require_mapping


CHARTS = ("latency_cdf", "latency_percentiles", "slo_throughput", "throughput_timeseries")


@dataclass(frozen=True)
class EvaluationConfig:
    window_start_seconds: float = field(default=0.0, metadata={"min": 0})
    window_end_seconds: float = field(default=10.0, metadata={"gt": 0})
    time_bin_seconds: float = field(default=1.0, metadata={"gt": 0})
    default_ttft_slo_seconds: float = field(default=1.0, metadata={"gt": 0})
    default_itl_slo_seconds: float = field(default=0.1, metadata={"gt": 0})
    output_dir: str = "../artifacts/evaluation"
    plot_dpi: int = field(default=160, metadata={"min": 72})
    percentiles: Tuple[float, ...] = field(default=(0.5, 0.95, 0.99), metadata={"internal": True})
    plot_formats: Tuple[str, ...] = field(default=("png", "pdf"), metadata={"internal": True})
    charts: Tuple[str, ...] = field(default=CHARTS, metadata={"internal": True})


def parse_config(raw):
    raw = require_mapping(raw, "evaluation")
    defaults = EvaluationConfig()
    percentiles = raw.pop("percentiles", list(defaults.percentiles))
    formats = raw.pop("plot_formats", list(defaults.plot_formats))
    charts = raw.pop("charts", list(defaults.charts))
    config = read_settings(EvaluationConfig, raw, "evaluation")
    if config.window_end_seconds <= config.window_start_seconds:
        raise ConfigError("evaluation.window_end_seconds must be > window_start_seconds")
    duration = config.window_end_seconds - config.window_start_seconds
    if duration / config.time_bin_seconds > 1000000:
        raise ConfigError("evaluation: more than 1,000,000 time bins; increase time_bin_seconds")
    if not config.output_dir.strip():
        raise ConfigError("evaluation.output_dir must not be empty")
    if not isinstance(percentiles, list) or not percentiles:
        raise ConfigError("evaluation.percentiles must be a non-empty list")
    if any(type(q) not in (int, float) or not math.isfinite(q) or not 0 <= q <= 1 for q in percentiles):
        raise ConfigError("evaluation.percentiles must contain finite numbers in [0, 1]")
    if percentiles != sorted(set(percentiles)):
        raise ConfigError("evaluation.percentiles must be unique and ascending")
    for name, values, allowed in (("plot_formats", formats, ("png", "pdf", "svg")),
                                  ("charts", charts, CHARTS)):
        if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
            raise ConfigError("evaluation.{} must be a non-empty list of names".format(name))
        if len(set(values)) != len(values) or any(v not in allowed for v in values):
            raise ConfigError("evaluation.{} must contain unique values from {}".format(name, allowed))
    return replace(config, percentiles=tuple(percentiles), plot_formats=tuple(formats), charts=tuple(charts))
