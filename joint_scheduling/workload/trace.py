"""Import public arrival/length traces without clipping or inventing completions."""

import copy
import csv
import hashlib
import json
import re
from calendar import timegm
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
from pathlib import Path

from ..backend.runtime import number, validate_requests


def _nanoseconds(value, units_per_second=1):
    try:
        result = Decimal(str(value)) * Decimal(10**9) / units_per_second
        if not result.is_finite() or result < 0 or result >= 2**63:
            raise ValueError("trace time is outside the nonnegative nanosecond clock")
        return int(result.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError) as error:
        raise ValueError("trace time must be a finite number") from error


def _azure_timestamp(value):
    match = re.fullmatch(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?", value)
    if not match:
        raise ValueError("Azure TIMESTAMP must be a timezone-naive date with up to nine fractional digits")
    whole = datetime.strptime(match[1].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    return timegm(whole.timetuple()) * 10**9 + int((match[2] or "").ljust(9, "0"))


def _positive_integer(value, label):
    if type(value) is int:
        return number(value, label, positive=True, integral=True)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return number(int(value), label, positive=True, integral=True)
    raise ValueError("{} must be a positive integer; failed/empty responses require explicit preprocessing".format(label))


def read_trace(path, trace_format, source_url="", expected_sha256=None, source_synthetic=False):
    """Return ordered records plus source identity; retain source row identities.

    Mooncake hashes stay in the source file. Importing a trace never enables a
    cache, creates sessions, or subtracts reused tokens from input lengths.
    """
    path = Path(path)
    if type(source_synthetic) is not bool:
        raise ValueError("source_synthetic must be explicitly boolean")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("trace SHA-256 does not match the frozen source")
    records = []
    if trace_format == "azure":
        rows = csv.DictReader(data.decode("utf-8-sig").splitlines())
        if len(rows.fieldnames or []) != 3 or set(rows.fieldnames or []) != {"TIMESTAMP", "ContextTokens", "GeneratedTokens"}:
            raise ValueError("Azure trace requires TIMESTAMP,ContextTokens,GeneratedTokens")
        for index, row in enumerate(rows):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Azure row does not match its three-column schema")
            records.append(dict(source_index=index, source_time_ns=_azure_timestamp(row["TIMESTAMP"]),
                                input_tokens=_positive_integer(row["ContextTokens"], "ContextTokens"),
                                output_tokens=_positive_integer(row["GeneratedTokens"], "GeneratedTokens")))
    elif trace_format == "mooncake":
        for index, line in enumerate(data.decode("utf-8").splitlines()):
            if not line.strip():
                raise ValueError("empty JSONL record at line {}".format(index + 1))
            row = json.loads(line, parse_float=Decimal)
            if not isinstance(row, dict) or not {"timestamp", "input_length", "output_length", "hash_ids"} <= set(row):
                raise ValueError("Mooncake record is missing timestamp/length/hash fields")
            if type(row["timestamp"]) is bool:
                raise ValueError("timestamp must be numeric")
            records.append(dict(source_index=index, source_time_ns=_nanoseconds(row["timestamp"], 1000),
                                input_tokens=_positive_integer(row["input_length"], "input_length"),
                                output_tokens=_positive_integer(row["output_length"], "output_length")))
    else:
        raise ValueError("trace format must be azure or mooncake")
    if not records:
        raise ValueError("trace must contain at least one request")
    records.sort(key=lambda row: (row["source_time_ns"], row["source_index"]))
    origin = records[0]["source_time_ns"]
    for row in records:
        row["source_time_ns"] -= origin
    provenance = dict(source_file=path.name, source_url=source_url, source_sha256=digest,
                      source_format=trace_format, source_request_count=len(records),
                      source_origin_ns=origin, source_duration_ns=records[-1]["source_time_ns"] + 1,
                      source_synthetic=source_synthetic,
                      source_lengths="unchanged paired observations", clipped_requests=0, dropped_requests=0,
                      session_dependencies_modeled=False, prefix_reuse_enabled_by_import=False)
    return dict(records=records, provenance=provenance)


def scale_trace(trace, target_rps=None, *, request_prefix="trace", start_seconds=0,
                duration_seconds=None, ttft_slo_seconds=None, itl_slo_seconds=None):
    """Scale the full time axis, optionally selecting an explicit injection prefix.

    The source interval is [first arrival,last arrival+1ns). Integer arithmetic
    maps it to a half-open target interval, so the final arrival is never lost
    to floating-point rounding at the right endpoint. ``duration_seconds``
    selects a reported prefix; it never clips request token lengths.
    """
    provenance = copy.deepcopy(trace["provenance"])
    records = trace["records"]
    number(start_seconds, "start_seconds")
    start_ns = _nanoseconds(start_seconds)
    source_duration = provenance["source_duration_ns"]
    if target_rps is None:
        scaled_duration = source_duration
    else:
        number(target_rps, "target_rps", positive=True)
        ratio = Fraction(str(target_rps))
        desired = Fraction(len(records) * 10**9, 1) / ratio
        scaled_duration = (desired.numerator + desired.denominator - 1) // desired.denominator
    if scaled_duration <= 0 or scaled_duration + start_ns >= 2**63:
        raise ValueError("scaled trace exceeds the supported nanosecond clock")
    if duration_seconds is None:
        injection_ns = scaled_duration
    else:
        number(duration_seconds, "duration_seconds", positive=True)
        injection_ns = _nanoseconds(duration_seconds)
        if injection_ns > scaled_duration:
            raise ValueError("requested duration exceeds this trace; provide more data instead of implicitly repeating it")
    for name, value in (("ttft_slo_seconds", ttft_slo_seconds), ("itl_slo_seconds", itl_slo_seconds)):
        if value is not None:
            number(value, name, positive=True)
    if not isinstance(request_prefix, str) or not request_prefix.strip():
        raise ValueError("request_prefix must be nonempty")
    requests, source_indices = [], []
    for row in records:
        at_ns = row["source_time_ns"] * scaled_duration // source_duration
        if at_ns >= injection_ns:
            break
        item = dict(request_id="{}-{:09d}".format(request_prefix, row["source_index"]),
                    arrival_seconds=(start_ns + at_ns) / 1e9,
                    input_tokens=row["input_tokens"], output_tokens=row["output_tokens"])
        if ttft_slo_seconds is not None:
            item["ttft_slo_seconds"] = ttft_slo_seconds
        if itl_slo_seconds is not None:
            item["itl_slo_seconds"] = itl_slo_seconds
        requests.append(item)
        source_indices.append(row["source_index"])
    if not requests:
        raise ValueError("selected trace interval contains no requests")
    pairs = [[row["input_tokens"], row["output_tokens"]] for row in requests]
    provenance.update(synthetic=provenance["source_synthetic"] or target_rps is not None,
                      description="Real-source paired lengths with {} arrival timing; no simulation results.".format(
                          "uniformly time-scaled" if target_rps is not None else "original relative"),
                      arrival_time_scaled=target_rps is not None,
                      full_scaled_duration_seconds=scaled_duration / 1e9,
                      injection_start_seconds=start_ns / 1e9, injection_duration_seconds=injection_ns / 1e9,
                      requested_full_trace_rate_rps=target_rps,
                      actual_selected_rate_rps=len(requests) / (injection_ns / 1e9),
                      time_scale=scaled_duration / source_duration,
                      source_window_end_seconds=min(source_duration, (injection_ns * source_duration + scaled_duration - 1) // scaled_duration) / 1e9,
                      selected_request_count=len(requests), time_window_excluded_requests=len(records)-len(requests),
                      paired_lengths_sha256=hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest(),
                      source_row_order_sha256=hashlib.sha256(json.dumps(source_indices, separators=(",", ":")).encode()).hexdigest(),
                      time_scale_changes_absolute_burst_duration=target_rps is not None,
                      slo_origin="explicit experiment assumptions" if ttft_slo_seconds is not None or itl_slo_seconds is not None else "not supplied by source")
    workload = dict(schema_version=1, provenance=provenance, requests=requests)
    validated, _ = validate_requests(workload)
    if [row["request_id"] for row in validated] != [row["request_id"] for row in requests]:
        raise ValueError("native request sorting would change trace order; timing needs finer resolution")
    return workload


def profile_coverage(workload, profile):
    """Audit all requests, including the last context queried during decode."""
    prefill_high = min(layer["points"][-1]["tokens"] for layer in profile.phases["prefill"]["layers"]) if profile.layered else profile.phases["prefill"]["points"][-1]["tokens"]
    prefill_low = max(layer["points"][0]["tokens"] for layer in profile.phases["prefill"]["layers"]) if profile.layered else profile.phases["prefill"]["points"][0]["tokens"]
    decode = profile.phases["decode"]["points"]
    bad = []
    for row in workload["requests"]:
        inp, out = row["input_tokens"], row["output_tokens"]
        if not prefill_low <= inp <= prefill_high or (out > 1 and not decode[0]["tokens"] <= inp <= inp + out - 2 <= decode[-1]["tokens"]):
            bad.append(row["request_id"])
    return dict(request_count=len(workload["requests"]), covered_count=len(workload["requests"])-len(bad),
                coverage_fraction=(len(workload["requests"])-len(bad))/len(workload["requests"]),
                out_of_coverage_count=len(bad), first_out_of_coverage_ids=bad[:10],
                prefill_range=[prefill_low, prefill_high], decode_range=[decode[0]["tokens"], decode[-1]["tokens"]])
