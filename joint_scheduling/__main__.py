"""Configuration, trace evaluation and offline plotting; no simulator is launched."""

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
    run = commands.add_parser("run-baseline", help="轮询 P/D、FIFO 计算和整份 KV 的真实 ns-3 基线")
    run.add_argument("--workload", type=Path)
    run.add_argument("--profile", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--plot", action="store_true")
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
    args = parser.parse_args(argv)
    try:
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
                "status": "ns3_simple_baseline_available_optimized_policies_pending",
                "config_path": str(config.config_path),
                "topology": {"leaves": config.topology.num_leaves,
                             "spines": config.topology.num_spines,
                             "nodes": config.topology.num_nodes,
                             "gpus": config.topology.num_gpus,
                             "prefill_instances": sum(p.role == "prefill" for p in config.topology.placements),
                             "decode_instances": sum(p.role == "decode" for p in config.topology.placements),
                             "routing": config.topology.routing},
                "binding_policy": config.binding.policy,
                "kv_policy": config.kv_scheduler.policy,
                "time_authority": config.feedback.time_authority,
                "evaluation": asdict(config.evaluation),
                "runtime": asdict(config.runtime),
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
            result = run_baseline(config, args.output, args.workload, args.profile, args.plot)
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
