"""Expert routing and compute costs, independent of runtime execution."""
from ..validation import integer, number, require_mapping
from .expert_config import validate_config


class ExpertProfile:
    def __init__(self, raw, layers):
        raw = require_mapping(raw, "profile.expert_parallel")
        keys = {"activation_layout", "dense_compute_excludes_experts", "num_experts", "top_k", "hidden_size", "dispatch_element_bytes",
                "combine_element_bytes", "routing_pattern", "prefill_layer_ids", "decode_layers",
                "expert_compute_seconds_per_assignment", "expert_tp_collectives_per_call"}
        optional={"expert_compute_seconds_per_active_expert", "n_group", "topk_group"}
        if not keys <= set(raw) or set(raw)-keys-optional or raw["activation_layout"] != "tp_sharded" or raw['dense_compute_excludes_experts'] is not True:
            raise ValueError("EP profile requires explicit TP-sharded activations and all EP fields")
        self.raw = raw
        for key in ("num_experts", "top_k", "hidden_size", "dispatch_element_bytes", "combine_element_bytes", "decode_layers"):
            number(raw[key], "EP " + key, True, True)
        number(raw["expert_tp_collectives_per_call"], "EP expert TP collectives", integral=True)
        if raw["top_k"] > raw["num_experts"]:
            raise ValueError("EP top_k must not exceed num_experts")
        grouped = 'n_group' in raw or 'topk_group' in raw
        if grouped:
            if not {'n_group','topk_group'} <= set(raw):
                raise ValueError('EP n_group and topk_group must be declared together')
            for key in ('n_group','topk_group'):
                number(raw[key],'EP '+key,positive=True,integral=True)
            if raw['num_experts'] % raw['n_group'] or raw['topk_group'] > raw['n_group']:
                raise ValueError('EP expert groups must divide num_experts and cover topk_group')
        pattern = raw["routing_pattern"]
        if not isinstance(pattern, list) or not pattern:
            raise ValueError("EP routing_pattern must be a nonempty periodic token-to-expert table")
        period_counts = [0] * raw['num_experts']
        for experts in pattern:
            if not isinstance(experts, list) or len(experts) != raw["top_k"]:
                raise ValueError("Each EP routing row must contain top_k distinct expert IDs")
            for expert in experts:
                integer(expert, "expert ID")
                if expert >= raw["num_experts"]:
                    raise ValueError("EP expert ID is outside num_experts")
                period_counts[expert] += 1
            if len(set(experts)) != len(experts):
                raise ValueError("EP top-k experts must be distinct for each token")
            if grouped and len({expert // (raw['num_experts']//raw['n_group']) for expert in experts}) > raw['topk_group']:
                raise ValueError('EP routing row exceeds topk_group')
        # Compile the validated table once: long group-complete periods must
        # not make each one-token Decode lookup scan the entire table.
        self._pattern = tuple(tuple(row) for row in pattern)
        self._period_counts = tuple(period_counts)
        ids = raw["prefill_layer_ids"]
        if not isinstance(ids, list) or not ids:
            raise ValueError("EP prefill_layer_ids must be nonempty")
        for i in ids:
            integer(i, "EP prefill layer")
            if i >= layers:
                raise ValueError("EP prefill layer is outside the compute profile")
        if len(set(ids)) != len(ids) or raw["decode_layers"] != len(ids):
            raise ValueError("EP layer IDs must be unique and agree with decode_layers")
        timing = require_mapping(raw["expert_compute_seconds_per_assignment"], "EP expert timing")
        if set(timing) != {"prefill", "decode"}:
            raise ValueError("EP expert timing requires prefill and decode")
        for phase, seconds in timing.items():
            number(seconds, "EP expert timing " + phase, True)
        resident=raw.get('expert_compute_seconds_per_active_expert',{'prefill':0.,'decode':0.})
        if not isinstance(resident,dict) or set(resident)!={'prefill','decode'}:
            raise ValueError('EP active expert timing requires prefill and decode')
        for phase,seconds in resident.items():
            number(seconds,'EP active expert timing '+phase)

    def compute_seconds(self,phase,counts):
        """Assignment work plus the weights/launch cost of experts actually touched."""
        return (sum(counts)*self.raw['expert_compute_seconds_per_assignment'][phase]+
                sum(n>0 for n in counts)*self.raw.get('expert_compute_seconds_per_active_expert',{}).get(phase,0.))

    def counts(self, tokens, offset=0):
        """Exact full-period totals plus the requested tail, independent of context."""
        pattern = self._pattern
        rounds, tail = divmod(tokens, len(pattern))
        counts = [rounds*n for n in self._period_counts]
        for index in range(tail):
            for expert in pattern[(offset+index) % len(pattern)]:
                counts[expert] += 1
        return counts

    def destinations(self, tokens, group, offset=0):
        counts = self.counts(tokens, offset)
        per_rank = self.raw["num_experts"] // len(group)
        return {instance: sum(counts[i*per_rank:(i+1)*per_rank]) for i, instance in enumerate(group)}


def validate_profile(config, profile):
    validate_config(config)  # Also validate programmatically replaced configurations.
    if config.expert_parallel.enabled != (profile.expert is not None):
        raise ValueError("EP config and compute profile must both enable EP; dense timing must exclude expert compute")
    if profile.expert is None:
        return
    for group in config.expert_parallel.groups:
        if profile.expert.raw["num_experts"] % len(group):
            raise ValueError("num_experts must be divisible by each EP group size")
        role = next(p.role for p in config.topology.placements if p.instance_id == group[0])
        declared=profile.provenance.get('expert_parallel_sizes', {}).get(role,
            profile.provenance.get('expert_parallel_size'))
        if declared is not None and declared != len(group):
            raise ValueError('EP group size does not match the compute profile expert_parallel_size')
    for p in config.topology.placements:
        if len(p.gpu_ids) != profile.phases[p.role]["tp_size"]:
            raise ValueError("EP TP width does not match compute profile")
        if profile.expert.raw["hidden_size"] < len(p.gpu_ids):
            raise ValueError("EP hidden_size must cover every TP channel shard")
