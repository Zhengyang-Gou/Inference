"""Preflight for matched fixed-input comparisons, independent of execution.

The contract audits requested and effective differences before any simulator
process or output directory is created. Existing unconstrained suites retain
their original failure-recording behavior.
"""
import copy
import hashlib
import itertools
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..backend.compute_profile import ComputeProfile
from ..validation import read_json
from ..workload.inputs import load_workload
from ..backend.compute_profile import profile_kind
from ..backend.expert_profile import validate_profile
from ..config import load_config
from ..binding.static import binding_plan_document, endpoint_policy, plan_summary
from ..workload.trace import profile_coverage
from .parameters import allowed_override as _allowed, set_override as _set


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _get(root,path):
    value=root
    for key in path.split('.'):
        if not isinstance(value,dict) or key not in value:
            raise ValueError('unknown comparison setting: '+path)
        value=value[key]
    return value


def _flatten(root,prefix=''):
    result={}
    for key,value in root.items():
        path=prefix+'.'+key if prefix else key
        if isinstance(value,dict):result.update(_flatten(value,path))
        else:result[path]=value
    return result


def _differences(left,right):
    a,b=_flatten(left),_flatten(right)
    return {k:dict(left=a.get(k),right=b.get(k)) for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k)}


def audit_comparison(plan,contract):
    required={'purpose','allowed_changes','fixed','input_sha256'}
    if not isinstance(contract,dict) or set(contract)!=required:
        raise ValueError('comparison requires purpose, allowed_changes, fixed and input_sha256')
    if contract['purpose'] not in ('kv_policy','network_priority','validation'):
        raise ValueError('unknown comparison purpose')
    allowed=contract['allowed_changes']
    if (not isinstance(allowed,list) or not allowed or any(not isinstance(k,str) or not _allowed(k) or k.startswith('workload.') for k in allowed)
            or len(set(allowed))!=len(allowed)):
        raise ValueError('comparison allowed_changes must list distinct implemented configuration fields')
    fixed=contract['fixed']
    if not isinstance(fixed,dict) or not fixed or set(fixed)&set(allowed):
        raise ValueError('comparison fixed settings must be nonempty and disjoint from allowed changes')
    if plan['generator_spec'] is not None or not plan['fixed_workload']:
        raise ValueError('matched comparison requires a frozen workload file, not a generator')
    identities=contract['input_sha256']
    if not isinstance(identities,dict) or set(identities)!={'profile','workload'}:
        raise ValueError('comparison must pin profile and workload SHA-256')
    actual={'profile':_sha(plan['profile']),'workload':_sha(plan['fixed_workload'])}
    for key,value in actual.items():
        if value!=identities[key]:raise ValueError('comparison '+key+' SHA-256 mismatch')
    base=copy.deepcopy(plan['base_config'])
    base['runtime']['workload_path']=plan['fixed_workload']
    variants={r['case_id']:r['parameters'] for r in plan['runs']}
    effective={};cfgs={}
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'config.json'
        for name,overrides in [('__base__',{})]+list(variants.items()):
            forbidden=set(overrides)-set(allowed)
            if forbidden:raise ValueError('comparison forbids overrides: '+', '.join(sorted(forbidden)))
            raw=copy.deepcopy(base)
            for key,value in overrides.items():_set(raw,key,value)
            path.write_text(json.dumps(raw,allow_nan=False))
            cfg=load_config(path);value=asdict(cfg);value.pop('config_path')
            value=json.loads(json.dumps(value))
            for key,wanted in fixed.items():
                if _get(value,key)!=wanted:raise ValueError('comparison fixed setting changed: '+key+' in '+name)
            effective[name]=value;cfgs[name]=cfg
    reference=effective.pop('__base__');config=cfgs.pop('__base__')
    cases={}
    for name,value in effective.items():
        changes=_differences(reference,value)
        if set(changes)-set(allowed):raise ValueError('unexpected effective comparison differences')
        cases[name]=dict(overrides=copy.deepcopy(variants[name]),differences_from_base=changes)
    pairwise=[]
    for left,right in itertools.combinations(effective,2):
        changes=_differences(effective[left],effective[right])
        if set(changes)-set(allowed):raise ValueError('unexpected pairwise comparison differences')
        pairwise.append(dict(left=left,right=right,differences=changes))
    profile=ComputeProfile(plan['profile'])
    for cfg in cfgs.values():validate_profile(cfg,profile)
    workload=read_json(plan['fixed_workload']);requests=workload['requests']
    if not requests:raise ValueError('matched comparison workload must be nonempty')
    coverage=profile_coverage(workload,profile)
    if coverage['out_of_coverage_count']:raise ValueError('comparison workload exceeds profile coverage')
    for cfg in cfgs.values():
        tp=profile.phases['prefill']['tp_size']
        if cfg.runtime.prefill_batch_token_budget:
            profile.validate_prefill_batch_coverage(max(r['input_tokens'] for r in requests),
                cfg.runtime.prefill_chunk_tokens,cfg.runtime.prefill_batch_token_budget,tp)
        decode=[r for r in requests if r['output_tokens']>1]
        if decode:
            profile.lookup_decode_batch([max(r['input_tokens']+r['output_tokens']-2 for r in decode)]*cfg.runtime.decode_batch_size,tp)
    locations={'runtime.seed','feedback.compute_profile_path','runtime.workload_path','evaluation.output_dir'}
    common={k:v for k,v in _flatten(reference).items() if k not in allowed and k not in locations}
    common_hash=hashlib.sha256(json.dumps(dict(settings=common,input_sha256=actual),sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
    provenance=workload.get('provenance',{})
    profile_status=profile_kind(profile.provenance)
    static_binding = None
    if endpoint_policy(config.binding) == 'balanced_random':
        ordered_requests, _ = load_workload(plan['fixed_workload'])
        document = binding_plan_document(ordered_requests, config.topology, config.binding)
        for cfg in cfgs.values():
            candidate = binding_plan_document(ordered_requests, cfg.topology, cfg.binding)
            if candidate != document:
                raise ValueError('matched comparison requires an identical frozen binding plan')
        static_binding = plan_summary(document)
    return dict(schema_version=1,purpose=contract['purpose'],passed=True,allowed_changes=allowed,fixed_settings=fixed,
        common_config_sha256=common_hash,input_sha256=actual,cases=cases,pairwise_differences=pairwise,
        seeds=plan['source_spec']['seeds'],pairing='All cases use identical frozen input bytes; network seed paired within each seed group.',
        static_binding=static_binding,
        model=dict(name=profile.raw.get('model'),status=profile_status,synthetic=profile.provenance['synthetic'],
            tp_size=profile.phases['prefill']['tp_size'],ep_size_unit=(
                'GPU_ranks' if config.expert_parallel.rank_unit == 'gpu' else 'TP_teams') if profile.expert else None,
            ep_teams_per_group=(len(config.expert_parallel.groups[0]) if len({len(g) for g in config.expert_parallel.groups}) == 1 else None) if profile.expert else None,
            model_group_gpus=(len(config.expert_parallel.groups[0])*profile.phases['prefill']['tp_size']
                if len({len(g) for g in config.expert_parallel.groups}) == 1 else None) if profile.expert else None,
            ep_sizes_by_phase={role: next(len(g) for g in config.expert_parallel.groups
                if next(p.role for p in config.topology.placements if p.instance_id == g[0]) == role)
                for role in ('prefill', 'decode')} if profile.expert else None,
            weights_bytes_per_gpu_by_phase=profile.weights_bytes_per_gpu_by_phase,
            ep_execution='asynchronous_source_batch_dispatch_expert_fifo_combine' if profile.expert else None),
        workload=dict(request_count=len(requests),source_format=provenance.get('source_format'),
            source_sha256=provenance.get('source_sha256'),arrival_time_scaled=provenance.get('arrival_time_scaled'),
            injection_duration_seconds=provenance.get('injection_duration_seconds'),actual_selected_rate_rps=provenance.get('actual_selected_rate_rps'),
            max_input_tokens=max(r['input_tokens'] for r in requests),max_output_tokens=max(r['output_tokens'] for r in requests)),
        topology=dict(gpus=config.topology.num_gpus,nodes=config.topology.num_nodes,gpus_per_node=config.topology.gpus_per_node,
            nic_bandwidth_gbps=config.topology.nic_bandwidth_gbps,nic_model=config.topology.nic_model,
            endpoint_bandwidth_gbps=config.topology.endpoint_bandwidth_gbps,
            num_leaves=config.topology.num_leaves,num_spines=config.topology.num_spines,
            uplink_bandwidth_gbps=config.topology.uplink_bandwidth_gbps,
            intra_node_model=config.topology.intra_node_model,
            intra_node_bandwidth_gbps=config.topology.intra_node_bandwidth_gbps),
        fixed_conditions=dict(packet_payload_bytes=config.runtime.packet_payload_bytes,prefill_chunk_tokens=config.runtime.prefill_chunk_tokens,
            prefill_batch_token_budget=config.runtime.prefill_batch_token_budget,prefill_batch_size=config.runtime.prefill_batch_size,
            decode_batch_size=config.runtime.decode_batch_size,observation_delay_us=config.feedback.observation_delay_us,
            observation_interval_us=config.feedback.network_observation_interval_us,
            ttft_slo_seconds=config.evaluation.default_ttft_slo_seconds,itl_slo_seconds=config.evaluation.default_itl_slo_seconds,
            window_start_seconds=config.evaluation.window_start_seconds,window_end_seconds=config.evaluation.window_end_seconds,
            time_limit_seconds=config.runtime.time_limit_seconds,
            slo_source='Explicit experiment assumptions unless individually supplied by the frozen workload.'),
        coverage=coverage)
