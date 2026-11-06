"""Frozen network attribution, migrated from the Azure diagnostic.

No simulator monkey patches. The live run publishes observational copies via
RuntimeHooks; replay preserves starts, bytes, priorities and endpoint order.
"""
import copy
import csv
import hashlib
import json
import statistics
import time
from array import array
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from ..config import load_config
from ..backend.compute_profile import profile_kind
from .provenance import freeze_source, record_run, write_json, sha256

def quantile(values,q):
    if not values:return None
    values=sorted(values);pos=(len(values)-1)*q;i=int(pos)
    return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(pos-i)

def stats(values):
    return dict(count=len(values),mean=statistics.mean(values) if values else None,
                p50=quantile(values,.5),p95=quantile(values,.95),maximum=max(values,default=None))

class Probe:
    def __init__(self, config):
        self.topology=config.topology
        self.gpus_per_node=config.topology.gpus_per_node
        # row: start_ns, ack_ns, source_endpoint, destination_endpoint, bytes, kind,
        #      priority, stage_index, request_id
        self.flows=[];self.active={};self.local_active={};self.stages=[];self.stage_ids={};self.calls={}
        self.counts=defaultdict(lambda:[0,0]);self.endpoint_overlap=defaultdict(float)
        self.endpoint_last={};self.shared=set();self.global_last=0;self.global_overlap_ns=0
        self.previous=None;self.ports={};self.next_progress=time.monotonic()+15
        self.native_schedule=[];self.last_remote_index=None

    def native_event(self, registration):
        row=dict(registration)
        row['flow_index']=self.last_remote_index if row['command'].startswith('SEND ') else None
        if row['flow_index'] is not None:
            flow=self.flows[row['flow_index']]
            assert flow[0]==row['time_ns'],'native SEND detached from logical transfer'
        self.native_schedule.append(row)

    def touch(self,now,endpoints,kind,change):
        self.global_overlap_ns+=(now-self.global_last)*bool(self.shared);self.global_last=now
        column=int(kind=='collective')
        for endpoint in endpoints:
            counts=self.counts[endpoint]
            if counts[0] and counts[1]:self.endpoint_overlap[endpoint]+=now-self.endpoint_last.get(endpoint,now)
            self.endpoint_last[endpoint]=now;counts[column]+=change
            assert min(counts)>=0
            if counts[0] and counts[1]:self.shared.add(endpoint)
            else:self.shared.discard(endpoint)

    def event(self,event,now,details):
        if event=='ep_call_started':self.calls[details['ep_call_id']]=details['phase']
        if event=='collective_started' and details.get('collective_type') in ('ep_dispatch','ep_combine'):
            key=(details['ep_call_id'],details['collective_type'])
            self.stage_ids[key]=len(self.stages)
            self.stages.append(dict(call_id=key[0],kind=key[1],phase=self.calls[key[0]],start_ns=now,
                                    ack_ns=-1,local_ack_ns=now))
        elif event=='collective_completed' and details.get('collective_type') in ('ep_dispatch','ep_combine'):
            stage=self.stage_ids[(details['ep_call_id'],details['collective_type'])]
            self.stages[stage]['ack_ns']=now
        if event=='transfer_started':
            self.last_remote_index=None
            source=details['source_gpu_id']//self.gpus_per_node
            dest=details['destination_gpu_id']//self.gpus_per_node
            kind=details['transfer_kind'];stage=-1
            if details.get('collective_type') in ('ep_dispatch','ep_combine'):
                stage=self.stage_ids[(details['ep_call_id'],details['collective_type'])]
            if source==dest:
                if stage>=0:self.local_active[details['transfer_id']]=stage
                return
            source=self.topology.network_endpoint(details['source_gpu_id'])
            dest=self.topology.network_endpoint(details['destination_gpu_id'])
            if kind=='collective':assert stage>=0,'TP must stay node-local'
            self.active[details['transfer_id']]=len(self.flows)
            self.last_remote_index=len(self.flows)
            self.flows.append([now,-1,source,dest,details['size_bytes'],kind,details['priority'],stage,details['request_id']])
            self.touch(now,('tx:'+str(source),'rx:'+str(dest)),kind,1)
        elif event=='transfer_completed':
            identifier=details['transfer_id']
            if identifier in self.local_active:
                stage=self.local_active.pop(identifier)
                self.stages[stage]['local_ack_ns']=max(self.stages[stage]['local_ack_ns'],now)
            index=self.active.pop(identifier,None)
            if index is not None:
                row=self.flows[index];row[1]=now
                self.touch(now,('tx:'+str(row[2]),'rx:'+str(row[3])),row[5],-1)
        if time.monotonic()>=self.next_progress:
            print('baseline progress',now/1e9,'seconds;',len(self.flows),'inter-node flows',flush=True)
            self.next_progress=time.monotonic()+15

    def sample(self,state):
        if self.previous is not None:
            dt=(state['time_seconds']-self.previous['time_seconds'])
            if dt>0:
                for key,row in state['resources'].items():
                    if key.startswith('local:'):continue
                    old=self.previous['resources'][key]
                    p=self.ports.setdefault(key,dict(node_id=row['node_id'],peer_id=row['peer_id'],capacity_bps=row['capacity_bps'],
                        tx_bytes=0,kv_bytes=0,ep_bytes=0,peak_queue_bytes=0,peak_1ms_utilization=0.,pfc_pause_events=0,
                        mixed_1ms_samples=0,active_1ms_samples=0))
                    tx=row['tx_bytes']-old['tx_bytes'];kv=row['kv_bytes']-old['kv_bytes'];ep=row['collective_bytes']-old['collective_bytes']
                    p['tx_bytes']+=tx;p['kv_bytes']+=kv;p['ep_bytes']+=ep
                    p['pfc_pause_events']+=row['pfc_pause_count']-old['pfc_pause_count']
                    p['peak_queue_bytes']=max(p['peak_queue_bytes'],row['queue_bytes'])
                    p['peak_1ms_utilization']=max(p['peak_1ms_utilization'],tx*8/(dt*row['capacity_bps']))
                    p['mixed_1ms_samples']+=int(kv>0 and ep>0)
                    p['active_1ms_samples']+=int(tx>0)
        self.previous=state

