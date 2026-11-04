"""Freeze experiment inputs, schedule independent runs and aggregate results."""

import csv
import hashlib
import json
import platform
import statistics
import sys
from pathlib import Path

from .planning import plan_suite
from .parameters import (CONTROL_SWEEPS, allowed_override as _allowed,
                         resolve_input as _resolve, set_override as _set)
from .metrics import METRICS, COLUMNS, extract_metrics as _metrics
from .execution import RunTask, execute_run, freeze_binding_plan
from .parallel import execution_options, run_tasks


def _write(path, data):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


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


def run_suite(spec_path, output_dir, make_plots=False, fail_fast=False, progress=None, *, jobs=1):
    execution_options(jobs, 0)
    plan = plan_suite(spec_path)
    execution = execution_options(jobs, len(plan["runs"]), fail_fast)
    if plan['source_spec'].get('experiment_type')=='frozen_network_replay':
        from .contention import run_contention
        return run_contention(plan, output_dir, progress=progress, execution=execution)
    frozen_profile = Path(plan["profile"]).read_bytes()
    frozen_workload = Path(plan["fixed_workload"]).read_bytes() if plan["fixed_workload"] is not None else None
    if 'comparison_audit' in plan:
        # Audit the exact bytes that will be copied, even if a source file was
        # changed between planning and freezing the suite inputs.
        for name, contents in (("profile", frozen_profile), ("workload", frozen_workload)):
            if hashlib.sha256(contents).hexdigest() != plan['comparison_audit']['input_sha256'][name]:
                raise ValueError('comparison '+name+' changed before input freeze')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "plan.json", plan)
    _write(output / "execution.json", execution)
    if 'comparison_audit' in plan:
        _write(output / 'comparison_audit.json',plan['comparison_audit'])
    _write(output / "environment.json", {"python": sys.version, "python_executable": sys.executable,
                                        "platform": platform.platform()})
    inputs = output / "inputs"
    inputs.mkdir()
    from .provenance import freeze_source
    source_snapshot = freeze_source(inputs/'source', (output,))
    (inputs / "compute_profile.json").write_bytes(frozen_profile)
    if plan["fixed_workload"] is not None:
        (inputs / "workload.json").write_bytes(frozen_workload)
    if plan["generator_spec"] is not None:
        _write(inputs / "workload_spec.json", plan["generator_spec"])
    binding_plan_path = freeze_binding_plan(plan, inputs)
    tasks = [RunTask(planned, plan['base_config'], plan['generator_spec'], str(output),
                     str(source_snapshot), make_plots, binding_plan_path)
             for planned in plan['runs']]
    completed_rows = {}
    with (output / "runs.jsonl").open("x", encoding="utf-8") as journal:
        for index, row in run_tasks(tasks, jobs, fail_fast, worker=execute_run):
            completed_rows[index] = row
            journal.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            journal.flush()
            if progress:
                progress(row)
    rows = [completed_rows[index] for index in range(len(tasks))]
    with (output / "runs.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row, parameters=json.dumps(row["parameters"], sort_keys=True)))
    report = {"schema_version": 1, "experiment_type": "end_to_end", "status": "completed_with_failures" if any(row["status"] != "completed" for row in rows) else "completed",
              "execution": execution, "total_runs": len(rows), "successful_runs": sum(row["status"] == "completed" for row in rows),
              "failed_runs": sum(row["status"] == "failed" for row in rows),
              "skipped_runs": sum(row["status"] == "skipped" for row in rows), "cases": _aggregate(rows),
              "semantics": {"engine": "per-run effective_baseline records round-robin/balanced-random/joint binding, FIFO/chunked/packed Prefill, continuous Decode batching and explicit KV mode",
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
    from .provenance import write_suite_report
    write_suite_report(output,report)
    return report
