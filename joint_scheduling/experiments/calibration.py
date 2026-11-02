"""Assess completed baseline replays; missing runs never become capacity data."""

import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from ..backend.runtime import read_json


def _same_explicit_fields(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _same_explicit_fields(value, actual[key])
                                               for key, value in expected.items())
    return expected == actual


def _backlog(requests, at):
    return sum(float(row["arrived_at_seconds"]) < at
               and (not row["completed_at_seconds"] or float(row["completed_at_seconds"]) >= at)
               and (not row["failed_at_seconds"] or float(row["failed_at_seconds"]) >= at)
               for row in requests)


def assess_calibration(plan_path, results_root):
    plan_path, results_root = Path(plan_path), Path(results_root)
    plan = read_json(plan_path)
    protocol_path = (plan_path.parent / plan["protocol"]).resolve()
    protocol = read_json(protocol_path)
    rules = protocol["baseline_acceptance"]
    grouped = defaultdict(list)
    trials = []
    seen = set()
    for row in plan["runs"]:
        if row["case"] != "baseline_whole":
            raise ValueError("capacity reference must use baseline_whole only")
        key = row["dataset"], row["target_rps"], row["seed"]
        if key in seen:
            raise ValueError("duplicate calibration trial")
        seen.add(key)
        trial = dict(run_id=row["run_id"], dataset=row["dataset"], target_rps=row["target_rps"], seed=row["seed"])
        run = results_root / row["run_id"]
        summary_path = run / "result/summary.json"
        if not summary_path.exists():
            trial.update(status="pending", accepted=None)
        else:
            for filename, field in (("workload.json", "workload_sha256"), ("compute_profile.json", "profile_sha256")):
                if hashlib.sha256((run/filename).read_bytes()).hexdigest() != row[field]:
                    raise ValueError("completed calibration run does not match planned input: " + row["run_id"])
            resolved = read_json(run/"resolved_config.json")
            expected = read_json(protocol_path.parent/row["config"])
            for block in ("topology", "binding", "kv_scheduler", "reservations", "prefix_cache"):
                if not _same_explicit_fields(expected.get(block, {}), resolved.get(block, {})):
                    raise ValueError("completed calibration run differs from planned " + block)
            for block, excluded in (("runtime", {"workload_path"}), ("feedback", {"compute_profile_path"})):
                fields = {key:value for key,value in expected.get(block, {}).items() if key not in excluded}
                if not _same_explicit_fields(fields,resolved.get(block, {})):
                    raise ValueError("completed calibration run differs from planned " + block)
            effective = resolved.get("effective_baseline", {})
            if effective.get("kv") != "whole_ready_immediate" or effective.get("endpoint_selection") != "round_robin":
                raise ValueError("capacity input was not the planned all-off baseline")
            if any(effective.get(key) for key in ("kv_admission_control", "network_telemetry", "state_observation",
                       "prefix_cache", "collective_protection", "dynamic_inflight", "binding_window",
                       "binding_prediction", "joint_binding_optimization", "native_vidur_batching")):
                raise ValueError("optional module was enabled during baseline calibration")
            summary = read_json(summary_path)
            pressure = summary["pressure"]
            start, end = pressure["measurement_start_seconds"], pressure["measurement_end_seconds"]
            if abs(start-row["warmup_seconds"]) > 1e-9 or abs(end-row["injection_seconds"]) > 1e-8:
                raise ValueError("completed calibration run has a different measurement window")
            if end-start < protocol["minimum_measurement_seconds"]:
                raise ValueError("calibration measurement is shorter than its frozen protocol")
            with (run / "result/requests.csv").open(newline="") as handle:
                requests = list(csv.DictReader(handle))
            if len(requests) != row["request_count"]:
                raise ValueError("request count differs from calibration plan")
            mid = (start+end)/2
            late_arrivals = sum(mid <= float(r["arrived_at_seconds"]) < end for r in requests)
            late_change = _backlog(requests,end)-_backlog(requests,mid)
            growth = max(0,pressure["backlog_change"])/max(1,pressure["measured_arrivals"])
            late_growth = max(0,late_change)/max(1,late_arrivals)
            checks = dict(nonempty_cohort=pressure["cohort_count"] > 0,
                slo_lower_pass=pressure["cohort_slo_fraction_lower"] is not None
                    and pressure["cohort_slo_fraction_lower"] >= rules["minimum_slo_fraction_lower"],
                no_unknown_slos=pressure["cohort_slo_unknown"] == 0,
                no_failed_requests=all(r["status"] != "failed" for r in requests),
                bounded_whole_window_growth=growth <= rules["maximum_positive_backlog_growth_fraction"],
                bounded_late_half_growth=late_growth <= rules["maximum_positive_backlog_growth_fraction"])
            trial.update(status="observed", accepted=all(checks.values()), checks=checks,
                         whole_window_positive_growth_fraction=growth,late_half_positive_growth_fraction=late_growth,
                         slo_lower=pressure["cohort_slo_fraction_lower"],pressure=pressure)
        trials.append(trial)
        grouped[(row["dataset"],row["target_rps"])].append(trial)
    by_dataset = defaultdict(list)
    for (dataset,rate), values in sorted(grouped.items()):
        if set(value["seed"] for value in values) != set(protocol["network_seeds"]):
            raise ValueError("calibration level is missing planned network seeds")
        observed = all(value["status"] == "observed" for value in values)
        by_dataset[dataset].append(dict(target_rps=rate,all_planned_seeds_observed=observed,
            accepted=all(value["accepted"] for value in values) if observed else None,
            seeds=[value["seed"] for value in values]))
    selections = {}
    for dataset, values in by_dataset.items():
        passing = [value["target_rps"] for value in values if value["accepted"] is True]
        selection = dict(status="awaiting_baseline_measurement", measured_reference_rps=None,
                         higher_rejected_rps=None,levels=values)
        if passing:
            low = max(passing)
            rejected_below = any(v["accepted"] is False and v["target_rps"] < low for v in values)
            higher = [v["target_rps"] for v in values if v["accepted"] is False and v["target_rps"] > low]
            if rejected_below:
                selection["status"] = "nonmonotonic_results_need_review"
            elif higher and all(v["accepted"] is not None for v in values if v["target_rps"] <= min(higher)):
                selection.update(status="finite_trace_baseline_bracket",measured_reference_rps=low,
                                 higher_rejected_rps=min(higher))
            elif all(v["accepted"] is not None for v in values):
                selection["status"] = "no_higher_rejected_level_extend_grid"
            else:
                selection["status"] = "partial_results_wait_for_bracket"
        elif values and all(v["accepted"] is False for v in values):
            selection["status"] = "no_passing_level_reduce_load_or_review_slo"
        selections[dataset] = selection
    return dict(status="assessment_only",simulation_started=False,trial_count=len(trials),
                completed_trial_count=sum(t["status"]=="observed" for t in trials),
                selections=selections,trials=trials,
                interpretation="Reference is specific to this finite trace, profile, topology and frozen SLO criteria; network seeds are not independent business samples.")
