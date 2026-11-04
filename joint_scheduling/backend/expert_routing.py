"""Deterministic balanced routing subject to a model's expert-group limit."""
import math
from itertools import combinations

from ..validation import number


def balanced_group_pattern(num_experts, top_k, num_groups, topk_groups):
    """Cover every allowed group subset with uniform per-expert marginals.

    This is a synthetic routing table, not the model's learned gate scores.
    Every row chooses topk_groups groups and an equal number of experts in each.
    """
    for name, value in dict(num_experts=num_experts,top_k=top_k,
                            num_groups=num_groups,topk_groups=topk_groups).items():
        number(value,name,positive=True,integral=True)
    if (num_experts % num_groups or topk_groups > num_groups or top_k > num_experts or
            top_k % topk_groups or top_k // topk_groups > num_experts // num_groups):
        raise ValueError('balanced routing requires divisible experts/groups and an equal feasible top-k per selected group')
    width, selected = num_experts // num_groups, top_k // topk_groups
    strides = [s for s in range(1,width) if width // math.gcd(s,width) >= selected] or [1]
    subsets, seen = [], set()
    for groups in combinations(range(num_groups),topk_groups):
        if groups in seen:
            continue
        subsets.append(groups);seen.add(groups)
        # With half the groups selected, adjacent complementary rows balance
        # group demand even for short prompts, before a full period is seen.
        complement = tuple(g for g in range(num_groups) if g not in groups)
        if len(complement) == topk_groups and complement not in seen:
            subsets.append(complement);seen.add(complement)
    pattern = []
    for cycle in range(width):
        for index, groups in enumerate(subsets):
            offset = cycle + (index//2)*13 + (index%2)*7
            pattern.append([group*width+(offset+group*3+j*strides[(index+group) % len(strides)]) % width
                            for group in groups for j in range(selected)])
    return pattern
