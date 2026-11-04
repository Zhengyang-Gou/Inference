"""Expert-parallel configuration and deployment constraints."""
from dataclasses import dataclass, field

from ..validation import ConfigError, read_settings, require_mapping


@dataclass(frozen=True)
class ExpertParallelConfig:
    enabled: bool = False
    groups: tuple = field(default=(), metadata={"internal": True})
    rank_unit: str = field(default='tp_team', metadata={'choices': ('tp_team', 'gpu')})


def parse_config(raw):
    raw = require_mapping(raw, "expert_parallel")
    groups = raw.pop("groups", [])
    settings = read_settings(ExpertParallelConfig, raw, "expert_parallel")
    if not isinstance(groups, list) or any(not isinstance(g, list) or len(g) < 2 or
            any(not isinstance(i, str) or not i.strip() for i in g) for g in groups):
        raise ConfigError("expert_parallel.groups must contain lists of at least two instance IDs")
    if bool(groups) != settings.enabled:
        raise ConfigError("expert_parallel.enabled requires groups; disabled EP must have no groups")
    return ExpertParallelConfig(settings.enabled, tuple(tuple(g) for g in groups), settings.rank_unit)


def validate_config(config):
    ep = config.expert_parallel
    if not ep.enabled:
        if ep.groups:
            raise ConfigError("Disabled EP must have no groups")
        return
    placements = {p.instance_id: p for p in config.topology.placements}
    seen = set()
    for group in ep.groups:
        if len(group) < 2 or any(i not in placements or i in seen for i in group) or len(set(group)) != len(group):
            raise ConfigError("EP groups must contain distinct, known instances exactly once")
        peers = [placements[i] for i in group]
        if len({p.role for p in peers}) != 1 or len({len(p.gpu_ids) for p in peers}) != 1:
            raise ConfigError("Each EP group requires one role and equal TP widths")
        seen.update(group)
    if seen != set(placements):
        raise ConfigError("EP groups must cover every P/D instance exactly once")
    for role in ('prefill', 'decode'):
        if len({len(g) for g in ep.groups if placements[g[0]].role == role}) != 1:
            raise ConfigError('EP groups within each phase must have equal size')
    if ep.rank_unit == 'gpu' and any(len(p.gpu_ids) != 1 for p in placements.values()):
        raise ConfigError('GPU-rank EP requires TP1 for every attention instance')
    for p in placements.values():
        if len({g // config.topology.gpus_per_node for g in p.gpu_ids}) != 1:
            raise ConfigError("EP mode requires every TP instance to stay within one node")
    if config.binding.joint_selection_enabled or config.binding.prediction_enabled:
        raise ConfigError("Experimental EP does not yet support joint binding or binding prediction; disable both explicitly")
