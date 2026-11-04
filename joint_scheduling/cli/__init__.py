"""CLI composition; each command family owns its arguments and execution."""
import argparse
import sys
from pathlib import Path

from ..config import DEFAULT_CONFIG
from . import backend, configuration, evaluation, experiments, profiles, simulation, workload


def build_parser():
    parser = argparse.ArgumentParser(description="联合调度的 ns-3 基线、配置、统计与绘图工具")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="统一 JSON 配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    for register in (
        configuration.register_validate,
        configuration.register_describe,
        configuration.register_export_topology,
        backend.register_backend,
        simulation.register_run_baseline,
        evaluation.register_evaluate,
        evaluation.register_plot,
        evaluation.register_record_demo,
        workload.register_generate_workload,
        workload.register_import_trace,
        profiles.register_build_public_profile,
        profiles.register_build_moe_profile,
        experiments.register_audit_ep,
        experiments.register_assess_calibration,
        experiments.register_run_experiments,
    ):
        register(commands)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, OSError, RuntimeError) as error:
        print("错误：{}".format(error), file=sys.stderr)
        return 2
