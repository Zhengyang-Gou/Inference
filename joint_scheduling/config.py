"""One configuration entry point for scheduling infrastructure and evaluation."""

import json
from dataclasses import dataclass
from pathlib import Path

from .binding import BindingConfig
from .backend.runtime import RuntimeConfig, parse_config as parse_runtime
from .backend.expert_parallel import ExpertParallelConfig, parse_config as parse_ep, validate_config as validate_ep
from .feedback import FeedbackConfig
from .evaluation.config import EvaluationConfig, parse_config as parse_evaluation
from .kv_scheduler import KVConfig
from .reservations import ReservationsConfig
from .prefix_cache import PrefixCacheConfig
from .recording import RecordingConfig
from .topology import TopologyConfig, parse_config as parse_topology
from .validation import ConfigError, read_settings, require_mapping


DEFAULT_CONFIG = Path(__file__).resolve().parent / "configs" / "default.json"


@dataclass(frozen=True)
class ExperimentConfig:
    topology: TopologyConfig
    binding: BindingConfig
    kv_scheduler: KVConfig
    feedback: FeedbackConfig
    reservations: ReservationsConfig
    evaluation: EvaluationConfig
    runtime: RuntimeConfig
    config_path: Path
    prefix_cache: PrefixCacheConfig = PrefixCacheConfig()
    recording: RecordingConfig = RecordingConfig()
    expert_parallel: ExpertParallelConfig = ExpertParallelConfig()

    def input_notes(self):
        notes = []
        profile = self.feedback.compute_profile_path
        if not profile:
            notes.append("尚未指定计算性能数据；运行仿真前需要提供并校准。")
        elif not self.profile_path().is_file():
            notes.append("计算性能数据文件不存在：{}".format(self.profile_path()))
        notes.append("run-baseline 支持整份、分块、逐层立即和动态 SLO/FIFO KV 门控；逐层模式需 schema 2，可选有界 P/D 联合选择。")
        notes.append("前缀缓存{}；开启后使用 prefix_blocks 与显式 cached_points，支持 P/D 独立命中。".format(
            "已开启" if self.prefix_cache.enabled else "已关闭"))
        notes.append("绑定窗口{}，完整方案诊断{}，实际 P/D 选择为 {}。".format(
            "已开启" if self.binding.window_enabled else "已关闭", "已开启" if self.binding.prediction_enabled else "已关闭",
            "候选剪枝与增量贪心" if self.binding.joint_selection_enabled and self.binding.policy == "joint_lexicographic" else "轮询"))
        notes.append("记录模式为 {}：必要请求轨迹与汇总始终保留，高频明细仅在 debug 模式写盘。".format(self.recording.mode))
        if self.expert_parallel.enabled:
            notes.append("实验性 EP 已开启：节点内 TP、显式专家路由及共享专家队列；当前不支持 EP 联合选择或绑定预测。")
        return notes

    def profile_path(self):
        path = Path(self.feedback.compute_profile_path)
        return path if path.is_absolute() else self.config_path.parent / path

    def evaluation_output_path(self):
        path = Path(self.evaluation.output_dir)
        return path if path.is_absolute() else self.config_path.parent / path


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle, object_pairs_hook=_unique_keys)
    except (OSError, ValueError) as error:
        raise ConfigError("Cannot read {}: {}".format(path, error)) from error
    raw = require_mapping(raw, "config")
    expected = {"topology", "binding", "kv_scheduler", "feedback", "reservations"}
    if not expected <= set(raw) or set(raw) - expected - {"evaluation", "runtime", "prefix_cache", "recording", "expert_parallel"}:
        raise ConfigError("Configuration requires {} and optionally evaluation/runtime/prefix_cache/recording/expert_parallel".format(", ".join(sorted(expected))))
    config = ExperimentConfig(
        topology=parse_topology(raw["topology"]),
        binding=read_settings(BindingConfig, raw["binding"], "binding"),
        kv_scheduler=read_settings(KVConfig, raw["kv_scheduler"], "kv_scheduler"),
        feedback=read_settings(FeedbackConfig, raw["feedback"], "feedback"),
        reservations=read_settings(ReservationsConfig, raw["reservations"], "reservations"),
        evaluation=parse_evaluation(raw.get("evaluation", {})),
        runtime=parse_runtime(raw.get("runtime", {})),
        config_path=path,
        prefix_cache=read_settings(PrefixCacheConfig, raw.get("prefix_cache", {}), "prefix_cache"),
        recording=read_settings(RecordingConfig, raw.get("recording", {}), "recording"),
        expert_parallel=parse_ep(raw.get("expert_parallel", {})),
    )
    if any(value*1e6 >= 2**63 for value in (config.binding.window_ms, config.binding.max_wait_ms,
                                           config.binding.early_trigger_margin_ms)):
        raise ConfigError("binding window times exceed the nanosecond clock range")
    if config.binding.decision_overhead_us*1000 >= 2**63 or config.binding.prediction_time_limit_seconds*1e9 >= 2**63:
        raise ConfigError("binding prediction/decision times exceed the nanosecond clock range")
    if config.kv_scheduler.max_inflight_bytes < config.kv_scheduler.max_block_bytes:
        raise ConfigError("kv_scheduler.max_inflight_bytes must be >= max_block_bytes")
    if config.kv_scheduler.decode_admission_limit and config.runtime.kv_transfer_mode not in ("layer_ready_fifo", "layer_ready_slo"):
        raise ConfigError("decode admission requires a controlled layer-ready KV mode")
    if config.runtime.kv_transfer_mode in ("layer_ready_fifo", "layer_ready_slo"):
        if config.kv_scheduler.max_resource_inflight_bytes < config.kv_scheduler.max_block_bytes:
            raise ConfigError("controlled KV requires max_resource_inflight_bytes >= max_block_bytes")
    if config.feedback.network_ewma_alpha > 1:
        raise ConfigError("feedback.network_ewma_alpha must be <= 1")
    if config.kv_scheduler.completion_feedback_alpha > 1:
        raise ConfigError("kv_scheduler.completion_feedback_alpha must be <= 1")
    if not config.feedback.compute_profile_excludes_communication:
        raise ConfigError("Compute profiles must exclude communication to avoid double-counting network time")
    if config.kv_scheduler.mlu_medium_threshold >= config.kv_scheduler.mlu_urgent_threshold:
        raise ConfigError('MLU thresholds must increase from medium to urgent')
    if config.kv_scheduler.policy == 'slo_mlu':
        if config.runtime.kv_transfer_mode != 'layer_ready_slo' or not config.kv_scheduler.slo_priority_enabled:
            raise ConfigError('slo_mlu requires layer_ready_slo and slo_priority_enabled')
        if config.binding.joint_selection_enabled or config.binding.prediction_enabled:
            raise ConfigError('slo_mlu currently requires fixed/round-robin binding with binding prediction disabled')
    validate_ep(config)
    return config
