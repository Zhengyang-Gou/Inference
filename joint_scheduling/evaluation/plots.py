"""Matplotlib figures generated solely from exported result directories."""

import csv
import json
import math
from dataclasses import asdict
from pathlib import Path

from .config import EvaluationConfig, parse_config
from .metrics import EvaluationError, quantile


PLOT_METRICS = (("ttft_seconds", "TTFT"), ("handoff_seconds", "First-to-second token gap"),
                ("later_max_itl_seconds", "Later maximum ITL"), ("e2e_seconds", "Request E2E latency"))


def _load(path):
    path = Path(path)
    with (path / "summary.json").open(encoding="utf-8") as handle:
        summary = json.load(handle)
    if summary.get("schema_version") != 1:
        raise EvaluationError("unsupported result schema: {}".format(path))
    with (path / "requests.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"arrival_cohort", "status"} | {name for name, _ in PLOT_METRICS}
        if not required <= set(reader.fieldnames or ()):
            raise EvaluationError("requests.csv is missing metric columns: {}".format(path))
        rows = list(reader)
    series = {}
    for name, _ in PLOT_METRICS:
        values = [float(row[name]) for row in rows
                  if row["arrival_cohort"] == "True" and row["status"] == "completed" and row[name] != ""]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise EvaluationError("invalid latency value in {}".format(path))
        series[name] = values
    with (path / "timeseries.csv").open(encoding="utf-8", newline="") as handle:
        timeseries = [{key: float(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    run = summary["run"]
    synthetic = run.get("metadata", {}).get("synthetic", False)
    label = "{} (seed {}){}".format(run["strategy"], run["seed"], " [synthetic]" if synthetic else "")
    return {"summary": summary, "series": series, "timeseries": timeseries, "label": label}


def _style_axis(axis, ylabel):
    axis.set_ylabel(ylabel)
    axis.grid(True, axis="y", alpha=0.25)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def _empty(axis):
    axis.text(0.5, 0.5, "No applicable completed samples", ha="center", va="center", transform=axis.transAxes)


def _latency_cdf(plt, runs, config):
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), layout="constrained")
    for axis, (metric, title) in zip(axes.flat, PLOT_METRICS):
        any_data = False
        for run in runs:
            values = sorted(value * 1000 for value in run["series"][metric])
            if values:
                any_data = True
                axis.step([values[0]] + values, [0.0] + [(i + 1) / len(values) for i in range(len(values))],
                          where="post", label=run["label"])
        axis.set_title(title)
        axis.set_xlabel("Latency (ms)")
        axis.set_ylim(0, 1.03)
        _style_axis(axis, "Empirical CDF")
        if any_data:
            axis.legend(fontsize=7, loc="lower right")
        else:
            _empty(axis)
    fig.suptitle("Latency distributions | completed requests from the arrival cohort", fontsize=12)
    return fig


def _latency_percentiles(plt, runs, config):
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), layout="constrained")
    width = 0.8 / len(runs)
    for axis, (metric, title) in zip(axes.flat, PLOT_METRICS):
        any_data = False
        for index, run in enumerate(runs):
            values = [quantile(run["series"][metric], q) for q in config.percentiles]
            if any(value is not None for value in values):
                any_data = True
            positions = [i - 0.4 + width * (index + 0.5) for i in range(len(values))]
            axis.bar(positions, [value * 1000 if value is not None else float("nan") for value in values],
                     width, label=run["label"])
        axis.set_xticks(range(len(config.percentiles)))
        axis.set_xticklabels(["P{:g}".format(q * 100) for q in config.percentiles])
        axis.set_title(title)
        _style_axis(axis, "Latency (ms)")
        if any_data:
            axis.legend(fontsize=7)
        else:
            _empty(axis)
    fig.suptitle("Latency percentiles | missing intervals are excluded", fontsize=12)
    return fig


