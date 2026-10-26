"""Generate arrivals and correlated request classes without generating results."""

import copy
import json
import math
import random
from collections import Counter
from pathlib import Path

from ..backend.runtime import number


def _keys(value, required, optional, label):
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("{} requires {} and only optional {}".format(label, sorted(required), sorted(optional)))


def _weights(values, label):
    for value in values:
        number(value, label, positive=True)
    if not math.isfinite(sum(values)):
        raise ValueError("{} sum must be finite".format(label))


def _distribution(value, label):
    if type(value) is int:
        number(value, label, positive=True, integral=True)
        return lambda rng: value
    if not isinstance(value, dict):
        raise ValueError("{} must be a positive integer or distribution".format(label))
    kind = value.get("type")
    if kind == "uniform":
        _keys(value, {"type", "min", "max"}, set(), label)
        low = number(value["min"], label + ".min", positive=True, integral=True)
        high = number(value["max"], label + ".max", positive=True, integral=True)
        if low > high:
            raise ValueError("{} min exceeds max".format(label))
        return lambda rng: rng.randint(low, high)
    if kind == "choice":
        _keys(value, {"type", "values"}, {"weights"}, label)
        values = value["values"]
        if not isinstance(values, list) or not values:
            raise ValueError("{} values must be nonempty".format(label))
        for item in values:
            number(item, label + ".values", positive=True, integral=True)
        weights = value.get("weights", [1.0] * len(values))
        if not isinstance(weights, list) or len(weights) != len(values):
            raise ValueError("{} weights must match values".format(label))
        _weights(weights, label + ".weights")
        return lambda rng: rng.choices(values, weights=weights, k=1)[0]
    raise ValueError("{} distribution must be uniform or choice".format(label))


def generate_workload(spec, seed=None):
    """Return a validated workload; arrival RNG and request-class RNG are separate.

    Changing only an arrival rate or network configuration does not change the
    sampled request lengths/SLOs for a given seed and class specification.
    Exact output lengths are simulator inputs, not an online prediction oracle.
    """
    spec = copy.deepcopy(spec)
    _keys(spec, {"schema_version", "arrival", "classes"}, {"seed", "description"}, "workload spec")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise ValueError("unsupported workload spec schema")
    seed = spec.get("seed", 7) if seed is None else seed
    number(seed, "seed", integral=True)
    if seed >= 2147483647:
        raise ValueError("seed must be below 2147483647")
    spec["seed"] = seed
    if "description" in spec and not isinstance(spec["description"], str):
        raise ValueError("description must be a string")
    arrival = spec["arrival"]
    _keys(arrival, {"pattern", "count"},
          {"start_seconds", "rate_rps", "burst_size", "burst_interval_seconds", "spread_seconds"}, "arrival")
    count = number(arrival["count"], "arrival.count", integral=True)
    if count > 1000000:
        raise ValueError("arrival.count exceeds 1,000,000 requests")
    start = number(arrival.get("start_seconds", 0.0), "arrival.start_seconds")
    pattern = arrival["pattern"]
    times, arrival_rng = [], random.Random("{}:arrivals".format(seed))
    if pattern in ("periodic", "poisson"):
        if set(arrival) & {"burst_size", "burst_interval_seconds", "spread_seconds"}:
            raise ValueError("burst fields require the burst arrival pattern")
        rate = number(arrival.get("rate_rps"), "arrival.rate_rps", positive=True)
        time = start
        for index in range(count):
            if pattern == "periodic":
                time = start + index / rate
            else:
                time += arrival_rng.expovariate(rate)
            times.append(time)
    elif pattern == "burst":
        if "rate_rps" in arrival:
            raise ValueError("burst uses burst_size/interval instead of rate_rps")
        size = number(arrival.get("burst_size"), "arrival.burst_size", positive=True, integral=True)
        interval = number(arrival.get("burst_interval_seconds"), "arrival.burst_interval_seconds", positive=True)
        spread = number(arrival.get("spread_seconds", 0.0), "arrival.spread_seconds")
        if spread >= interval:
            raise ValueError("burst spread must be smaller than interval")
        times = [start + (index // size) * interval + arrival_rng.random() * spread for index in range(count)]
        times.sort()
    else:
        raise ValueError("arrival.pattern must be periodic, poisson or burst")
    if any(not math.isfinite(time) or time * 1e9 >= 2**63 for time in times):
        raise ValueError("arrival times exceed the supported simulation clock")

    classes = spec["classes"]
    if not isinstance(classes, list) or not classes:
        raise ValueError("classes must be a nonempty list")
    samplers, names, class_weights = [], set(), []
    for item in classes:
        _keys(item, {"name", "weight", "input_tokens", "output_tokens"},
              {"ttft_slo_seconds", "itl_slo_seconds"}, "request class")
        name = item["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("request class names must be nonempty and unique")
        names.add(name)
        class_weights.append(item["weight"])
        samplers.append((_distribution(item["input_tokens"], name + ".input_tokens"),
                         _distribution(item["output_tokens"], name + ".output_tokens")))
        for key in ("ttft_slo_seconds", "itl_slo_seconds"):
            if key in item:
                number(item[key], key, positive=True)
    _weights(class_weights, "class weights")
    rng, requests, counts = random.Random("{}:requests".format(seed)), [], Counter()
    for index, time in enumerate(times):
        choice = rng.choices(range(len(classes)), weights=class_weights, k=1)[0]
        item = classes[choice]
        inputs, outputs = samplers[choice]
        row = {"request_id": "req-{:06d}".format(index), "arrival_seconds": round(time, 9),
               "input_tokens": inputs(rng), "output_tokens": outputs(rng)}
        for key in ("ttft_slo_seconds", "itl_slo_seconds"):
            if key in item:
                row[key] = item[key]
        requests.append(row)
        counts[item["name"]] += 1
    return {"schema_version": 1, "provenance": {
        "synthetic": True, "description": spec.get("description", "Generated synthetic request workload"),
        "generator": "inference-workload-v1", "seed": seed, "generator_spec": spec,
        "class_counts": dict(sorted(counts.items()))}, "requests": requests}


def write_workload(spec, output, seed=None):
    workload = generate_workload(spec, seed)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(workload, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    return workload
