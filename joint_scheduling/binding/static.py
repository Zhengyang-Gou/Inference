"""Arrival-time P round robin and independently shuffled, balanced D routing.

Only request identity/order, placement and the binding seed enter this plan.
No lengths, future completions, network seed or scheduler state are consulted.
"""
import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict

from . import Binding
from ..validation import ConfigError, integer


def endpoint_policy(settings):
    if settings.joint_selection_enabled and settings.policy == 'joint_lexicographic':
        return 'joint_lexicographic'
    return 'balanced_random' if settings.policy == 'balanced_random' else 'round_robin'


def validate_static_settings(settings):
    if settings.policy != 'balanced_random':
        return
    integer(settings.seed, 'binding.seed')
    if settings.seed >= 2147483647:
        raise ConfigError('binding.seed must be below 2147483647')
    if settings.window_enabled or settings.joint_selection_enabled:
        raise ConfigError('balanced_random binds at arrival; disable binding windows and joint selection')


def plan_bindings(requests, topology, settings):
    """Return an immutable plan in the same stable order as load_workload.

Legacy policies keep their initial round-robin layout; joint/window dispatch
may subsequently replace that layout through its existing runtime path.
"""
    validate_static_settings(settings)
    ps = [p.instance_id for p in topology.placements if p.role == 'prefill']
    ds = [p.instance_id for p in topology.placements if p.role == 'decode']
    rng = random.Random(settings.seed)
    order, bindings = list(ds), []
    for index, row in enumerate(sorted(requests, key=lambda r: (r['arrival_seconds'], r['request_id']))):
        if settings.policy == 'balanced_random' and index % len(ds) == 0:
            order = list(ds)
            rng.shuffle(order)
        bindings.append(Binding(row['request_id'], ps[index % len(ps)], order[index % len(ds)]))
    return tuple(bindings)


def binding_plan_document(requests, topology, settings):
    if endpoint_policy(settings) != 'balanced_random':
        raise ValueError('a frozen balanced-random plan requires binding.policy=balanced_random')
    bindings = plan_bindings(requests, topology, settings)
    placements = {p.instance_id: p for p in topology.placements}
    def leaf(gpu):
        return gpu // topology.gpus_per_node // topology.nodes_per_leaf
    rows = []
    for binding in bindings:
        p = placements[binding.prefill_instance_id]
        d = placements[binding.decode_instance_id]
        rows.append(dict(asdict(binding),
            prefill_leaf_ids=sorted({leaf(g) for g in p.gpu_ids}),
            decode_leaf_ids=sorted({leaf(g) for g in d.gpu_ids}),
            cross_leaf=any(leaf(a) != leaf(b) for a, b in zip(p.gpu_ids, d.gpu_ids))))
    identity = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()
    cross = sum(row['cross_leaf'] for row in rows)
    p_counts = Counter(b.prefill_instance_id for b in bindings)
    d_counts = Counter(b.decode_instance_id for b in bindings)
    return dict(schema_version=1, policy='balanced_random', seed=settings.seed,
        request_order='arrival_seconds_then_request_id', selection_time='request_arrival',
        prefill_selection='round_robin', decode_selection='reshuffle_each_pool_cycle',
        bindings_sha256=hashlib.sha256(identity).hexdigest(), request_count=len(rows),
        prefill_counts={p.instance_id: p_counts[p.instance_id] for p in topology.placements if p.role == 'prefill'},
        decode_counts={p.instance_id: d_counts[p.instance_id] for p in topology.placements if p.role == 'decode'},
        cross_leaf_requests=cross, cross_leaf_fraction=cross/len(rows) if rows else None,
        bindings=rows)


def plan_summary(document):
    return {key: value for key, value in document.items() if key != 'bindings'}
