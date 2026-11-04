"""Argument registration and handlers for configuration commands."""
import json
from pathlib import Path
from dataclasses import asdict

from ..config import load_config
from ..topology import build_manifest


def register_validate(commands):
    command = commands.add_parser("validate", help="检查配置字段、部署映射和跨模块约束")
    command.set_defaults(handler=handle_validate)


def handle_validate(args):
    config = load_config(args.config)
    print("配置有效：基础模块与 evaluation 的结构和约束检查通过。")
    for note in config.input_notes():
        print("说明：" + note)
    return 0


def register_describe(commands):
    command = commands.add_parser("describe", help="显示模块配置摘要和当前接入状态")
    command.set_defaults(handler=handle_describe)


def handle_describe(args):
    config = load_config(args.config)
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
    return 0


def register_export_topology(commands):
    export = commands.add_parser("export-topology", help="导出检查用拓扑 JSON；不是 ns-3 输入格式")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--format", choices=("inspection", "simai"), default="inspection")
    export.set_defaults(handler=handle_export_topology)


def handle_export_topology(args):
    config = load_config(args.config)
    if args.format == "simai":
        from ..topology import export_backend_topology
        export_backend_topology(config.topology, args.output)
        print("已导出后端拓扑（服务器共享网卡模型）：{}".format(args.output.resolve()))
        return 0
    manifest = build_manifest(config.topology)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("已导出检查用拓扑（不能直接输入 ns-3）：{}".format(args.output.resolve()))
    return 0
