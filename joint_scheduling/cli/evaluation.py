"""Argument registration and handlers for evaluation commands."""
import json
from pathlib import Path
from dataclasses import asdict

from ..config import load_config


def register_evaluate(commands):
    evaluate = commands.add_parser("evaluate", help="从逐 token JSONL 轨迹计算指标并导出结果")
    evaluate.add_argument("--trace", required=True, type=Path)
    evaluate.add_argument("--output", type=Path, help="新的结果目录；默认使用 evaluation.output_dir")
    evaluate.add_argument("--plot", action="store_true", help="导出后同时绘图，需要 matplotlib")
    evaluate.set_defaults(handler=handle_evaluate)


def handle_evaluate(args):
    config = load_config(args.config)
    from ..evaluation.metrics import analyze_trace
    from ..evaluation.export import export_result
    result = analyze_trace(args.trace, config.evaluation)
    snapshot = asdict(config)
    snapshot["config_path"] = str(config.config_path)
    output = export_result(result, args.output or config.evaluation_output_path(), snapshot)
    print("统计结果已导出：{}".format(output))
    print(json.dumps(result.summary["completion_window"], ensure_ascii=False, indent=2))
    if args.plot:
        from ..evaluation.plots import plot_results
        targets = plot_results([output], output / "figures", config.evaluation)
        print("已生成 {} 个图文件。".format(len(targets)))
    return 0


def register_plot(commands):
    plot = commands.add_parser("plot", help="仅从导出结果绘图，可比较多个策略/种子")
    plot.add_argument("--results", required=True, nargs="+", type=Path)
    plot.add_argument("--output", type=Path)
    plot.set_defaults(handler=handle_plot)


def handle_plot(args):
    config = load_config(args.config)
    from ..evaluation.plots import plot_results
    output = args.output or config.evaluation_output_path() / "figures"
    targets = plot_results(args.results, output, config.evaluation)
    print("已生成 {} 个图文件：{}".format(len(targets), output.resolve()))
    return 0


def register_record_demo(commands):
    demo = commands.add_parser("record-demo", help="生成明确标记为 synthetic 的示例轨迹，不代表策略效果")
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument("--strategy", default="synthetic_demo")
    demo.add_argument("--seed", type=int, default=7)
    demo.set_defaults(handler=handle_record_demo)


def handle_record_demo(args):
    config = load_config(args.config)
    from ..evaluation.demo import create_demo_trace
    create_demo_trace(args.output, args.strategy, args.seed, config.evaluation.window_end_seconds)
    print("已生成合成示例轨迹（非仿真结果）：{}".format(args.output.resolve()))
    return 0
