"""Run the existing baseline over a reproducible Cartesian parameter matrix."""

import copy
import csv
import hashlib
import itertools
import json
import platform
import statistics
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from ..backend.runtime import read_json, number
from ..config import load_config
from ..workload import write_workload


METRICS = ("arrived_count", "completed_count", "failed_count", "incomplete_count", "slo_attainment",
           "completion_throughput_rps", "slo_goodput_rps", "ttft_mean_seconds", "ttft_p95_seconds",
           "handoff_p95_seconds", "later_itl_p95_seconds", "e2e_p95_seconds")
COLUMNS = ("run_id", "case_id", "seed", "workload_seed", "status", "parameters", "output_dir",
           "error", "wall_seconds", "workload_sha256", "profile_sha256") + METRICS


def _write(path, data):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _resolve(value, parent, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a nonempty path".format(label))
    path = Path(value)
    path = (parent / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise ValueError("{} does not exist: {}".format(label, path))
    return str(path)


def _allowed(key):
    # Reject inert policy/interface knobs: every accepted sweep affects an
    # implemented part of this baseline or its evaluation/observation output.
    return (key.startswith("topology.") and key.count(".") == 1
            or key.startswith("workload.") and len(key.split(".")) >= 2
            or key in {"runtime.time_limit_seconds", "runtime.congestion_control", "runtime.packet_payload_bytes",
                       "runtime.switch_buffer_bytes", "runtime.rpc_timeout_seconds", "feedback.observation_delay_us",
                       "reservations.capacity_safety_fraction"}
            or key.startswith("evaluation.") and key.count(".") == 1 and key != "evaluation.output_dir")


def _set(root, dotted, value):
    keys = dotted.split(".")
    for key in keys[:-1]:
        if key not in root or not isinstance(root[key], dict):
            raise ValueError("override requires an object at: {}".format(dotted))
        root = root[key]
    root[keys[-1]] = copy.deepcopy(value)


def plan_suite(spec_path):
    """Validate suite structure without creating output or starting ns-3."""
    path = Path(spec_path).resolve()
    spec = read_json(path)
    required, optional = {"schema_version", "base_config", "seeds"}, {"workload_spec", "workload", "profile", "workload_seed", "sweep"}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - optional:
        raise ValueError("invalid experiment spec fields")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise ValueError("unsupported experiment spec schema")
    base_path = _resolve(spec["base_config"], path.parent, "base_config")
    config = load_config(base_path)
    base = read_json(base_path)
    base.setdefault("runtime", asdict(config.runtime))
    base.setdefault("evaluation", asdict(config.evaluation))
    profile = _resolve(spec["profile"], path.parent, "profile") if "profile" in spec else str(config.profile_path().resolve())
    if ("profile" not in spec and not config.feedback.compute_profile_path) or not Path(profile).is_file():
        raise ValueError("experiment requires an existing compute profile")
    base["feedback"]["compute_profile_path"] = profile
    if "workload" in spec and "workload_spec" in spec:
        raise ValueError("choose workload or workload_spec, not both")
    generator = None
    if "workload_spec" in spec:
        generator = read_json(_resolve(spec["workload_spec"], path.parent, "workload_spec"))
        workload = None
    else:
        workload = _resolve(spec["workload"], path.parent, "workload") if "workload" in spec else _resolve(
            config.runtime.workload_path, Path(base_path).parent, "base config workload")
    seeds = spec["seeds"]
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("seeds must be nonempty")
    for seed in seeds:
        number(seed, "seed", integral=True)
        if seed >= 2147483647:
            raise ValueError("seed must be below 2147483647")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    fixed_seed = spec.get("workload_seed")
    if fixed_seed is not None:
        number(fixed_seed, "workload_seed", integral=True)
        if fixed_seed >= 2147483647 or generator is None:
            raise ValueError("workload_seed requires a generator and a seed below 2147483647")
    sweep = spec.get("sweep", {})
    if not isinstance(sweep, dict):
        raise ValueError("sweep must be an object of parameter/value-list pairs")
    for key, values in sweep.items():
        if not isinstance(key, str) or not _allowed(key) or any(not part for part in key.split(".")):
            raise ValueError("unsupported or inactive baseline sweep: {}".format(key))
        if key.startswith("workload.") and generator is None:
            raise ValueError("workload sweeps require workload_spec")
        if not isinstance(values, list) or not values:
            raise ValueError("sweep values must be nonempty lists")
        signatures = [json.dumps(value, sort_keys=True, allow_nan=False) for value in values]
        if len(set(signatures)) != len(values):
            raise ValueError("duplicate sweep values: {}".format(key))
    size = len(seeds)
    for values in sweep.values():
        size *= len(values)
    if size > 10000:
        raise ValueError("experiment matrix exceeds 10,000 runs; split the suite")
    keys = sorted(sweep)
    runs = []
    for index, values in enumerate(itertools.product(*(sweep[key] for key in keys))):
        parameters = dict(zip(keys, values))
        for seed in seeds:
            case = "case-{:04d}".format(index)
            runs.append({"run_id": "{}-seed-{}".format(case, seed), "case_id": case, "seed": seed,
                         "workload_seed": fixed_seed if fixed_seed is not None else seed if generator is not None else None,
                         "parameters": parameters})
    return {"schema_version": 1, "spec_path": str(path), "source_spec": spec, "base_config": base,
            "profile": profile, "fixed_workload": workload, "generator_spec": generator, "runs": runs}


def _metrics(summary):
    completion, cohort = summary["completion_window"], summary["arrival_cohort"]
    result = {key: completion[key] for key in ("completed_count", "slo_attainment", "completion_throughput_rps", "slo_goodput_rps")}
    result.update({key: cohort[key] for key in ("arrived_count", "failed_count", "incomplete_count")})
    result["ttft_mean_seconds"] = summary["latencies"]["ttft_seconds"]["mean"]
    for target, name in (("ttft", "ttft_seconds"), ("handoff", "handoff_seconds"),
                         ("later_itl", "later_max_itl_seconds"), ("e2e", "e2e_seconds")):
        result[target + "_p95_seconds"] = summary["latencies"][name]["quantiles"].get("0.95")
    return result


def _aggregate(rows):
    groups = {}
    for row in rows:
        group = groups.setdefault(row["case_id"], {"case_id": row["case_id"], "parameters": row["parameters"],
                                                  "successful_runs": 0, "failed_runs": 0, "skipped_runs": 0, "metrics": {}})
        group[{"completed": "successful_runs", "failed": "failed_runs", "skipped": "skipped_runs"}[row["status"]]] += 1
    for case_id, group in groups.items():
        successful = [row for row in rows if row["case_id"] == case_id and row["status"] == "completed"]
        for metric in METRICS:
            values = [row[metric] for row in successful if row.get(metric) is not None]
            group["metrics"][metric] = {"samples": len(values), "mean": statistics.mean(values) if values else None,
                                        "sample_stddev": statistics.stdev(values) if len(values) > 1 else None}
    return list(groups.values())


def run_suite(spec_path, output_dir, make_plots=False, fail_fast=False, progress=None):
    from ..backend.runner import run_baseline
    plan = plan_suite(spec_path)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "plan.json", plan)
    _write(output / "environment.json", {"python": sys.version, "python_executable": sys.executable,
                                        "platform": platform.platform()})
    inputs = output / "inputs"
    inputs.mkdir()
    (inputs / "compute_profile.json").write_bytes(Path(plan["profile"]).read_bytes())
    if plan["fixed_workload"] is not None:
        (inputs / "workload.json").write_bytes(Path(plan["fixed_workload"]).read_bytes())
    if plan["generator_spec"] is not None:
        _write(inputs / "workload_spec.json", plan["generator_spec"])
    rows, stopped = [], False
    with (output / "runs.jsonl").open("x", encoding="utf-8") as journal:
        for planned in plan["runs"]:
            row = dict(planned, output_dir="runs/" + planned["run_id"], status="skipped" if stopped else "failed")
            began = time.monotonic()
            if not stopped:
                case_dir = output / row["output_dir"]
                case_dir.mkdir(parents=True)
                try:
                    raw = copy.deepcopy(plan["base_config"])
                    raw["feedback"]["compute_profile_path"] = str(inputs / "compute_profile.json")
                    generator = copy.deepcopy(plan["generator_spec"])
                    for key, value in row["parameters"].items():
                        if key.startswith("workload."):
                            _set(generator, key[len("workload."):], value)
                        else:
                            _set(raw, key, value)
                    raw.setdefault("runtime", {})["seed"] = row["seed"]
                    if generator is not None:
                        write_workload(generator, case_dir / "workload.json", row["workload_seed"])
                    else:
                        (case_dir / "workload.json").write_bytes((inputs / "workload.json").read_bytes())
                    raw["runtime"]["workload_path"] = str(case_dir / "workload.json")
                    _write(case_dir / "config.json", raw)
                    config = load_config(case_dir / "config.json")
                    result = run_baseline(config, case_dir / "run", make_plots=make_plots)
                    row.update(_metrics(result.summary), status="completed")
                    row["workload_sha256"] = hashlib.sha256((case_dir / "run/workload.json").read_bytes()).hexdigest()
                    row["profile_sha256"] = hashlib.sha256((case_dir / "run/compute_profile.json").read_bytes()).hexdigest()
                    row["measurement"] = result.summary["measurement"]
                    row["effective_baseline"] = result.summary["run"]["metadata"]["effective_baseline"]
                    row["synthetic"] = result.summary["run"]["metadata"]["synthetic"]
                except Exception as error:
                    row.update(status="failed", error="{}: {}".format(type(error).__name__, error))
                    _write(case_dir / "error.json", dict(row, traceback=traceback.format_exc()))
                    stopped = fail_fast
            else:
                row["error"] = "not attempted after an earlier failure (--fail-fast)"
            row["wall_seconds"] = time.monotonic() - began
            rows.append(row)
            journal.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            journal.flush()
            if progress:
                progress(row)
    with (output / "runs.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row, parameters=json.dumps(row["parameters"], sort_keys=True)))
    report = {"schema_version": 1, "status": "completed_with_failures" if any(row["status"] != "completed" for row in rows) else "completed",
              "total_runs": len(rows), "successful_runs": sum(row["status"] == "completed" for row in rows),
              "failed_runs": sum(row["status"] == "failed" for row in rows),
              "skipped_runs": sum(row["status"] == "skipped" for row in rows), "cases": _aggregate(rows),
              "semantics": {"engine": "existing round-robin/FIFO, whole-KV baseline; no joint optimization",
                "aggregation": "unweighted mean and sample standard deviation across successful seeds per case",
                "null_metrics": "no valid samples; never replaced with zero", "failures": "excluded from metric means but explicitly counted",
                "request_counts": "arrived/failed/incomplete use arrival cohort; completed uses completion window",
                "latencies": "arrival-cohort completed requests; seconds; p95 null if not configured",
                "wall_seconds": "execution duration, not simulated latency"}}
    _write(output / "summary.json", report)
    with (output / "comparison.csv").open("x", encoding="utf-8", newline="") as handle:
        fields = ("case_id", "parameters", "successful_runs", "failed_runs", "skipped_runs") + tuple(
            metric + suffix for metric in METRICS for suffix in ("_samples", "_mean", "_sample_stddev"))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in report["cases"]:
            row = {key: group[key] for key in fields[:5]}
            row["parameters"] = json.dumps(row["parameters"], sort_keys=True)
            for metric, values in group["metrics"].items():
                row.update({metric + "_" + key: value for key, value in values.items()})
            writer.writerow(row)
    return report
