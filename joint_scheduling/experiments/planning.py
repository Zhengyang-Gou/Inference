"""Validate and expand experiment specifications without starting execution."""
import copy
import itertools
import json
import re
from dataclasses import asdict
from pathlib import Path

from ..backend.settings import CONTROLLED_KV_MODES
from ..config import load_config
from ..validation import read_json, number
from .parameters import (CONTROL_SWEEPS, allowed_override as _allowed,
                         resolve_input as _resolve, set_override as _set)


def plan_suite(spec_path):
    """Validate suite structure without creating output or starting ns-3."""
    path = Path(spec_path).resolve()
    spec = read_json(path)
    required, optional = {"schema_version", "base_config", "seeds"}, {"workload_spec", "workload", "profile", "workload_seed", "sweep", "cases", "comparison", "common_overrides", "experiment_type"}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - optional:
        raise ValueError("invalid experiment spec fields")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise ValueError("unsupported experiment spec schema")
    if spec.get('experiment_type','end_to_end') not in ('end_to_end','frozen_network_replay'):
        raise ValueError('unknown experiment_type')
    base_path = _resolve(spec["base_config"], path.parent, "base_config")
    config = load_config(base_path)
    base = read_json(base_path)
    base.setdefault("runtime", asdict(config.runtime))
    base.setdefault("evaluation", asdict(config.evaluation))
    base.setdefault("prefix_cache", asdict(config.prefix_cache))
    base["binding"] = dict(asdict(config.binding), **base["binding"])
    common=spec.get('common_overrides',{})
    if not isinstance(common,dict) or any(not isinstance(k,str) or not _allowed(k) or k.startswith('workload.') for k in common):
        raise ValueError('common_overrides must contain implemented configuration fields')
    for key,value in common.items():_set(base,key,value)
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
    if "cases" in spec and "sweep" in spec:
        raise ValueError("choose named cases or a Cartesian sweep, not both")
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
    modes = sweep.get("runtime.kv_transfer_mode", [base['runtime'].get('kv_transfer_mode',config.runtime.kv_transfer_mode)])
    if set(sweep) & CONTROL_SWEEPS and not any(mode in CONTROLLED_KV_MODES for mode in modes):
        raise ValueError("control parameter sweeps require at least one controlled KV mode")
    matrix_size=len(seeds)
    for values in sweep.values():matrix_size*=len(values)
    if matrix_size>10000:
        raise ValueError("experiment matrix exceeds 10,000 runs; split the suite")
    variants=[]
    if "cases" in spec:
        cases=spec["cases"]
        if not isinstance(cases,list) or not cases:
            raise ValueError("named cases must be a nonempty list")
        if len(cases)*len(seeds)>10000:
            raise ValueError("experiment matrix exceeds 10,000 runs; split the suite")
        seen=set()
        for case in cases:
            if not isinstance(case,dict) or set(case)!={"id","overrides"}:
                raise ValueError("named cases require id and overrides")
            identifier=case["id"]
            if not isinstance(identifier,str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}",identifier) or identifier in seen:
                raise ValueError("case IDs must be unique lowercase names")
            seen.add(identifier)
            overrides=case["overrides"]
            if not isinstance(overrides,dict) or any(not isinstance(k,str) or not _allowed(k) for k in overrides):
                raise ValueError("named case contains unsupported overrides")
            if generator is None and any(k.startswith('workload.') for k in overrides):
                raise ValueError('workload overrides require workload_spec')
            variants.append((identifier,copy.deepcopy(overrides)))
    else:
        keys=sorted(sweep)
        variants=[("case-{:04d}".format(index),dict(zip(keys,values)))
                  for index,values in enumerate(itertools.product(*(sweep[key] for key in keys)))]
    size = len(seeds)*len(variants)
    if size > 10000:
        raise ValueError("experiment matrix exceeds 10,000 runs; split the suite")
    runs = []
    for case,parameters in variants:
        for seed in seeds:
            runs.append({"run_id": "{}-seed-{}".format(case, seed), "case_id": case, "seed": seed,
                         "workload_seed": fixed_seed if fixed_seed is not None else seed if generator is not None else None,
                         "parameters": parameters})
    plan={"schema_version": 1, "spec_path": str(path), "source_spec": spec, "base_config": base,
          "profile": profile, "fixed_workload": workload, "generator_spec": generator, "runs": runs}
    if "comparison" in spec:
        from .comparison import audit_comparison
        plan["comparison_audit"]=audit_comparison(plan,spec["comparison"])
    if spec.get('experiment_type')=='frozen_network_replay':
        from .contention import validate_plan
        validate_plan(plan)
    return plan
