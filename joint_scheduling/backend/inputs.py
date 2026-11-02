"""Strict JSON, numeric and workload validation shared by input adapters."""
import json
import math
from pathlib import Path
from ..validation import require_mapping


def read_json(path):
    def bad_constant(value):
        raise ValueError("nonfinite JSON value: " + value)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=bad_constant, object_pairs_hook=unique)


def number(value, name, positive=False, integral=False):
    valid_type = type(value) is int if integral else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("{} must be a finite {}{}".format(name, "positive " if positive else "nonnegative ", "integer" if integral else "number"))
    return value


def load_workload(path):
    raw = require_mapping(read_json(path), "workload")
    return validate_requests(raw)


def validate_requests(raw):
    if raw.get("schema_version") != 1 or not isinstance(raw.get("requests"), list):
        raise ValueError("workload needs schema_version=1 and a requests list")
    provenance = require_mapping(raw.get("provenance"), "workload.provenance")
    if type(provenance.get("synthetic")) is not bool:
        raise ValueError("workload provenance must explicitly declare synthetic")
    requests, ids = [], set()
    allowed = {"request_id", "arrival_seconds", "input_tokens", "output_tokens", "ttft_slo_seconds", "itl_slo_seconds", "prefix_blocks", "max_output_tokens"}
    block_lengths = {}
    for item in raw["requests"]:
        item = require_mapping(item, "workload request")
        if set(item) - allowed or not {"request_id", "arrival_seconds", "input_tokens", "output_tokens"} <= set(item):
            raise ValueError("invalid workload request fields")
        rid = item["request_id"]
        if not isinstance(rid, str) or not rid.strip() or rid in ids or "\x00" in rid:
            raise ValueError("request IDs must be non-empty and unique")
        ids.add(rid)
        number(item["arrival_seconds"], "arrival_seconds")
        number(item["input_tokens"], "input_tokens", True, True)
        number(item["output_tokens"], "output_tokens", True, True)
        if "max_output_tokens" in item:
            number(item["max_output_tokens"], "max_output_tokens", True, True)
            if item["max_output_tokens"] < item["output_tokens"]:
                raise ValueError("max_output_tokens must cover the simulated output_tokens")
        blocks = item.get("prefix_blocks", [])
        if not isinstance(blocks, list):
            raise ValueError("prefix_blocks must be a list")
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {"id", "tokens"} or not isinstance(block["id"], str) or not block["id"].strip():
                raise ValueError("prefix blocks require a nonempty id and tokens")
            number(block["tokens"], "prefix block tokens", True, True)
            if block["id"] in block_lengths and block_lengths[block["id"]] != block["tokens"]:
                raise ValueError("same prefix block ID has inconsistent token lengths")
            block_lengths[block["id"]] = block["tokens"]
        if sum(block["tokens"] for block in blocks) > item["input_tokens"]:
            raise ValueError("prefix blocks exceed input_tokens")
        for name in ("ttft_slo_seconds", "itl_slo_seconds"):
            if name in item:
                number(item[name], name, True)
        requests.append(item)
    return sorted(requests, key=lambda item: (item["arrival_seconds"], item["request_id"])), provenance