def baseline(out,config,source_snapshot):
    from ..backend.runner import run_baseline
    from ..backend.execution import RuntimeHooks
    profile=config.profile_path()
    workload=json.loads(Path(config.runtime.workload_path).read_text())
    probe=Probe(config)
    began=time.monotonic()
    result=run_baseline(config,out/'run',hooks=RuntimeHooks(event=probe.event,network_sample=probe.sample,
                        native_schedule=probe.native_event),
                        source_snapshot=source_snapshot)
    assert not probe.active and not probe.local_active,'baseline stopped with unfinished flow'
    assert all(s['ack_ns']>=s['start_ns'] for s in probe.stages)
    with (out/'flows.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['start_ns','ack_ns','source_endpoint','destination_endpoint','payload_bytes','kind','priority','stage_index','request_id']);w.writerows(probe.flows)
    (out/'stages.json').write_text(json.dumps(probe.stages,separators=(',',':'))+'\n')
    with (out/'native_schedule.jsonl').open('w') as handle:
        for row in probe.native_schedule:handle.write(json.dumps(row,separators=(',',':'))+'\n')
    pairs=defaultdict(set)
    for r in probe.flows:pairs[r[2],r[3]].add(r[5])
    assert all(len(v)==1 for v in pairs.values()),'removing a class could change the other class port allocation'
    active_end=max(r[1] for r in probe.flows)/1e9
    for p in probe.ports.values():p['mean_utilization_during_flow_span']=p['tx_bytes']*8/(active_end*p['capacity_bps'])
    summary=dict(wall_seconds=time.monotonic()-began,flow_count=len(probe.flows),stage_count=len(probe.stages),active_span_seconds=active_end,
        endpoint_outstanding_overlap_seconds={k:v/1e9 for k,v in probe.endpoint_overlap.items()},
        any_endpoint_outstanding_overlap_seconds=probe.global_overlap_ns/1e9,
        ports=probe.ports,arrival_cohort=result.summary['arrival_cohort'],pressure=result.summary['pressure'],
        workload_provenance=workload['provenance'],profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        model_status=profile_kind(json.loads(profile.read_text())['provenance']),
        experiment_type='end_to_end',
        endpoint_pairs_separate_by_traffic_class=True)
    (out/'baseline_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('BASELINE COMPLETE',json.dumps({k:summary[k] for k in ['wall_seconds','flow_count','active_span_seconds','any_endpoint_outstanding_overlap_seconds']},ensure_ascii=False),flush=True)
    return config,probe,summary

def replay(out,config,probe,keep):
    """Native network replay at frozen starts; no scheduler or GPU changes."""
    from ..backend.client import Ns3Backend
    end=array('q',[-1])*len(probe.flows);completed=0;selected=0;next_progress=time.monotonic()+15
    baseline_ack=[r[1] for r in probe.flows]
    # Identify frozen traffic and source; this run never executes GPU/request scheduling.
    from ..backend.build import require_backend
    state=require_backend()
    record_run(out,out.parent/'config.json',out.parent/'inputs/compute_profile.json',
        out.parent/'inputs/workload.json',json.loads(state['manifest'].read_text()),
        'frozen_network_replay',out.parent/'inputs/source')
    prior=defaultdict(list);port_checks=0
    parts=defaultdict(int)
    for row in probe.native_schedule:
        if row['flow_index'] is not None:parts[row['flow_index']]+=1
    native_completed=set();selected_ids=set()
    with Ns3Backend(config.topology,out/'network',config.runtime.seed,config.runtime.congestion_control,
        config.runtime.packet_payload_bytes,config.runtime.switch_buffer_bytes,config.runtime.rpc_timeout_seconds,False) as backend:
        def done(index):
            nonlocal completed
            parts[index]-=1
            if parts[index]==0:
                end[index]=backend.now_ns;completed+=1
        for registration in probe.native_schedule:
            index=registration['flow_index']
            row=probe.flows[index] if index is not None else None
            if row is not None and keep!='both' and row[5]!=keep:continue
            at=registration['time_ns']
            while backend.now_ns < at:backend.advance(at/1e9)
            assert backend.now_ns==at
            if keep=='both':
                # A deadline at N can precede an already queued packet at N.
                # Deliver the original triggering completions before registering
                # their children; all starts remain frozen at the exact same N.
                for _ in range(len(registration['after_ids'])+1):
                    if all(i in native_completed for i in registration['after_ids']):break
                    backend.advance(at/1e9)
                assert all(i in native_completed for i in registration['after_ids']), 'native causal epoch differs'
            if row is not None:
                history=prior[row[2],row[3]]
                if len(history)>=64512:
                    previous=history[-64512]
                    assert end[previous]>=0 and at>=end[previous]+100000000,'port cooldown would alter fixed route identity'
                    assert at>=baseline_ack[previous]+100000000,'baseline port cooldown could alter fixed route identity'
                    port_checks+=1
                history.append(index)
            def native_done(native_id=registration['native_id'],flow_index=index):
                native_completed.add(native_id)
                if flow_index is not None:done(flow_index)
            backend.submit_frozen_event(registration['command'],native_done)
            if row is not None:
                selected_ids.add(index);selected=len(selected_ids)
            if time.monotonic()>=next_progress:
                print('replay',keep,'submitted',selected,'completed',completed,flush=True);next_progress=time.monotonic()+15
        selected=sum(1 for i,row in enumerate(probe.flows) if keep=='both' or row[5]==keep)
        deadline=max(config.runtime.time_limit_seconds,max(baseline_ack)/1e9+1.)
        while completed<selected and backend.now_seconds()<deadline:backend.advance(deadline)
        assert completed==selected,'replay did not drain'
        backend.run_until(deadline)
        snapshot=backend.network_snapshot()
    with (out/'completions.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['flow_index','ack_ns'])
        writer.writerows((i,n) for i,n in enumerate(end) if n>=0)
    report=dict(experiment_type='frozen_network_replay', selected_flows=selected,completed_flows=completed,port_cooldown_checks=port_checks,
        retained_start_times_payload_and_endpoint_order=True,network_priority_enabled=False,
        retained_native_timers_and_local_submissions=True,
        final_port_counters=snapshot['resources'])
    (out/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    print('REPLAY COMPLETE',keep,selected,flush=True)
    return end,report

def analyze(out,config,probe,baseline_summary,replays):
    full=replays['both'][0];ep=replays['collective'][0];kv=replays['kv'][0]
    errors=[full[i]-r[1] for i,r in enumerate(probe.flows)]
    max_error=max(abs(v) for v in errors)
    # Tiny 1 ns ties would be disclosed, rather than claimed exact.
    assert max_error<=1,'frozen both-traffic replay does not reproduce baseline transport'
    classes={}
    for kind,isolated in [('collective',ep),('kv',kv)]:
        ids=[i for i,r in enumerate(probe.flows) if r[5]==kind]
        classes[kind]=dict(flow_count=len(ids),payload_bytes=sum(probe.flows[i][4] for i in ids),
            full_flow_latency_us=stats([(full[i]-probe.flows[i][0])/1000 for i in ids]),
            isolated_flow_latency_us=stats([(isolated[i]-probe.flows[i][0])/1000 for i in ids]),
            added_latency_us=stats([(full[i]-isolated[i])/1000 for i in ids]),
            delayed_by_over_1us=sum(full[i]-isolated[i]>1000 for i in ids),
            faster_by_over_1us=sum(isolated[i]-full[i]>1000 for i in ids))
    stage_full=[s['local_ack_ns'] for s in probe.stages];stage_ep=stage_full.copy()
    for i,r in enumerate(probe.flows):
        if r[7]>=0:
            stage_full[r[7]]=max(stage_full[r[7]],full[i]);stage_ep[r[7]]=max(stage_ep[r[7]],ep[i])
    assert max(abs(stage_full[i]-s['ack_ns']) for i,s in enumerate(probe.stages))<=1
    stage_groups={}
    for phase in ('prefill','decode'):
        for kind in ('ep_dispatch','ep_combine'):
            ids=[i for i,s in enumerate(probe.stages) if s['phase']==phase and s['kind']==kind]
            stage_groups[phase+'.'+kind]=dict(count=len(ids),
                full_latency_us=stats([(stage_full[i]-probe.stages[i]['start_ns'])/1000 for i in ids]),
                no_kv_latency_us=stats([(stage_ep[i]-probe.stages[i]['start_ns'])/1000 for i in ids]),
                added_latency_us=stats([(stage_full[i]-stage_ep[i])/1000 for i in ids]),
                delayed_by_over_1us=sum(stage_full[i]-stage_ep[i]>1000 for i in ids))
    with (out/'ep_stage_comparison.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['phase','kind','full_latency_us','without_kv_us','added_us'])
        for i,s in enumerate(probe.stages):w.writerow([s['phase'],s['kind'],(stage_full[i]-s['start_ns'])/1000,
            (stage_ep[i]-s['start_ns'])/1000,(stage_full[i]-stage_ep[i])/1000])
    report=dict(profile_sha256=baseline_summary['profile_sha256'],model_status=baseline_summary['model_status'],experiment_type='frozen_network_replay',
        baseline_flow_replay_max_ack_error_ns=max_error,traffic_classes=classes,ep_stages=stage_groups,
        source_baseline=baseline_summary,method='Frozen native-network replay with all traffic, EP only, and KV only; same starts, bytes, ordering and separate per-pair port sequences. Local EP/TP completion times frozen for stage barriers.',
        limits=['Finite frozen trace diagnostic; no sustained-load capacity claim.',
                'Outstanding overlap includes pacing/ACK wait; 1ms mixed-byte samples do not resolve individual packet service.',
                'Counterfactual latency changes are transport effects at frozen starts, not end-to-end request or SLO gains.',
                'Profile estimation type and modeling assumptions are inherited from the frozen simulation input.'])
    (out/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print('ANALYSIS COMPLETE',json.dumps(dict(replay_error_ns=max_error,classes=classes,stages=stage_groups),ensure_ascii=False),flush=True)
    return report



def validate_plan(plan):
    """Reject experiments for which removing traffic changes route identity."""
    if len(plan['runs'])!=1 or plan['generator_spec'] is not None or 'comparison_audit' not in plan:
        raise ValueError('frozen replay requires one audited case/seed and a fixed workload')
    raw=copy.deepcopy(plan['base_config'])
    from .parameters import set_override as _set
    for key,value in plan['runs'][0]['parameters'].items():_set(raw,key,value)
    if (not raw.get('expert_parallel',{}).get('enabled') or raw['runtime'].get('network_priority_enabled')
            or not raw['feedback'].get('network_observation_enabled')
            or raw['feedback'].get('network_observation_interval_us',1000)!=1000):
        raise ValueError('frozen EP/KV replay requires EP, 1ms telemetry and native priority off')
    if raw['runtime']['kv_transfer_mode'] not in ('layer_ready_immediate','whole_ready_immediate'):
        raise ValueError('contention attribution uses an ungated immediate baseline')
    width=raw['topology']['gpus_per_node']
    if any(len({gpu//width for gpu in p['gpu_ids']})!=1 for p in raw['topology']['placements']):
        raise ValueError('TP must be node-local for EP-only network attribution')
    workload=json.loads(Path(plan['fixed_workload']).read_text())
    if not any(r['output_tokens']>1 for r in workload['requests']):
        raise ValueError('contention replay needs KV-producing requests')


def run_contention(plan, output_dir, progress=None, *, execution=None):
    """One specification runs live baseline, three replays, comparison and report."""
    validate_plan(plan)
    output=Path(output_dir).resolve()
    profile=Path(plan['profile']).read_bytes();workload=Path(plan['fixed_workload']).read_bytes()
    expected=plan['comparison_audit']['input_sha256']
    if hashlib.sha256(profile).hexdigest()!=expected['profile'] or hashlib.sha256(workload).hexdigest()!=expected['workload']:
        raise ValueError('contention inputs changed before freezing')
    output.mkdir(parents=True,exist_ok=False);inputs=output/'inputs';inputs.mkdir()
    write_json(output/'plan.json',plan);write_json(output/'comparison_audit.json',plan['comparison_audit'])
    if execution is not None:
        write_json(output/'execution.json',execution)
    (inputs/'compute_profile.json').write_bytes(profile);(inputs/'workload.json').write_bytes(workload)
    source=freeze_source(inputs/'source',(output,))
    raw=copy.deepcopy(plan['base_config']);from .parameters import set_override as _set
    for key,value in plan['runs'][0]['parameters'].items():_set(raw,key,value)
    raw['runtime'].update(seed=plan['runs'][0]['seed'],workload_path=str(inputs/'workload.json'))
    raw['feedback']['compute_profile_path']=str(inputs/'compute_profile.json')
    write_json(output/'config.json',raw);config=load_config(output/'config.json')
    try:
        config,probe,summary=baseline(output,config,source)
        replays={}
        for keep in ('both','collective','kv'):
            directory=output/('replay-'+keep);directory.mkdir()
            replays[keep]=replay(directory,config,probe,keep)
        comparison=analyze(output,config,probe,summary,replays)
        write_json(output/'replay_inputs.json',dict(experiment_type='frozen_network_replay',
            flow_sha256=sha256(output/'flows.csv'),stages_sha256=sha256(output/'stages.json'),
            native_schedule_sha256=sha256(output/'native_schedule.jsonl'),
            network_config_sha256=sha256(output/'config.json'),
            scope='Same starts, payloads, priorities and per-pair port order; local EP/TP stage ACK times held fixed.'))
        report=dict(schema_version=1,status='completed',experiment_type='frozen_network_replay',
            total_runs=1,successful_runs=1,failed_runs=0,live_baselines=1,frozen_replays=3,
            profile_status=summary['model_status'],request_count=len(probe.calls),
            flow_count=len(probe.flows),baseline_flow_replay_max_ack_error_ns=comparison['baseline_flow_replay_max_ack_error_ns'],
            interpretation='Transport attribution only. Read run/result/summary.json for live request metrics.',
            comparison='comparison.json',live_result='run/result/summary.json')
        report['request_count']=len(json.loads(workload)['requests'])
        if execution is not None:
            report['execution'] = execution
        write_json(output/'summary.json',report)
        (output/'report.md').write_text('# EP/KV 竞争诊断\n\n'
            f"端到端阶段：{report['request_count']} 请求，{len(probe.flows)} 条跨节点流。\n\n"
            f"固定流量阶段：both / EP only / KV only；both 重放最大 ACK 误差 {report['baseline_flow_replay_max_ack_error_ns']} ns。\n\n"
            f"模型状态：`{report['profile_status']}`。\n\n"
            '请求指标见 [端到端结果](run/result/summary.json)，网络干扰归因见 [comparison.json](comparison.json)。\n\n'
            '重放固定提交时间，不能将它的流时延差解释为请求 SLO 改善或持续负载承载率。\n')
        if progress:progress(dict(run_id='contention-diagnostic',status='completed'))
        return report
    except Exception as error:
        import traceback
        write_json(output/'error.json',dict(status='failed',error=str(error),traceback=traceback.format_exc()))
        raise RuntimeError('contention diagnostic failed; see '+str(output/'error.json')) from error
