"""Self-contained result bundles for offline statistics and plotting."""

import csv
import json
import os
import shutil
import tempfile
from pathlib import Path


REQUEST_COLUMNS = (
    "request_id", "status", "arrived_at_seconds", "completed_at_seconds", "failed_at_seconds",
    "failure_reason", "output_tokens", "expected_output_tokens", "ttft_slo_seconds", "itl_slo_seconds",
    "ttft_seconds", "handoff_seconds", "later_max_itl_seconds", "max_itl_seconds", "mean_tpot_seconds",
    "e2e_seconds", "minimum_normalized_slo_margin", "slo_met", "arrival_cohort", "completion_window",
    "ttft_slo_met", "handoff_slo_met", "later_itl_slo_met",
)
TOKEN_COLUMNS = ("request_id", "token_index", "returned_at_seconds", "itl_seconds", "phase")
TIMESERIES_COLUMNS = ("start_seconds", "end_seconds", "completed_count", "slo_met_count",
                      "completion_throughput_rps", "slo_goodput_rps")


def _csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def export_result(result, output_dir, experiment_config=None, copy_trace=True):
    """Export metrics; a run can reference its single sibling trace instead."""
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError("result directory already exists: {}".format(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".evaluation-", dir=str(output.parent)) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        source = Path(result.summary["source_trace"]).resolve()
        summary = dict(result.summary, raw_trace_file="events.jsonl" if copy_trace else os.path.relpath(source, output))
        with (bundle / "summary.json").open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        _csv(bundle / "requests.csv", REQUEST_COLUMNS, result.requests)
        _csv(bundle / "tokens.csv", TOKEN_COLUMNS, result.tokens)
        _csv(bundle / "timeseries.csv", TIMESERIES_COLUMNS, result.timeseries)
        if copy_trace:
            shutil.copyfile(source, bundle / "events.jsonl")
        snapshot = experiment_config if experiment_config is not None else {"evaluation": summary["evaluation_config"]}
        with (bundle / "config_snapshot.json").open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        if output.exists():
            raise FileExistsError("result directory already exists: {}".format(output))
        bundle.rename(output)
    return output
