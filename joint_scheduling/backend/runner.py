"""Compatibility entry point: validate inputs, run coordinator, export results."""
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from .expert_parallel import validate_profile as validate_ep_profile
from .build import require_backend
from .client import Ns3Backend
from .runtime import ComputeProfile, KV_TRANSFER_MODES, CONTROLLED_KV_MODES, load_workload
from ..evaluation import TraceRecorder, analyze_trace
from ..evaluation.export import export_result
from ..evaluation.pressure import summarize_pressure
from ..observation import StateObserver
from ..observation.monitor import DisabledStateObserver
from ..kv_scheduler.mlu import validate_settings as validate_mlu_settings


from .execution import ExecutionCoordinator

# Private legacy name retained for existing callers.
_Baseline = ExecutionCoordinator


def run_baseline(config, output_dir, workload_path=None, profile_path=None, make_plots=False, kv_mode=None, *, hooks=None, source_snapshot=None):
    backend_state = require_backend()
    backend_identity = json.loads(backend_state['manifest'].read_text())
    if kv_mode is not None:
        config = replace(config, runtime=replace(config.runtime, kv_transfer_mode=kv_mode))
    validate_mlu_settings(config)
    if profile_path is None and not config.feedback.compute_profile_path:
        raise ValueError("set feedback.compute_profile_path or pass --profile; no synthetic fallback is automatic")
    profile_path = Path(profile_path) if profile_path is not None else config.profile_path()
    if workload_path is None:
        if not config.runtime.workload_path:
            raise ValueError("set runtime.workload_path or pass --workload")
        workload_path = Path(config.runtime.workload_path)
        if not workload_path.is_absolute():
            workload_path = config.config_path.parent / workload_path
    requests, workload_provenance = load_workload(workload_path)
    profile = ComputeProfile(profile_path)
    validate_ep_profile(config, profile)
    if config.runtime.prefill_batch_size > 1 and not config.runtime.prefill_batch_token_budget:
        raise ValueError("prefill_batch_size > 1 requires a token budget")
    if config.runtime.prefill_batch_token_budget:
        if not config.runtime.prefill_chunk_tokens:
            raise ValueError("prefill batching requires a positive per-request chunk cap")
        if requests:
            profile.validate_prefill_batch_coverage(max(r["input_tokens"] for r in requests),
                config.runtime.prefill_chunk_tokens,config.runtime.prefill_batch_token_budget,
                profile.phases["prefill"]["tp_size"])
    if config.runtime.prefill_chunk_tokens:
        for request in ([] if config.runtime.prefill_batch_token_budget else requests):
            profile.prefill_chunks(request["input_tokens"],profile.phases["prefill"]["tp_size"],config.runtime.prefill_chunk_tokens)
    if config.runtime.decode_batch_size > 1:
        decode_requests = [request for request in requests if request["output_tokens"] > 1]
        if decode_requests:
            contexts = (min(r["input_tokens"] for r in decode_requests),
                        max(r["input_tokens"]+r["output_tokens"]-2 for r in decode_requests))
            # Check the cap first, then every smaller batch that runtime can form.
            for size in (config.runtime.decode_batch_size, *range(1, config.runtime.decode_batch_size)):
                for context in contexts:
                    profile.lookup_decode_batch([context]*size, profile.phases["decode"]["tp_size"])
    joint = config.binding.joint_selection_enabled and config.binding.policy == "joint_lexicographic"
    if joint and any(r["output_tokens"] > r.get("max_output_tokens", config.reservations.reserve_max_output_tokens) for r in requests):
        raise ValueError("visible output limit must cover simulated output_tokens for joint reservations")
    if config.prefix_cache.enabled:
        if not profile.layered:
            raise ValueError("prefix cache requires a schema 2 compute profile")
        for row in requests:
            cached = 0
            for block in row.get("prefix_blocks", []):
                cached += block["tokens"]
                if config.prefix_cache.prefill_reuse_enabled:
                    context = min(cached, row["input_tokens"]-1)
                    if config.runtime.prefill_chunk_tokens:
                        if not config.runtime.prefill_batch_token_budget:
                            profile.prefill_chunks(row["input_tokens"], profile.phases["prefill"]["tp_size"],
                                                  config.runtime.prefill_chunk_tokens, cached_tokens=context)
                    else:
                        profile.prefill_steps(row["input_tokens"], profile.phases["prefill"]["tp_size"], context)
    if config.runtime.kv_transfer_mode not in KV_TRANSFER_MODES:
        raise ValueError("unknown KV transfer mode")
    if config.runtime.kv_transfer_mode.startswith("layer_ready_") and not profile.layered:
        raise ValueError("layer-ready modes require an explicit schema 2 layer profile")
    controlled = config.runtime.kv_transfer_mode in CONTROLLED_KV_MODES
    if controlled and config.kv_scheduler.max_resource_inflight_bytes < config.kv_scheduler.max_block_bytes:
        raise ValueError("controlled KV requires max_resource_inflight_bytes >= max_block_bytes")
    if config.runtime.time_limit_seconds < config.evaluation.window_end_seconds:
        raise ValueError("runtime time limit must cover evaluation.window_end_seconds")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    profile_copy = output / "compute_profile.json"
    profile_copy.write_bytes(profile.path.read_bytes())
    (output / "workload.json").write_bytes(Path(workload_path).read_bytes())
    snapshot = asdict(config)
    snapshot["config_path"] = str(config.config_path)
    snapshot["effective_baseline"] = {"endpoint_selection": "joint_lexicographic" if joint else "round_robin", "prefill": "token_budget_running_first_fcfs" if config.runtime.prefill_batch_token_budget else "chunk_round_robin" if config.runtime.prefill_chunk_tokens else "fifo",
                                      "prefill_batch_size": config.runtime.prefill_batch_size,
                                      "prefill_batch_token_budget": config.runtime.prefill_batch_token_budget,
                                      "prefill_chunk_tokens": config.runtime.prefill_chunk_tokens,
                                      "decode": "continuous_batching" if config.runtime.decode_batch_size > 1 else "one_request_token_step_round_robin",
                                      "decode_batch_size": config.runtime.decode_batch_size,
                                      "decode_context_model": "padded_max_context", "kv": config.runtime.kv_transfer_mode,
                                      "prefill_model": "explicit_layers" if profile.layered else "aggregate_step",
                                      "kv_admission_control": controlled,
                                      "network_telemetry": config.feedback.network_observation_enabled,
                                      "state_observation": config.feedback.state_observation_enabled,
                                      "prefix_cache": config.prefix_cache.enabled,
                                      "collective_protection": controlled and config.kv_scheduler.collective_protection_enabled,
                                      "dynamic_inflight": controlled and config.kv_scheduler.inflight_budget_mode == "adaptive",
                                      "decode_admission_limit": config.kv_scheduler.decode_admission_limit if controlled else 0,
                                      "kv_priority": "mlu_multilevel" if config.kv_scheduler.policy=='slo_mlu' else "slo_slack" if config.runtime.kv_transfer_mode == "layer_ready_slo" and config.kv_scheduler.slo_priority_enabled else "fifo" if controlled else "immediate",
                                      "network_priority_enabled":config.runtime.network_priority_enabled,
                                      "kv_block_limit_bytes": config.kv_scheduler.max_block_bytes if config.runtime.kv_transfer_mode != "whole_ready_immediate" else None,
                                      "collectives": "ep_dispatch_combine_and_intra_node_tp_allreduce" if config.expert_parallel.enabled else "gpu_rank_ring_allreduce",
                                      "expert_parallel": config.expert_parallel.enabled, "native_vidur_batching": False}
    snapshot["effective_baseline"].update(binding_window=config.binding.window_enabled,
        binding_prediction=config.binding.prediction_enabled, joint_binding_optimization=joint,
        binding_decision_overhead_us=config.binding.decision_overhead_us,
        binding_prediction_use="sampled_diagnostics" if joint else "diagnostics_only",
        reservation_timing="atomic_binding_batch" if joint else "at_prefill_start")
    (output / "resolved_config.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    portable_config = asdict(config)
    portable_config.pop('config_path')
    portable_config['feedback']['compute_profile_path']='compute_profile.json'
    portable_config['runtime']['workload_path']='workload.json'
    portable_config['evaluation']['output_dir']='result'
    (output/'config.json').write_text(json.dumps(portable_config,indent=2)+'\n')
    from ..experiments.provenance import record_run
    record_run(output, output/'config.json', profile_copy, output/'workload.json',
               backend_identity, 'end_to_end', source_snapshot)
    with Ns3Backend(config.topology, output / "network", config.runtime.seed,
                    config.runtime.congestion_control, config.runtime.packet_payload_bytes,
                    config.runtime.switch_buffer_bytes, config.runtime.rpc_timeout_seconds,
                    network_priority_enabled=config.runtime.network_priority_enabled) as backend:
        metadata = {"synthetic": profile.provenance["synthetic"] or workload_provenance["synthetic"],
                    "backend": backend.metadata, "compute_profile_provenance": profile.provenance,
                    "workload_provenance": workload_provenance, "effective_baseline": snapshot["effective_baseline"],
                    "compute_profile_sha256": hashlib.sha256(profile_copy.read_bytes()).hexdigest(),
                    "recording": asdict(config.recording)}
        strategy = {"whole_ready_immediate": "round_robin_fifo_ns3", "whole_ready_chunked": "whole_chunked_ns3",
                    "layer_ready_immediate": "layer_ready_immediate_ns3", "layer_ready_fifo": "layer_ready_fifo_ns3",
                    "layer_ready_slo": "layer_ready_slo_ns3"}[config.runtime.kv_transfer_mode]
        observer_class = StateObserver if config.feedback.state_observation_enabled else DisabledStateObserver
        with TraceRecorder(output / "events.jsonl", strategy, config.runtime.seed, metadata,
                           detailed=config.recording.detailed) as recorder, \
                observer_class(output / "observation", backend, config.feedback.observation_delay_us,
                               detailed=config.recording.detailed,
                               measurement_window=(config.evaluation.window_start_seconds,
                                                   config.evaluation.window_end_seconds)) as observer:
            baseline = _Baseline(config, backend, recorder, profile, requests, observer, hooks=hooks)
            try:
                (output/'runtime_components.json').write_text(json.dumps(dict(schema_version=1,
                    request_state='immutable_inputs_with_named_progress_and_legacy_mapping',
                    components={name:sorted(component.owned_fields)
                                for name,component in baseline.components.items()}),indent=2)+'\n')
                baseline.capture("initial")
                for row in requests:
                    if row["arrival_seconds"] <= config.runtime.time_limit_seconds:
                        backend.call_at(row["arrival_seconds"], lambda rid=row["request_id"]: baseline.arrive(rid))
                backend.run_until(config.runtime.time_limit_seconds)
                baseline.capture("simulation_end")
                observer.finish()
                recorder.finish(backend.now_seconds())
                (output / "final_resources.json").write_text(json.dumps(baseline.ledger.snapshot(), indent=2) + "\n")
            finally:
                baseline.close()
    result = analyze_trace(output / "events.jsonl", config.evaluation)
    result.summary["recording"] = dict(mode=config.recording.mode, recorded_event_counts=recorder.recorded_counts,
                                       omitted_event_counts=recorder.omitted_counts)
    result.summary["pressure"] = summarize_pressure(result.requests, result.tokens,
        config.evaluation.window_start_seconds, config.evaluation.window_end_seconds,
        result.summary["observation_end_seconds"])
    result.summary["prefix_cache"] = (dict(enabled=True, **baseline.prefix_cache.snapshot())
                                      if baseline.prefix_cache is not None else
                                      dict(enabled=False, hit_tokens=0, saved_kv_transfer_bytes=0, resident_bytes=0))
    if config.expert_parallel.enabled:
        result.summary['expert_parallel'] = dict(enabled=True, **baseline.ep_stats)
    if baseline.scheduler is not None:
        result.summary['kv_scheduler']=dict(algorithm=config.kv_scheduler.policy,**baseline.scheduler.summary,
            pending_blocks=len(baseline.scheduler.pending),inflight_blocks=len(baseline.scheduler.inflight))
    if baseline.binding_report is not None:
        result.summary["binding"] = baseline.binding_report
    export_result(result, output / "result", snapshot, copy_trace=False)
    if make_plots:
        from ..evaluation.plots import plot_results
        plot_results([output / "result"], output / "figures", config.evaluation)
        if config.feedback.state_observation_enabled and config.recording.detailed:
            from ..observation.plots import plot_observation
            plot_observation(output / "observation")
    return result
