"""Commander independent static review. Never imports a simulator or starts a backend."""
import csv, hashlib, json, re, statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path('/home/s223/m0nesy/Inference')
R=ROOT/'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02'
OLD=ROOT/'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
A=ROOT/'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit'
T=A/'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
D=T/'results/diagnostic'
checks=[]
def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
def csvrows(p):
 with p.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))
def test(name,ok,detail=None): checks.append(dict(check=name,passed=bool(ok),detail=detail))
def tree():
 rows=[dict(path=str(p.relative_to(ROOT)),bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(OLD.rglob('*')) if p.is_file()]
 h=hashlib.sha256()
 for x in rows:h.update((x['path']+':'+x['sha256']+'\n').encode())
 return dict(files=rows,file_count=len(rows),tree_sha256=h.hexdigest())

before=tree()
manifest=load(ROOT/'coordination/history/2026-11-07/ARCHIVE.json')
pkg=ROOT/'artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz'
test('archive_package_identity',sha(pkg)==manifest['archive']['sha256'] and pkg.stat().st_size==manifest['archive']['bytes'])
bad=[]; live_bad=[]; live_count=0
for e in manifest['files']:
 p=A/e['path']
 if not p.is_file() or p.stat().st_size!=e['bytes'] or sha(p)!=e['sha256']:bad.append(e['path'])
 if e['tracked_snapshot']:
  live_count+=1
  if not (ROOT/e['path']).is_file() or sha(ROOT/e['path'])!=e['sha256']:live_bad.append(e['path'])
test('323_extracted_and_262_tracked_hashes',len(manifest['files'])==323 and live_count==262 and not bad and not live_bad,dict(extracted_mismatches=bad,live_mismatches=live_bad))
expected_tree=load(R/'results/t003-original-tree-before.json')
test('original_T003_matches_reported_tree',all(before[k]==expected_tree[k] for k in before),dict(actual_tree=before['tree_sha256'],expected_tree=expected_tree['tree_sha256'],actual_files=before['file_count'],expected_files=expected_tree['file_count']))
sources=load(R/'results/static-recomputation.json')['sources']
test('r02_recompute_source_hashes',all(sha(A/p)==v for p,v in sources.items()))

# Identity and actual completion.
frozen=ROOT/'outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen'
workload=load(T/'inputs/workload.json')
test('original_eight_request_prefix',workload['requests']==load(frozen/'workload.json')['requests'][:8])
plan=load(T/'inputs/binding_plan.json');parent={r['request_id']:r for r in load(frozen/'binding_plan.json')['bindings']}
test('parent_binding_projection',len(plan['bindings'])==8 and all(b==parent[b['request_id']] for b in plan['bindings']))
test('estimated_profile_unchanged',sha(T/'inputs/compute_profile.json')==sha(frozen/'compute_profile.json'))
req={r['request_id']:r for r in csvrows(D/'run/result/requests.csv')}
tok=csvrows(D/'run/result/tokens.csv')
token_ns={(r['request_id'],int(r['token_index'])):round(float(r['returned_at_seconds'])*1e9) for r in tok}
test('8_completed_550_tokens',len(req)==8 and len(tok)==550 and len(token_ns)==550 and all(r['status']=='completed' for r in req.values()))
test('token_indices_and_counts',all(sorted(k[1] for k in token_ns if k[0]==r['request_id'])==list(range(1,r['output_tokens']+1)) for r in workload['requests']))
summary=load(D/'run/result/summary.json')
test('cohort_failed_incomplete_zero',summary['arrival_cohort']['failed_count']==summary['arrival_cohort']['incomplete_count']==0)
resources=load(D/'run/final_resources.json')
test('32_memory_domains_released',len(resources)==32 and all(v['occupied_bytes']==v['reserved_bytes']==0 for v in resources.values()))
ep=load(D/'run/expert_parallel_summary.json')
test('32074_EP_calls_no_active_residue',ep['started_calls']==ep['completed_calls']==32074 and all(ep[k]=={} for k in ('active_calls_at_end','active_gpu_tasks_at_end','queued_gpu_tasks_at_end','planned_phase_demands_at_end')))

# Complete, unique raw replay completions, rather than summary-only assertions.
flows=csvrows(D/'flows.csv');n=len(flows)
classes=Counter(f['kind'] for f in flows)
test('flow_class_counts',classes=={'kv':671,'collective':201006})
acks={};completion_bad=[]
for variant in ('both','collective','kv'):
 vals=[None]*n;seen=set()
 for row in csvrows(D/('replay-'+variant)/'completions.csv'):
  i=int(row['flow_index']);v=int(row['ack_ns'])
  if not 0<=i<n or i in seen:completion_bad.append([variant,i]);continue
  vals[i]=v;seen.add(i)
 expected={i for i,f in enumerate(flows) if variant=='both' or f['kind']==variant}
 test('completion_identity.'+variant,seen==expected and all(vals[i]>=int(flows[i]['start_ns']) for i in seen))
 acks[variant]=vals
test('completion_no_duplicate_or_invalid_ID',not completion_bad,completion_bad)
both=acks['both']
test('both_ACK_exactly_reproduces_live',max(abs(both[i]-int(f['ack_ns'])) for i,f in enumerate(flows))==0)
deltas={k:[both[i]-acks[k][i] for i,f in enumerate(flows) if f['kind']==k] for k in ('kv','collective')}
traffic={k:dict(count=len(v),minimum_ns=min(v),maximum_ns=max(v),mean_ns=statistics.mean(v),
 positive=sum(x>0 for x in v),negative=sum(x<0 for x in v),nonzero=sum(x!=0 for x in v),
 over_1us=sum(x>1000 for x in v),below_minus_1us=sum(x<-1000 for x in v)) for k,v in deltas.items()}
test('reported_signed_flow_deltas',traffic['kv']['maximum_ns']==144 and traffic['kv']['positive']==3 and traffic['kv']['negative']==0 and traffic['collective']['maximum_ns']==136 and traffic['collective']['minimum_ns']==-127 and traffic['collective']['positive']==5 and traffic['collective']['negative']==1,traffic)
comparison_count=0;comparison_bad=[]
for row in csvrows(T/'results/flow-comparison.csv'):
 i=int(row['flow_index']);comparison_count+=1
 if int(row['delta_ack_ns'])!=both[i]-acks[flows[i]['kind']][i]:comparison_bad.append(i)
test('all_flow_comparison_rows_recomputed',comparison_count==n and not comparison_bad,comparison_bad[:5])
pairs=defaultdict(set)
for f in flows:pairs[f['source_endpoint'],f['destination_endpoint']].add(f['kind'])
test('endpoint_pairs_separate_by_class',all(len(v)==1 for v in pairs.values()))

stages=load(D/'stages.json')
stage_both=[s['local_ack_ns'] for s in stages];stage_no_kv=stage_both.copy()
for i,f in enumerate(flows):
 si=int(f['stage_index'])
 if si>=0:
  stage_both[si]=max(stage_both[si],both[i]);stage_no_kv[si]=max(stage_no_kv[si],acks['collective'][i])
stage_delta=[a-b for a,b in zip(stage_both,stage_no_kv)]
test('64148_stage_gates_exactly_reproduce_live',len(stages)==64148 and max(abs(stage_both[i]-s['ack_ns']) for i,s in enumerate(stages))==0)
nonzero_stage=[dict(stage_index=i,delta_ns=d,**stages[i]) for i,d in enumerate(stage_delta) if d]
test('one_9ns_stage_difference',len(nonzero_stage)==1 and nonzero_stage[0]['delta_ns']==9,nonzero_stage)

# Stream the original 161MB event links; retain only identity/boundary records.
calls={};stage_links={};batches={};boundaries=[];kv_completed={};transfer_bad=[]
link_counts=Counter()
with (T/'results/event-links.jsonl').open() as h:
 for line in h:
  e=json.loads(line);kind=e['link_type'];link_counts[kind]+=1
  if kind=='ep_call':calls[e['ep_call_id']]=e
  elif kind=='ep_stage':stage_links[e['stage_index']]=e
  elif kind=='batch' and e['event'].endswith('_started'):batches[e['batch_id']]=e
  elif kind=='boundary':boundaries.append(e)
  elif kind=='kv_transfer_completed':kv_completed[e['transfer_id']]=e
  elif kind=='transfer' and e.get('flow_index') is not None:
   i=e['flow_index'];f=flows[i]
   if e['time_ns']!=int(f['start_ns']) or e['request_id']!=f['request_id'] or e['source_endpoint']!=int(f['source_endpoint']) or e['destination_endpoint']!=int(f['destination_endpoint']) or e['size_bytes']!=int(f['payload_bytes']):transfer_bad.append(i)
test('all_flow_identity_links_and_stage_links',not transfer_bad and len(calls)==32074 and set(stage_links)==set(range(len(stages))),transfer_bad[:5])
test('all_stages_link_to_correct_calls',all(e['ep_call_id']==stages[i]['call_id'] and e['collective_type']==stages[i]['kind'] and stages[i]['phase']==calls[e['ep_call_id']]['phase'] for i,e in stage_links.items()))
inputs={r['request_id']:r['input_tokens'] for r in workload['requests']}
gate_rows=[];gate_bad=[]
for rid in req:
 ids=[i for i,f in enumerate(flows) if f['kind']=='kv' and f['request_id']==rid]
 p=max(e['time_ns'] for e in boundaries if e.get('event')=='prefill_chunk_completed' and e.get('request_id')==rid)
 kv_live=[e['time_ns'] for e in boundaries if e.get('event')=='kv_all_received' and e.get('request_id')==rid]
 b=max(both[i] for i in ids);isolated=max(acks['kv'][i] for i in ids)
 row=dict(request_id=rid,prefill_end_ns=p,first_token_ns=token_ns[rid,1],KV_both_ns=b,KV_isolated_ns=isolated,delta_G_ns=max(p,b)-max(p,isolated))
 ok=len(kv_live)==1 and kv_live[0]==b and p==token_ns[rid,1]
 if not ok:gate_bad.append(row)
 gate_rows.append(row)
test('8_KV_gates_and_first_tokens_independently_rebuilt',not gate_bad and len(gate_rows)==8 and all(r['delta_G_ns']==0 for r in gate_rows),gate_bad)
kvlinks=load(T/'results/kv-transfer-links.json')
test('671_KV_payload_and_ACK_identities',len(kvlinks)==671 and sum(e['size_bytes'] for e in kvlinks.values())==274974336 and all(e['ack_ns']==both[e['flow_index']]==kv_completed[k]['time_ns'] for k,e in kvlinks.items()))
mapped={};map_bad=[]
for i,stage in enumerate(stages):
 call=calls[stage['call_id']]
 if stage['kind']!='ep_combine' or call['layer_id']!=60:continue
 batch=batches[call['batch_id']]
 if batch['batch_size']!=1:map_bad.append(dict(stage=i,reason='unexpected batch size'));continue
 rid=batch['request_ids'][0]
 if call['phase']=='prefill':
  cached=batch['cached_tokens'][rid];query=batch['scheduled_tokens'][rid]
  if cached+query!=inputs[rid]:continue
  index=1
 else:
  context=batch['context_tokens'][0]
  if call['context_tokens']!=batch['context_tokens']:map_bad.append(dict(stage=i,reason='context mismatch'))
  index=context-inputs[rid]+2
 key=(rid,index)
 if key in mapped or key not in token_ns or stage_both[i]!=token_ns.get(key):map_bad.append(dict(stage=i,request_id=rid,index=index,reason='duplicate/unmatched token or time mismatch'))
 mapped[key]=dict(request_id=rid,token_index=index,stage_index=i,delta_ns=stage_delta[i],token_ns=token_ns.get(key))
test('all_550_token_final_combine_identities',not map_bad and set(mapped)==set(token_ns),map_bad[:8])
test('all_550_final_boundaries_zero_exposure',len(mapped)==550 and all(e['delta_ns']==0 for e in mapped.values()))
impact=load(T/'results/request-impact-summary.json')
test('end_to_end_counterfactual_not_measured',impact['layer3_quantified_end_to_end_slo_change']['status']=='not_measured')

# Native calendar and stopping protocol, not only wall-clock success.
native_counts=Counter();send_indices=[]
with (D/'native_schedule.jsonl').open() as h:
 for line in h:
  e=json.loads(line);cmd=e['command'].split()[0];native_counts[cmd]+=1
  if cmd=='SEND':send_indices.append(e['flow_index'])
test('native_calendar_complete',native_counts=={'TIMER':298164,'SEND':201677,'LOCAL_PORTS':175334} and len(set(send_indices))==n and set(send_indices)==set(range(n)),dict(native_counts))
attempts=[]
for p in sorted((T/'logs').glob('*console*.log')):
 text=p.read_text()
 for marker,ts in re.findall(r'^(LIVE\d*_START)=(\S+)$',text,re.M):
  exitname=marker.replace('_START','_EXIT')
  exitm=re.search(r'^'+re.escape(exitname)+r'=(-?\d+)$',text,re.M)
  attempts.append(dict(log=p.name,start=ts,exit_code=int(exitm[1]) if exitm else None))
attempts.sort(key=lambda e:e['start'])
failed_rpc=load(T/'results/attempt1-failed/run/network/backend_rpc_summary.json')
failed_live=load(T/'results/attempt1-failed/live-stage.json')
test('two_live_starts_first_already_entered_network',len(attempts)==2 and attempts[0]['exit_code']==1 and attempts[1]['exit_code']==0 and failed_rpc['time_ns']==472621 and failed_rpc['submission_commands']==12 and failed_live['cross_node_flows']==1,dict(attempts=attempts,first_time_ns=failed_rpc['time_ns'],first_native_commands=failed_rpc['submission_commands']))
corrected=load(R/'results/acceptance-summary.corrected.json');deviations=load(R/'results/deviation-register.json')
test('A6_failed_and_not_all_passed',corrected['all_passed'] is False and next(x for x in corrected['items'] if x['id']=='A6')['status']=='failed' and corrected['process_gate']['live_start_count']==2 and corrected['process_gate']['status']=='failed')
test('four_process_findings_registered',{v['id'] for v in corrected['process_gate']['violations']}=={'V1','V2','V3','V4'})
reported=load(R/'results/static-recomputation.json')
test('r02_source_statistics_match_independent_results',reported['traffic_classes']['kv']['added_latency_ns']['maximum']==traffic['kv']['maximum_ns'] and reported['traffic_classes']['collective']['added_latency_ns']['minimum']==traffic['collective']['minimum_ns'] and reported['ep_stages']['max_added_ns']==9)
locators=[]
for rel in ['results/attempt1-failed/live-stage.json','results/attempt1-failed/run/network/backend_rpc_summary.json','results/diagnostic/flows.csv','results/diagnostic/stages.json','results/diagnostic/native_schedule.jsonl','results/flow-comparison.csv','results/stage-comparison.csv','results/port-intervals.csv','results/event-links.jsonl']:
 p=T/rel
 locators.append(dict(original_relative_path=rel,server_tracked_copy_exists=(OLD/rel).exists(),resolved_historical_path=str(p),sha256=sha(p),bytes=p.stat().st_size))
test('evidence_paths_resolve_in_verified_archive',all(Path(e['resolved_historical_path']).is_file() for e in locators))
test('original_task_files_preserved_during_review',tree()==before)
report=dict(reviewed_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),reviewer='commander',scope='Independent server static review of T003-r02 corrections and historical evidence. No simulation/replay.',
checks_total=len(checks),passed=sum(c['passed'] for c in checks),failed=[c for c in checks if not c['passed']],checks=checks,
traffic=traffic,nonzero_stage=nonzero_stage,gate_rows=gate_rows,token_boundaries=list(mapped.values()),native_counts=dict(native_counts),attempts=attempts,
parent_experiment=dict(A6='failed',all_passed=False,scientific_data='Historical diagnostic evidence with recorded process deviations; not a new server experiment'),
original_tree=before['tree_sha256'],evidence_locations=locators,archive_sha256=manifest['archive']['sha256'])
(R/'results/review-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(checks_total=report['checks_total'],passed=report['passed'],failed=report['failed'],traffic=traffic,stage_nonzero=nonzero_stage,gate_count=len(gate_rows),token_boundaries=len(mapped),parent_A6='failed',simulation_starts=0),ensure_ascii=False,indent=2))
raise SystemExit(0 if not report['failed'] else 1)
