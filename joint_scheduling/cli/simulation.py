"""Argument registration and handlers for simulation commands."""
import json
from pathlib import Path

from ..config import load_config


def register_run_baseline(commands):
    run = commands.add_parser("run-baseline", help="联合/轮询绑定、分块/合批执行和整份/逐层 KV 的 ns-3 基线")
    run.add_argument("--workload", type=Path)
    run.add_argument("--profile", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--plot", action="store_true")
    from ..backend.settings import KV_TRANSFER_MODES
    run.add_argument("--kv-mode", choices=KV_TRANSFER_MODES, help="覆盖 runtime.kv_transfer_mode")
    run.set_defaults(handler=handle_run_baseline)


def handle_run_baseline(args):
    config = load_config(args.config)
    from ..backend.runner import run_baseline
    result = run_baseline(config, args.output, args.workload, args.profile, args.plot, args.kv_mode)
    print("基线运行完成：{}".format(args.output.resolve()))
    print(json.dumps(result.summary["completion_window"], ensure_ascii=False, indent=2))
    return 0
