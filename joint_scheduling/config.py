"""One configuration entry point for scheduling infrastructure and evaluation."""

import json
from dataclasses import dataclass
from pathlib import Path

from .binding import BindingConfig
from .backend.runtime import RuntimeConfig, parse_config as parse_runtime
from .feedback import FeedbackConfig
from .evaluation.config import EvaluationConfig, parse_config as parse_evaluation
from .kv_scheduler import KVConfig
from .reservations import ReservationsConfig
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

    def input_notes(self):
        notes = []
        profile = self.feedback.compute_profile_path
        if not profile:
            notes.append("尚未指定计算性能数据；运行仿真前需要提供并校准。")
        elif not self.profile_path().is_file():
            notes.append("计算性能数据文件不存在：{}".format(self.profile_path()))
        notes.append("可用 run-baseline 运行 ns-3 简单基线；优化策略及完整 Vidur 批处理尚未接入。")
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
    if not expected <= set(raw) or set(raw) - expected - {"evaluation", "runtime"}:
        raise ConfigError("Configuration requires {} and optionally evaluation/runtime".format(", ".join(sorted(expected))))
    config = ExperimentConfig(
        topology=parse_topology(raw["topology"]),
        binding=read_settings(BindingConfig, raw["binding"], "binding"),
        kv_scheduler=read_settings(KVConfig, raw["kv_scheduler"], "kv_scheduler"),
        feedback=read_settings(FeedbackConfig, raw["feedback"], "feedback"),
        reservations=read_settings(ReservationsConfig, raw["reservations"], "reservations"),
        evaluation=parse_evaluation(raw.get("evaluation", {})),
        runtime=parse_runtime(raw.get("runtime", {})),
        config_path=path,
    )
    if config.binding.max_wait_ms < config.binding.window_ms:
        raise ConfigError("binding.max_wait_ms must be >= binding.window_ms")
    if config.kv_scheduler.max_inflight_bytes < config.kv_scheduler.max_block_bytes:
        raise ConfigError("kv_scheduler.max_inflight_bytes must be >= max_block_bytes")
    if not config.feedback.compute_profile_excludes_communication:
        raise ConfigError("Compute profiles must exclude communication to avoid double-counting network time")
    return config