def _slo_throughput(plt, runs, config):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained")
    labels = [run["label"] for run in runs]
    data = [run["summary"]["completion_window"] for run in runs]
    attainment = [row["slo_attainment"] for row in data]
    axes[0].bar(range(len(runs)), [value * 100 if value is not None else float("nan") for value in attainment], color="#3274a1")
    for index, value in enumerate(attainment):
        if value is None:
            axes[0].text(index, 2, "N/A", ha="center")
    axes[0].set_ylim(0, 105)
    axes[0].set_title("All applicable SLOs met")
    _style_axis(axes[0], "Completed requests (%)")
    axes[1].bar([i - 0.2 for i in range(len(runs))], [row["completion_throughput_rps"] for row in data],
                0.4, label="Completed")
    axes[1].bar([i + 0.2 for i in range(len(runs))], [row["slo_goodput_rps"] for row in data],
                0.4, label="Completed and SLO-compliant")
    axes[1].legend(fontsize=8)
    axes[1].set_title("Completion throughput and SLO goodput")
    _style_axis(axes[1], "Requests / second")
    for axis in axes:
        axis.set_xticks(range(len(labels)))
        axis.set_xticklabels(labels, rotation=15, ha="right", fontsize=8)
    fig.suptitle("Requests completed within each measurement window", fontsize=12)
    return fig


def _throughput_timeseries(plt, runs, config):
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), layout="constrained")
    for axis, metric, title in zip(axes, ("completion_throughput_rps", "slo_goodput_rps"),
                                    ("Completion throughput", "SLO-compliant completion throughput")):
        for run in runs:
            buckets = run["timeseries"]
            if buckets:
                x = [bucket["start_seconds"] for bucket in buckets] + [buckets[-1]["end_seconds"]]
                y = [bucket[metric] for bucket in buckets] + [buckets[-1][metric]]
                axis.step(x, y, where="post", label=run["label"])
        axis.set_title(title)
        axis.set_xlabel("Simulation time (seconds)")
        axis.set_ylim(bottom=0)
        _style_axis(axis, "Requests / second")
        axis.legend(fontsize=8)
    fig.suptitle("Time bins use their actual widths, including the final partial bin", fontsize=12)
    return fig


def plot_results(result_dirs, output_dir, config=None):
    """Plot each run separately; seeds are not silently pooled or averaged."""
    config = config or EvaluationConfig()
    raw = asdict(config)
    for name in ("percentiles", "plot_formats", "charts"):
        raw[name] = list(raw[name])
    config = parse_config(raw)
    if not result_dirs:
        raise EvaluationError("at least one result directory is required")
    runs = [_load(path) for path in result_dirs]
    if len({run["label"] for run in runs}) != len(runs):
        raise EvaluationError("duplicate strategy/seed labels; give each run a distinct strategy or seed")
    windows = {(run["summary"]["measurement"]["start_seconds"], run["summary"]["measurement"]["end_seconds"])
               for run in runs}
    if len(windows) != 1:
        raise EvaluationError("comparison plots require matching measurement windows")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise EvaluationError("Plotting needs matplotlib; install joint_scheduling/evaluation/requirements-plot.txt") from error
    output = Path(output_dir).resolve()
    targets = [output / (chart + "." + suffix) for chart in config.charts for suffix in config.plot_formats]
    if any(path.exists() for path in targets):
        raise FileExistsError("one or more figure files already exist in {}".format(output))
    output.mkdir(parents=True, exist_ok=True)
    functions = {"latency_cdf": _latency_cdf, "latency_percentiles": _latency_percentiles,
                 "slo_throughput": _slo_throughput, "throughput_timeseries": _throughput_timeseries}
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 11,
                         "axes.prop_cycle": matplotlib.cycler(color=["#3274a1", "#e1812c", "#3a923a", "#c03d3e", "#9467bd"])}):
        for chart in config.charts:
            fig = functions[chart](plt, runs, config)
            try:
                for suffix in config.plot_formats:
                    fig.savefig(output / (chart + "." + suffix), dpi=config.plot_dpi, bbox_inches="tight")
            finally:
                plt.close(fig)
    return targets
