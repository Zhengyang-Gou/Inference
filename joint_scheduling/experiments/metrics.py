"""Stable suite columns and per-run metric extraction; no execution dependencies."""

METRICS = ("arrived_count", "completed_count", "failed_count", "incomplete_count", "slo_attainment",
           "completion_throughput_rps", "slo_goodput_rps", "ttft_mean_seconds", "ttft_p95_seconds",
           "handoff_p95_seconds", "later_itl_p95_seconds", "e2e_p95_seconds",
           "prefix_hit_tokens", "prefix_saved_kv_transfer_bytes", "prefix_resident_bytes",
           "binding_candidate_scores", "binding_budget_exhaustions", "binding_round_p50_us",
           "binding_round_p95_us", "binding_round_p99_us", "binding_inclusive_p99_us")
COLUMNS = ("run_id", "case_id", "seed", "workload_seed", "status", "parameters", "output_dir", "binding_plan_sha256",
           "error", "wall_seconds", "worker_pid", "experiment_type", "source_tree_sha256", "backend_fingerprint",
           "workload_sha256", "profile_sha256") + METRICS


def extract_metrics(summary):
    completion, cohort = summary["completion_window"], summary["arrival_cohort"]
    result = {key: completion[key] for key in ("completed_count", "slo_attainment", "completion_throughput_rps", "slo_goodput_rps")}
    result.update({key: cohort[key] for key in ("arrived_count", "failed_count", "incomplete_count")})
    result["ttft_mean_seconds"] = summary["latencies"]["ttft_seconds"]["mean"]
    for target, name in (("ttft", "ttft_seconds"), ("handoff", "handoff_seconds"),
                         ("later_itl", "later_max_itl_seconds"), ("e2e", "e2e_seconds")):
        result[target + "_p95_seconds"] = summary["latencies"][name]["quantiles"].get("0.95")
    cache = summary.get("prefix_cache", {})
    for column, key in (("prefix_hit_tokens", "hit_tokens"), ("prefix_saved_kv_transfer_bytes", "saved_kv_transfer_bytes"),
                        ("prefix_resident_bytes", "resident_bytes")):
        result[column] = cache.get(key, 0)
    return result
