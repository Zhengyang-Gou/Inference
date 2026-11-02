"""Uniform-layer chunked-prefill profile with explicit cached-context timing."""
import math


def validate_chunked(raw, number):
    if not isinstance(raw, dict) or not {'collectives_per_layer', 'groups'} <= set(raw) or set(raw)-{'collectives_per_layer', 'groups', 'linear_points', 'layer_overrides'}:
        raise ValueError('chunked prefill requires collectives_per_layer and groups')
    number(raw['collectives_per_layer'], 'chunk collectives', integral=True)
    if not isinstance(raw['groups'], list) or not raw['groups']:
        raise ValueError('chunked prefill groups must be nonempty')
    previous = 0
    for group in raw['groups']:
        if not isinstance(group, dict) or set(group) != {'chunk_tokens', 'points'}:
            raise ValueError('chunk group requires chunk_tokens and points')
        size = number(group['chunk_tokens'], 'chunk_tokens', True, True)
        if size <= previous:
            raise ValueError('chunk sizes must be unique and increasing')
        previous = size
        if not isinstance(group['points'], list) or not group['points']:
            raise ValueError('chunk context points must be nonempty')
        prior = -1
        for point in group['points']:
            if not isinstance(point, dict) or not {'cached_tokens', 'compute_seconds', 'embedding_seconds', 'collective_bytes'} <= set(point) or set(point)-{'cached_tokens', 'compute_seconds', 'embedding_seconds', 'collective_bytes','attention_seconds'}:
                raise ValueError('invalid chunk context point')
            cached = number(point['cached_tokens'], 'cached_tokens', integral=True)
            if cached <= prior:
                raise ValueError('chunk cached contexts must be unique and increasing')
            prior = cached
            number(point['compute_seconds'], 'chunk compute', True)
            number(point['embedding_seconds'], 'chunk embedding')
            number(point['collective_bytes'], 'chunk collective bytes', integral=True)
            if raw['collectives_per_layer'] and point['collective_bytes'] == 0:
                raise ValueError('enabled chunk collectives need positive message size')

    if 'linear_points' in raw:
        points=raw['linear_points']
        if not isinstance(points,list) or not points:
            raise ValueError('prefill batching needs nonempty linear_points')
        previous=0
        for point in points:
            if not isinstance(point,dict) or set(point)!={'tokens','compute_seconds','embedding_seconds','collective_bytes'}:
                raise ValueError('invalid prefill linear point')
            tokens=number(point['tokens'],'linear tokens',True,True)
            if tokens<=previous:raise ValueError('prefill linear tokens must increase')
            previous=tokens
            number(point['compute_seconds'],'linear compute',True)
            number(point['embedding_seconds'],'linear embedding')
            number(point['collective_bytes'],'linear collective bytes',integral=True)
            if raw['collectives_per_layer'] and point['collective_bytes']==0:
                raise ValueError('prefill batch collective must have positive bytes')
        for group in raw['groups']:
            for point in group['points']:
                number(point.get('attention_seconds'),'prefill attention')
    elif any('attention_seconds' in point for group in raw['groups'] for point in group['points']):
        raise ValueError('attention decomposition requires linear_points')
    overrides=raw.get('layer_overrides',[])
    if not isinstance(overrides,list):
        raise ValueError('chunk layer_overrides must be a list')
    seen=set()
    for override in overrides:
        if not isinstance(override,dict) or set(override)!={'layer_ids','profile'}:
            raise ValueError('chunk override requires layer_ids and profile')
        ids=override['layer_ids']
        if not isinstance(ids,list) or not ids:
            raise ValueError('chunk override needs nonempty layer_ids')
        for index in ids:
            number(index,'chunk override layer',integral=True)
            if index in seen:raise ValueError('chunk layer override IDs must be unique')
            seen.add(index)
        if not isinstance(override['profile'],dict) or 'layer_overrides' in override['profile']:
            raise ValueError('nested chunk layer overrides are unsupported')
        validate_chunked(override['profile'],number)


def interpolate_chunk(raw, chunk_tokens, cached_tokens):
    def bracket(rows, key, value):
        if not rows[0][key] <= value <= rows[-1][key]:
            raise ValueError('chunked prefill {}={} outside declared profile coverage [{}, {}]'.format(key, value, rows[0][key], rows[-1][key]))
        lo = max((r for r in rows if r[key] <= value), key=lambda r:r[key])
        hi = min((r for r in rows if r[key] >= value), key=lambda r:r[key])
        return lo, hi, 0 if lo is hi else (value-lo[key])/(hi[key]-lo[key])
    def blend(lo, hi, fraction):
        return {key:lo[key]+fraction*(hi[key]-lo[key]) for key in (('compute_seconds','embedding_seconds','collective_bytes','attention_seconds') if 'linear_points' in raw else ('compute_seconds','embedding_seconds','collective_bytes'))}
    left, right, fraction = bracket(raw['groups'], 'chunk_tokens', chunk_tokens)
    samples=[]
    for group in (left,right):
        lo, hi, weight = bracket(group['points'], 'cached_tokens', cached_tokens)
        samples.append(blend(lo,hi,weight))
    result = blend(*samples,fraction)
    result['collective_bytes'] = math.ceil(result['collective_bytes'])
    return result


def interpolate_linear(raw,tokens):
    points=raw.get('linear_points',[])
    if not points or not points[0]['tokens']<=tokens<=points[-1]['tokens']:
        raise ValueError('prefill batch total tokens outside decomposed linear profile coverage')
    lo=max((p for p in points if p['tokens']<=tokens),key=lambda p:p['tokens'])
    hi=min((p for p in points if p['tokens']>=tokens),key=lambda p:p['tokens'])
    weight=0 if lo is hi else (tokens-lo['tokens'])/(hi['tokens']-lo['tokens'])
    result={key:lo[key]+weight*(hi[key]-lo[key]) for key in ('compute_seconds','embedding_seconds','collective_bytes')}
    result['collective_bytes']=math.ceil(result['collective_bytes'])
    return result
