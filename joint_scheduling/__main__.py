"""Workload generation, baseline simulation, observation and experiment suites."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .topology import build_manifest
from .validation import ConfigError


def main(argv=None):
    parser = argparse.ArgumentParser(description="联合调度的 ns-3 基线、配置、统计与绘图工具")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="统一 JSON 配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="检查配置字段、部署映射和跨模块约束")
    commands.add_parser("describe", help="显示模块配置摘要和当前接入状态")
    export = commands.add_parser("export-topology", help="导出检查用拓扑 JSON；不是 ns-3 输入格式")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--format", choices=("inspection", "simai"), default="inspection")
    backend = commands.add_parser("backend", help="固定版本网络后端的检查、构建与回归验证")
    backend_commands = backend.add_subparsers(dest="backend_command", required=True)
    backend_commands.add_parser("doctor", help="检查工具链、固定版本和后端状态")
    build = backend_commands.add_parser("build", help="在独立目录中构建后端")
    build.add_argument("--jobs", type=int, default=2)
    smoke = backend_commands.add_parser("smoke", help="执行真实 ns-3 网络与端到端回归")
    smoke.add_argument("--output", type=Path)
    run = commands.add_parser("run-baseline", help="联合/轮询绑定、分块/合批执行和整份/逐层 KV 的 ns-3 基线")
    run.add_argument("--workload", type=Path)
    run.add_argument("--profile", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--plot", action="store_true")
    from .backend.runtime import KV_TRANSFER_MODES
    run.add_argument("--kv-mode", choices=KV_TRANSFER_MODES, help="覆盖 runtime.kv_transfer_mode")
    evaluate = commands.add_parser("evaluate", help="从逐 token JSONL 轨迹计算指标并导出结果")
    evaluate.add_argument("--trace", required=True, type=Path)
    evaluate.add_argument("--output", type=Path, help="新的结果目录；默认使用 evaluation.output_dir")
    evaluate.add_argument("--plot", action="store_true", help="导出后同时绘图，需要 matplotlib")
    plot = commands.add_parser("plot", help="仅从导出结果绘图，可比较多个策略/种子")
    plot.add_argument("--results", required=True, nargs="+", type=Path)
    plot.add_argument("--output", type=Path)
    demo = commands.add_parser("record-demo", help="生成明确标记为 synthetic 的示例轨迹，不代表策略效果")
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument("--strategy", default="synthetic_demo")
    demo.add_argument("--seed", type=int, default=7)
    generate = commands.add_parser("generate-workload", help="生成可重复的请求到达、长度和 SLO 负载")
    generate.add_argument("--spec", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--seed", type=int, help="覆盖负载规格中的随机种子")
    trace = commands.add_parser("import-trace", help="导入真实来源请求轨迹，保留长度配对并可按到达率缩放时间")
    trace.add_argument("--trace", type=Path, required=True)
    trace.add_argument("--format", choices=("azure", "mooncake"), required=True)
    trace.add_argument("--source-url", default="")
    trace.add_argument("--source-synthetic", action="store_true", help="原始文件本身是合成轨迹时必须声明")
    trace.add_argument("--sha256", help="验证已冻结的原始文件")
    trace.add_argument("--target-rps", type=float)
    trace.add_argument("--duration-seconds", type=float, help="显式选取缩放后时间前缀，不重复或截短请求")
    trace.add_argument("--request-prefix", default="trace")
    trace.add_argument("--ttft-slo-seconds", type=float)
    trace.add_argument("--itl-slo-seconds", type=float)
    trace.add_argument("--profile", type=Path, help="检查所有请求及最终 Decode 上下文；越界拒绝写出")
    trace.add_argument("--output", type=Path, required=True)
    public = commands.add_parser("build-public-profile", help="从公开 Vidur A100 算子 CSV 构建明确标记为估算的 70B 逐层参数")
    public.add_argument("--mlp", type=Path, required=True)
    public.add_argument("--attention", type=Path, required=True)
    public.add_argument("--source-commit", default="")
    public.add_argument("--tp", type=int, default=4)
    public.add_argument("--prefill-chunk-tokens", type=int, default=0)
    public.add_argument("--target-model", choices=("meta-llama/Meta-Llama-3-70B", "meta-llama/Llama-3.1-70B-Instruct"), default="meta-llama/Llama-3.1-70B-Instruct")
    public.add_argument("--output", type=Path, required=True)
    moe = commands.add_parser("build-moe-profile", help="从 H800 DeepSeek 公开采样或显式 AIOB 数据构建注明估算来源的 MoE profile")
    moe.add_argument("--model-config", type=Path, required=True)
    moe.add_argument("--prefill-trace", type=Path)
    moe.add_argument("--decode-trace", type=Path)
    moe.add_argument("--aiob-manifest", type=Path)
    moe.add_argument("--tp", type=int, default=4)
    moe.add_argument("--ep", type=int, default=8)
    moe.add_argument("--max-context", type=int, default=16384)
    moe.add_argument("--prefill-chunk-tokens", type=int, default=512)
    moe.add_argument("--prefill-batch-token-budget", type=int, default=2048)
    moe.add_argument("--decode-batch-size", type=int, default=64)
    moe.add_argument("--output", type=Path, required=True)
    assess = commands.add_parser("assess-calibration", help="只读取已完成的基线回放，按冻结判据确定承载率区间")
    assess.add_argument("--plan", type=Path, required=True)
    assess.add_argument("--results-root", type=Path, required=True)
    assess.add_argument("--output", type=Path, required=True)
    experiments = commands.add_parser("run-experiments", help="串行执行参数矩阵并汇总结果和失败记录")
    experiments.add_argument("--spec", type=Path, required=True)
    experiments.add_argument("--output", type=Path, required=True)
    experiments.add_argument("--plot", action="store_true")
    experiments.add_argument("--fail-fast", action="store_true")
    experiments.add_argument("--dry-run", action="store_true", help="仅显示运行计划，不创建输出或启动网络后端")
    args = parser.parse_args(argv)
    try:
        if args.command == "build-moe-profile":
            from .backend.moe_profile import extract_public_anchors, load_aiob_anchors, build_moe_profile
            from .backend.runtime import ComputeProfile
            if bool(args.aiob_manifest) == bool(args.prefill_trace or args.decode_trace) or (not args.aiob_manifest and not (args.prefill_trace and args.decode_trace)):
                raise ValueError("provide both --prefill-trace/--decode-trace, or one --aiob-manifest")
            anchors = (load_aiob_anchors(args.aiob_manifest) if args.aiob_manifest else
                       extract_public_anchors(args.prefill_trace,args.decode_trace))
            profile,audit = build_moe_profile(args.model_config,anchors,args.tp,args.ep,args.max_context,
                args.prefill_chunk_tokens,args.prefill_batch_token_budget,args.decode_batch_size)
            audit_path = args.output.with_suffix(".audit.json")
            if args.output.exists() or audit_path.exists():
                raise ValueError("profile or audit output already exists")
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                candidate=Path(folder)/"profile.json"
                candidate.write_text(json.dumps(profile,allow_nan=False))
                checked=ComputeProfile(candidate)
                checked.validate_prefill_batch_coverage(args.max_context,args.prefill_chunk_tokens,args.prefill_batch_token_budget,args.tp)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            with args.output.open("x",encoding="utf-8") as handle:
                json.dump(profile,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write("\n")
            with audit_path.open("x",encoding="utf-8") as handle:
                json.dump(audit,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write("\n")
            print(json.dumps(dict(profile=str(args.output.resolve()),audit=str(audit_path.resolve()),
                status=audit['status'],coverage=audit['exported_coverage'],weights_gib_per_gpu=audit['weights_gib_per_gpu']),ensure_ascii=False,indent=2))
            return 0
        if args.command == "assess-calibration":
            from .experiments.calibration import assess_calibration
            report = assess_calibration(args.plan, args.results_root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps(dict(completed=report["completed_trial_count"], planned=report["trial_count"],
                                  selections=report["selections"]), ensure_ascii=False, indent=2))
            return 0
        if args.command == "build-public-profile":
            from .backend.public_profile import build_public_profile
            profile, audit = build_public_profile(args.mlp, args.attention, args.tp, args.source_commit, args.prefill_chunk_tokens, args.target_model)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(profile, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps(dict(profile=str(args.output.resolve()), audit=audit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "import-trace":
            from .backend.runtime import ComputeProfile
            from .workload.trace import read_trace, scale_trace, profile_coverage
            source = read_trace(args.trace, args.format, args.source_url, args.sha256, args.source_synthetic)
            workload = scale_trace(source, args.target_rps, request_prefix=args.request_prefix,
                                   duration_seconds=args.duration_seconds,
                                   ttft_slo_seconds=args.ttft_slo_seconds, itl_slo_seconds=args.itl_slo_seconds)
            coverage = profile_coverage(workload, ComputeProfile(args.profile)) if args.profile else None
            if coverage is not None and coverage["out_of_coverage_count"]:
                raise ValueError("{} requests exceed the profile; no token clipping performed".format(coverage["out_of_coverage_count"]))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(workload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps(dict(output=str(args.output.resolve()), requests=len(workload["requests"]),
                                  provenance=workload["provenance"], coverage=coverage), ensure_ascii=False, indent=2))
            return 0
        if args.command == "generate-workload":
            from .backend.runtime import read_json
            from .workload import write_workload
            workload = write_workload(read_json(args.spec), args.output, args.seed)
            print("已生成 {} 个请求：{}".format(len(workload["requests"]), args.output.resolve()))
            return 0
        if args.command == "run-experiments":
            from .experiments import plan_suite, run_suite
            if args.dry_run:
                plan = plan_suite(args.spec)
                report={"experiment_type":plan['source_spec'].get('experiment_type','end_to_end'),
                        "total_runs": len(plan["runs"]), "runs": plan["runs"]}
                if 'comparison_audit' in plan:report['comparison_audit']=plan['comparison_audit']
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            report = run_suite(args.spec, args.output, args.plot, args.fail_fast,
                               progress=lambda row: print("{}: {}{}".format(row["run_id"], row["status"],
                                   " — " + row["error"] if row.get("error") else ""), flush=True))
            print("实验汇总：{}".format(args.output.resolve() / "summary.json"))
            return 0 if report["status"] == "completed" else 2
        # Toolchain operations must remain usable even if an experiment config
        # is temporarily missing or invalid while the user edits it.
        if args.command == "backend":
            from .backend.build import doctor, build_backend
            if args.backend_command == "doctor":
                report = doctor()
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["backend_ready"] else 2
            if args.backend_command == "build":
                state = build_backend(args.jobs)
                print("后端构建完成：{}".format(state["executable"]))
            else:
                from .backend.smoke import smoke_test
                print(json.dumps(smoke_test(args.output), ensure_ascii=False, indent=2))
            return 0
        config = load_config(args.config)
        if args.command == "validate":
            print("配置有效：基础模块与 evaluation 的结构和约束检查通过。")
            for note in config.input_notes():
                print("说明：" + note)
        elif args.command == "describe":
            summary = {
                "status": "experimental_ep_round_robin" if config.expert_parallel.enabled else "bounded_incremental_joint_binding_available",
                "config_path": str(config.config_path),
                "topology": {"leaves": config.topology.num_leaves,
                             "spines": config.topology.num_spines,
                             "nodes": config.topology.num_nodes,
                             "gpus": config.topology.num_gpus,
                             "prefill_instances": sum(p.role == "prefill" for p in config.topology.placements),
                             "decode_instances": sum(p.role == "decode" for p in config.topology.placements),
                             "routing": config.topology.routing},
                "binding_policy": config.binding.policy,
                "binding": asdict(config.binding),
                "effective_endpoint_selection": "joint_lexicographic" if config.binding.joint_selection_enabled and config.binding.policy == "joint_lexicographic" else "round_robin",
                "kv_policy": config.kv_scheduler.policy,
                "time_authority": config.feedback.time_authority,
                "evaluation": asdict(config.evaluation),
                "runtime": asdict(config.runtime),
                "prefix_cache": asdict(config.prefix_cache),
                "recording": asdict(config.recording),
                "expert_parallel": asdict(config.expert_parallel),
                "feature_switches": {
                    "binding_window_enabled": config.binding.window_enabled,
                    "binding_prediction_enabled": config.binding.prediction_enabled,
                    "slo_priority_enabled": config.kv_scheduler.slo_priority_enabled,
                    "collective_protection_enabled": config.kv_scheduler.collective_protection_enabled,
                    "inflight_budget_mode": config.kv_scheduler.inflight_budget_mode,
                    "network_observation_enabled": config.feedback.network_observation_enabled,
                    "state_observation_enabled": config.feedback.state_observation_enabled},
                "notes": config.input_notes(),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "export-topology":
            if args.format == "simai":
                from .topology import export_backend_topology
                export_backend_topology(config.topology, args.output)
                print("已导出后端拓扑（服务器共享网卡模型）：{}".format(args.output.resolve()))
                return 0
            manifest = build_manifest(config.topology)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            print("已导出检查用拓扑（不能直接输入 ns-3）：{}".format(args.output.resolve()))
        elif args.command == "run-baseline":
            from .backend.runner import run_baseline
            result = run_baseline(config, args.output, args.workload, args.profile, args.plot, args.kv_mode)
            print("基线运行完成：{}".format(args.output.resolve()))
            print(json.dumps(result.summary["completion_window"], ensure_ascii=False, indent=2))
        elif args.command == "record-demo":
            from .evaluation.demo import create_demo_trace
            create_demo_trace(args.output, args.strategy, args.seed, config.evaluation.window_end_seconds)
            print("已生成合成示例轨迹（非仿真结果）：{}".format(args.output.resolve()))
        elif args.command == "evaluate":
            from .evaluation.metrics import analyze_trace
            from .evaluation.export import export_result
            result = analyze_trace(args.trace, config.evaluation)
            snapshot = asdict(config)
            snapshot["config_path"] = str(config.config_path)
            output = export_result(result, args.output or config.evaluation_output_path(), snapshot)
            print("统计结果已导出：{}".format(output))
            print(json.dumps(result.summary["completion_window"], ensure_ascii=False, indent=2))
            if args.plot:
                from .evaluation.plots import plot_results
                targets = plot_results([output], output / "figures", config.evaluation)
                print("已生成 {} 个图文件。".format(len(targets)))
        else:
            from .evaluation.plots import plot_results
            output = args.output or config.evaluation_output_path() / "figures"
            targets = plot_results(args.results, output, config.evaluation)
            print("已生成 {} 个图文件：{}".format(len(targets), output.resolve()))
    except (ConfigError, ValueError, OSError, RuntimeError) as error:
        print("错误：{}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
