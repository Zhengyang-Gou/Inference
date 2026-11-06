"""Token-by-token EP traffic oracle, independent of optimized runtime counters.

Only activation payload is counted. The node reference describes one copy per
token per remote node; it does not simulate DeepEP forwarding or wire overhead.
"""
from collections import Counter
from types import SimpleNamespace


def audit_routing_constraint(expert, num_groups, max_groups_per_token):
    """Compare an explicit periodic table with a declared external gate constraint."""
    if (type(num_groups) is not int or type(max_groups_per_token) is not int or
            not 1 <= max_groups_per_token <= num_groups <= expert.raw['num_experts'] or
            expert.raw['num_experts'] % num_groups):
        raise ValueError('routing reference requires divisible expert groups and a limit in [1, groups]')
    per_group = expert.raw['num_experts'] // num_groups
    histogram, violations = Counter(), []
    for index, experts in enumerate(expert.raw['routing_pattern']):
        touched = len({expert // per_group for expert in experts})
        histogram[touched] += 1
        if touched > max_groups_per_token:
            violations.append(index)
    return dict(status='differs_from_reference_constraint' if violations else 'matches_reference_constraint',
                expert_groups=num_groups,max_groups_per_token=max_groups_per_token,
                routing_rows=len(expert.raw['routing_pattern']),groups_touched_histogram=dict(histogram),
                violating_rows=len(violations),first_violating_row_indices=violations[:8],
                scope='expert ID groups, not physical nodes; audit only, no routing changes')


def token_reference(expert, group, placements, source, gpus_per_node, spans, layer_id=None):
    """Enumerate independent (token count, cursor) spans, including batch members."""
    raw = expert.raw
    width = len(placements[source].gpu_ids)
    per_rank = raw['num_experts'] // len(group)
    counts = [0] * raw['num_experts']
    assignment_flows, rank_flows = Counter(), Counter()
    node_bytes = 0
    tokens = 0
    rank_tokens = Counter()
    shift = ((raw.get('routing_seed',0)+104729*(layer_id+1)) % len(raw['routing_pattern'])
             if raw.get('routing_mode') == 'layerwise_periodic' and layer_id is not None else 0)
    for size, offset in spans:
        for cursor in range(offset, offset + size):
            tokens += 1
            targets = []
            for expert_id in raw['routing_pattern'][(cursor+shift) % len(raw['routing_pattern'])]:
                counts[expert_id] += 1
                targets.append(group[expert_id // per_rank])
            rank_tokens.update(set(targets))
            for coordinate, src in enumerate(placements[source].gpu_ids):
                # Enumerate channel IDs instead of reusing the runtime's formula.
                channels = len(range(coordinate, raw['hidden_size'], width))
                for target in targets:
                    dst = placements[target].gpu_ids[coordinate]
                    if src != dst:
                        assignment_flows[src, dst] += channels
                for target in set(targets):
                    dst = placements[target].gpu_ids[coordinate]
                    if src != dst:
                        rank_flows[src, dst] += channels
                nodes = {placements[t].gpu_ids[coordinate] // gpus_per_node for t in targets}
                node_bytes += channels * len(nodes - {src // gpus_per_node})
    return dict(tokens=tokens, assignments=counts,
                assignment_channels=assignment_flows, rank_channels=rank_flows,
                remote_node_channels=node_bytes, rank_tokens=dict(rank_tokens))


def stage_flows(reference, element_bytes, combine=False, rank_deduplicated=False):
    channels = reference['rank_channels' if rank_deduplicated else 'assignment_channels']
    return Counter({(dst, src, count * element_bytes) if combine else (src, dst, count * element_bytes): 1
                    for (src, dst), count in channels.items()})


def payload_totals(reference, raw, gpus_per_node):
    result = {}
    for stage in ('dispatch', 'combine'):
        unit = raw[stage + '_element_bytes']
        direct = reference['assignment_channels']
        ranks = reference['rank_channels']
        result[stage] = dict(
            direct_bytes=sum(direct.values()) * unit,
            direct_inter_node_bytes=sum(n for (s, d), n in direct.items()
                                        if s // gpus_per_node != d // gpus_per_node) * unit,
            distinct_rank_bytes=sum(ranks.values()) * unit,
            distinct_rank_inter_node_bytes=sum(n for (s, d), n in ranks.items()
                                               if s // gpus_per_node != d // gpus_per_node) * unit,
            single_copy_per_remote_node_bytes=reference['remote_node_channels'] * unit)
        modeled = ranks if raw.get('communication_mode') == 'token_rank' else direct
        result[stage].update(modeled_bytes=sum(modeled.values())*unit,
            modeled_inter_node_bytes=sum(n for (s,d),n in modeled.items()
                if s//gpus_per_node != d//gpus_per_node)*unit)
    return result


def audit_layout(config, profile):
    """Check real runtime flow generation against an independent integer oracle."""
    from ..backend.expert_parallel import ExpertParallelMixin
    from ..backend.expert_profile import validate_profile
    validate_profile(config, profile)
    if profile.expert is None:
        raise ValueError('EP audit requires expert_parallel.enabled=true and an EP profile')
    placements = {p.instance_id: p for p in config.topology.placements}
    groups = {source: group for group in config.expert_parallel.groups for source in group}
    runtime = SimpleNamespace(instances=placements, ep_groups=groups, profile=profile)
    period = len(profile.expert.raw['routing_pattern'])
    samples = sorted({(1, 0), (1, period - 1), (period + 1, 1), (period, 0)})
    phases = {}
    cases = 0
    deduplicate = profile.expert.communication_mode == 'token_rank'
    ids = profile.expert.raw['prefill_layer_ids']
    layers = (sorted({ids[0],ids[len(ids)//2],ids[-1]})
              if profile.expert.routing_mode == 'layerwise_periodic' else [None])
    for source, group in groups.items():
        for layer, (tokens, offset) in ((layer,sample) for layer in layers for sample in samples):
            reference = token_reference(profile.expert, group, placements, source,
                                        config.topology.gpus_per_node, [(tokens, offset)], layer)
            counts, rank_tokens = profile.expert.routing_counts([(tokens,offset)],len(group),layer)
            if counts != reference['assignments'] or sum(counts) != tokens * profile.expert.raw['top_k']:
                raise ValueError('EP routing count mismatch: ' + source)
            for combine, stage in ((False, 'dispatch'), (True, 'combine')):
                actual = Counter(ExpertParallelMixin.ep_flows(runtime, source, counts, combine,
                    **(dict(rank_tokens=rank_tokens) if deduplicate else {})))
                expected = stage_flows(reference, profile.expert.raw[stage + '_element_bytes'], combine, deduplicate)
                if actual != expected:
                    raise ValueError('EP flow payload/endpoint mismatch: ' + source + '/' + stage)
            cases += 1
            if (tokens, offset) == (period, 0) and layer == layers[0]:
                phase = placements[source].role
                row = phases.setdefault(phase, dict(source_count=0, tokens=0,
                    assignments=0, dispatch=Counter(), combine=Counter()))
                row['source_count'] += 1
                row['tokens'] += tokens
                row['assignments'] += sum(counts)
                totals = payload_totals(reference, profile.expert.raw, config.topology.gpus_per_node)
                for stage in totals:
                    row[stage].update(totals[stage])
    return dict(status='passed',checked_source_cursor_cases=cases,
                checked_stages=cases*2,period_tokens=period,phase_period_totals=phases,
                layer_ids_checked=layers,period_totals_layer_id=layers[0],
                production_model=('token_target_rank_deduplicated_activation_payload' if deduplicate else
                                  'direct_token_expert_assignment_activation_payload'),
                reference_scope='activation payload only; no metadata, scales, alignment, control packets or forwarding latency',
                node_reference='one activation per token per remote physical node; not a simulated DeepEP transport')
