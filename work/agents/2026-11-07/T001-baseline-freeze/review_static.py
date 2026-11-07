import hashlib,json,sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from joint_scheduling.config import load_config
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import validate_profile
from joint_scheduling.backend.settings import CONTROLLED_KV_MODES
from joint_scheduling.binding.static import binding_plan_document
from joint_scheduling.workload.inputs import load_workload
root=Path.cwd(); task=root/'outputs/agents/2026-11-07/T001-baseline-freeze'; frozen=task/'results/frozen'
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
manifest=read(frozen/'manifest.json');raw=read(frozen/'config.json');base=read(root/'joint_scheduling/configs/research/base.json')
checks=[]
def check(name,ok,detail=None):
    checks.append(dict(check=name,passed=bool(ok),detail=detail))
for record in manifest['sources']:
    path=root/record['path'].replace('\\','/')
    check('source_identity:'+record['role'],sha(path)==record['sha256'] and path.stat().st_size==record['bytes'])
for record in manifest['frozen_files']:
    path=task/record['path'].replace('\\','/')
    check('frozen_identity:'+path.name,sha(path)==record['sha256'] and path.stat().st_size==record['bytes'])
check('workload_exact_copy',(frozen/'workload.json').read_bytes()==(root/'data/azure/conv_original.json').read_bytes())
check('profile_exact_copy',(frozen/'compute_profile.json').read_bytes()==(root/'joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json').read_bytes())
def flatten(value,prefix=''):
    result={}
    for key,item in value.items():
        name=prefix+'.'+key if prefix else key
        result.update(flatten(item,name) if isinstance(item,dict) else {name:item})
    return result
before=flatten(base);after=flatten(raw)
diff={k:after.get(k) for k in set(before)|set(after) if k not in before or k not in after or before[k]!=after[k]}
expected={'kv_scheduler.policy':'ready_immediate','kv_scheduler.slo_priority_enabled':False,
'kv_scheduler.collective_protection_enabled':False,'kv_scheduler.decode_admission_limit':0,
'runtime.kv_transfer_mode':'layer_ready_immediate','feedback.state_observation_enabled':True,
'feedback.compute_profile_path':'compute_profile.json','runtime.workload_path':'workload.json','evaluation.output_dir':'evaluation'}
check('exact_nine_authorized_config_differences',diff==expected,sorted(diff))
check('manifest_config_diff_matches_actual',set(diff)==set(manifest['config_field_diff_from_base']))
cfg=load_config(frozen/'config.json');profile=ComputeProfile(cfg.profile_path());rows,provenance=load_workload(frozen/'workload.json')
check('portable_profile_path',cfg.profile_path().resolve()==(frozen/'compute_profile.json').resolve())
check('portable_workload_path',(frozen/cfg.runtime.workload_path).resolve()==(frozen/'workload.json').resolve())
check('portable_evaluation_path',not Path(cfg.evaluation.output_dir).is_absolute())
check('main_layout',cfg.topology.num_nodes==4 and cfg.topology.num_gpus==32 and len(cfg.expert_parallel.groups)==2 and all(len(g)==16 for g in cfg.expert_parallel.groups))
check('tp1_all_instances',all(len(p.gpu_ids)==1 for p in cfg.topology.placements))
check('prefill_batch_settings',(cfg.runtime.prefill_chunk_tokens,cfg.runtime.prefill_batch_token_budget,cfg.runtime.prefill_batch_size)==(512,2048,16))
check('decode_settings',cfg.runtime.decode_batch_size==64 and profile.raw['phases']['decode']['layers']==['dense']*3+['moe']*58)
check('immediate_no_controlled_kv',cfg.runtime.kv_transfer_mode not in CONTROLLED_KV_MODES)
check('optimization_switches_off',not any((cfg.binding.window_enabled,cfg.binding.prediction_enabled,cfg.binding.joint_selection_enabled,cfg.prefix_cache.enabled,cfg.kv_scheduler.slo_priority_enabled,cfg.kv_scheduler.collective_protection_enabled,cfg.runtime.network_priority_enabled)))
check('observation_settings',(cfg.feedback.network_observation_enabled,cfg.feedback.state_observation_enabled,cfg.feedback.network_observation_interval_us,cfg.feedback.observation_delay_us)==(True,True,1000,100))
check('slo_values',(cfg.evaluation.default_ttft_slo_seconds,cfg.evaluation.default_itl_slo_seconds)==(1.0,.1))
check('window_and_limit',(cfg.evaluation.window_start_seconds,cfg.evaluation.window_end_seconds,cfg.runtime.time_limit_seconds)==(60.,3501.721937001,4101.721937001))
check('request_count_and_ids',len(rows)==19366 and len({r['request_id'] for r in rows})==19366)
check('no_request_slo_overrides',not any('ttft_slo_seconds' in r or 'itl_slo_seconds' in r for r in rows))
plan=read(frozen/'binding_plan.json');regenerated=binding_plan_document(rows,cfg.topology,cfg.binding)
check('project_environment_binding_plan_matches',plan==regenerated,regenerated['bindings_sha256'])
check('bindings_cover_requests',{r['request_id'] for r in rows}=={r['request_id'] for r in plan['bindings']} and len(plan['bindings'])==len(rows))
check('plan_content_hash',hashlib.sha256(json.dumps(plan['bindings'],sort_keys=True,separators=(',',':')).encode()).hexdigest()==plan['bindings_sha256'])
for side in ('prefill','decode'):
    check(side+'_allocation_counts',dict(Counter(r[side+'_instance_id'] for r in plan['bindings']))==plan[side+'_counts'])
max_input=max(r['input_tokens'] for r in rows)
max_runtime_context=max(r['input_tokens']+r['output_tokens']-2 for r in rows if r['output_tokens']>1)
min_runtime_context=min(r['input_tokens'] for r in rows if r['output_tokens']>1)
conservative_context=max(r['input_tokens']+r['output_tokens']-1 for r in rows)
profile.validate_prefill_batch_coverage(max_input,512,2048,1);validate_profile(cfg,profile)
for batch in range(1,65):
    for context in (min_runtime_context,max_runtime_context,conservative_context):
        profile.lookup_decode_batch([context]*batch,1)
check('prefill_ep_and_decode_batches_1_to_64_covered',True)
start_bytes=(task/'logs/baseline-status.txt').read_bytes();end_bytes=(task/'logs/final-status.txt').read_bytes()
check('executor_before_after_git_logs_equal',start_bytes==end_bytes)
check('manifest_baseline_status_raw_hash',hashlib.sha256(start_bytes).hexdigest()==manifest['git']['baseline_status_sha256'])
metadata=dict(evidence_type='independent_review_static_only_no_simulation',python=sys.version,python_executable=sys.executable,
max_runtime_decode_context_tokens=max_runtime_context,reported_conservative_context_tokens=conservative_context,
context_note='Input audit uses input+output-1 (conservative final-KV extent); actual last Decode lookup uses input+output-2. Both are covered.',
checks=checks,checks_total=len(checks),failures=[c for c in checks if not c['passed']],frozen_config_sha256=sha(frozen/'config.json'),frozen_manifest_sha256=sha(frozen/'manifest.json'))
(task/'results/review-verification.json').write_text(json.dumps(metadata,indent=2)+'\n')
print(json.dumps({k:v for k,v in metadata.items() if k!='checks'},indent=2))
raise SystemExit(bool(metadata['failures']))