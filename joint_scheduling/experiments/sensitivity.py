"""Matched profile perturbations with byte/communication invariants preserved."""
import copy
import math


def perturb_profile(profile, compute_scale=1.0, step_overhead_seconds=0.0):
    if not math.isfinite(compute_scale) or compute_scale<=0 or not math.isfinite(step_overhead_seconds) or step_overhead_seconds<0:
        raise ValueError('compute scale must be positive and overhead nonnegative')
    result=copy.deepcopy(profile)
    def scale(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('compute_seconds','embedding_seconds','attention_seconds'):value[key]=item*compute_scale
                else:scale(item)
        elif isinstance(value,list):
            for item in value:scale(item)
    scale(result['phases'])
    if 'expert_parallel' in result:
        for key in ('expert_compute_seconds_per_assignment','expert_compute_seconds_per_active_expert'):
            for phase in result['expert_parallel'].get(key,{}):
                result['expert_parallel'][key][phase]*=compute_scale
    if step_overhead_seconds:
        prefill=result['phases']['prefill']
        first=prefill['layers'][0] if 'layers' in prefill else prefill
        for group in [first]+first.get('cached_points',[]):
            for point in group['points']:point['compute_seconds']+=step_overhead_seconds
        if 'chunked' in prefill:
            for curve in [prefill['chunked']]+[o['profile'] for o in prefill['chunked'].get('layer_overrides',[])]:
                for point in curve.get('linear_points',[]):point['embedding_seconds']+=step_overhead_seconds
                for group in curve['groups']:
                    for point in group['points']:point['embedding_seconds']+=step_overhead_seconds
        decode=result['phases']['decode']
        for group in [decode]+decode.get('batches',[]):
            for point in group['points']:point['compute_seconds']+=step_overhead_seconds
    result['provenance']['sensitivity']=dict(compute_scale=compute_scale,step_overhead_seconds=step_overhead_seconds,
        uncertainty='Chosen perturbation scenarios, not measured error bounds or a confidence interval',
        overhead_semantics='One serial additive cost per prefill chunk/forward or decode batch; distributed by the existing compute-segment timing model')
    return result
