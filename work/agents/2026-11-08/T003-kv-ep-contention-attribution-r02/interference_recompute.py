# T003-r02 read-only static recomputation from WSL historical raw CSVs. No simulation.
import csv, hashlib, json, statistics
from pathlib import Path
R = Path('/home/s223/m0nesy/Inference')
A = R / 'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit'
T = A / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
D, S = T / 'results/diagnostic', T / 'results'
O = R / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/results/static-recomputation.json'
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def q(v, f):
    if not v: return None
    v = sorted(v); p = (len(v)-1)*f; i = int(p)
    return v[i] + (v[min(i+1, len(v)-1)] - v[i]) * (p-i)
def st(v):
    return dict(count=len(v), mean=statistics.mean(v) if v else None,
                p50=q(v,.5), p95=q(v,.95), p99=q(v,.99),
                minimum=min(v, default=None), maximum=max(v, default=None))
def comp(p, n):
    a = [-1]*n
    for r in csv.DictReader(Path(p).open(encoding='utf-8', newline='')):
        a[int(r['flow_index'])] = int(r['ack_ns'])
    return a
def rows(p): return list(csv.DictReader(Path(p).open(encoding='utf-8', newline='')))

def main():
    f = rows(D / 'flows.csv'); n = len(f)
    live = [int(r['ack_ns']) for r in f]
    both = comp(D / 'replay-both/completions.csv', n)
    ep = comp(D / 'replay-collective/completions.csv', n)
    kv = comp(D / 'replay-kv/completions.csv', n)
    iso = dict(collective=ep, kv=kv)
    src = [D/'flows.csv', D/'stages.json', D/'replay-both/completions.csv',
           D/'replay-collective/completions.csv', D/'replay-kv/completions.csv',
           S/'flow-comparison.csv', S/'stage-comparison.csv', S/'request-impact.csv',
           S/'contention-summary.json', S/'request-impact-summary.json']
    cls = {}
    for kind in ('kv', 'collective'):
        ids = [i for i, r in enumerate(f) if r['kind'] == kind]
        d = [both[i] - iso[kind][i] for i in ids]
        nz = [(i, v) for i, v in zip(ids, d) if v != 0]
        cls[kind] = dict(flow_count=len(ids),
            logical_payload_bytes=sum(int(f[i]['payload_bytes']) for i in ids),
            isolated_replay='replay-kv' if kind=='kv' else 'replay-collective',
            added_latency_ns=st(d), positive_delta_count=sum(v>0 for v in d),
            negative_delta_count=sum(v<0 for v in d), zero_delta_count=sum(v==0 for v in d),
            delayed_over_1us_count=sum(v>1000 for v in d),
            improved_over_1us_count=sum(v<-1000 for v in d),
            nonzero_flow_details=[dict(flow_index=i, delta_ns=v, request_id=f[i]['request_id'],
                stage_index=int(f[i]['stage_index']), src=f[i]['source_endpoint'],
                dst=f[i]['destination_endpoint']) for i, v in nz])
    fc = rows(S / 'flow-comparison.csv')
    fdiff = max(abs(int(r['delta_ack_ns']) - (both[int(r['flow_index'])] - iso[r['kind']][int(r['flow_index'])])) for r in fc)
    fcross = dict(rows=len(fc), max_abs_delta_ns_difference=fdiff, all_delta_rows_match=fdiff==0,
        recorded_max_us={k: max(float(r['added_latency_us']) for r in fc if r['kind']==k) for k in ('kv','collective')},
        recorded_min_us={k: min(float(r['added_latency_us']) for r in fc if r['kind']==k) for k in ('kv','collective')})
    stages = json.loads((D / 'stages.json').read_text(encoding='utf-8'))
    full = [s['local_ack_ns'] for s in stages]
    nokv = [s['local_ack_ns'] for s in stages]
    for i, r in enumerate(f):
        si = int(r['stage_index'])
        if si >= 0:
            full[si] = max(full[si], both[i]); nokv[si] = max(nokv[si], ep[i])
    sd = [full[i] - nokv[i] for i in range(len(stages))]
    serr = max(abs(full[i] - s['ack_ns']) for i, s in enumerate(stages))
    sc = rows(S / 'stage-comparison.csv')
    fin = [r for r in sc if r['is_final_layer_combine'] == 'True']
    sdiff = max(abs(float(r['added_us']) - sd[int(r['stage_index'])]/1000) for r in sc)
    sinfo = dict(stage_count=len(stages), added_latency_ns=st(sd),
        nonzero_stage_count=sum(v!=0 for v in sd), max_added_ns=max(sd), min_added_ns=min(sd),
        delayed_over_1us_count=sum(v>1000 for v in sd),
        stage_reconstruction_vs_live_max_error_ns=serr,
        final_layer_combine_stage_rows=len(fin),
        final_layer_combine_max_added_us=max(float(r['added_us']) for r in fin),
        final_layer_combine_nonzero_count=sum(float(r['added_us'])!=0 for r in fin),
        stage_csv_rows=len(sc), stage_csv_max_abs_delta_us_difference=sdiff)
    ri = rows(S / 'request-impact.csv')
    gate = {}
    for r in fc:
        if r['kind'] != 'kv': continue
        rid = r['request_id']
        if rid not in gate or int(r['ack_both_ns']) > int(gate[rid]['ack_both_ns']): gate[rid] = r
    b = dict(requests=len(ri), delta_G_ns_all=[int(r['delta_G_ns']) for r in ri],
        delta_G_nonzero_count=sum(int(r['delta_G_ns'])!=0 for r in ri),
        T_KV_all_both_matches_live_count=sum(r['T_KV_all_both_matches_live']=='True' for r in ri),
        gate_determining_kv_flow_delta_ns={rid:int(r['delta_ack_ns']) for rid,r in gate.items()},
        mapped_decode_token_boundaries=sum(int(r['mapped_decode_token_boundaries']) for r in ri),
        max_boundary_exposure_us=max(float(r['max_boundary_exposure_us'] or 0) for r in ri),
        final_prefill_combine_added_us_all=[float(r['final_prefill_combine_added_us']) for r in ri],
        first_decode_token_combine_added_us_all=[float(r['first_decode_token_combine_added_us']) for r in ri],
        following_decode_combine_max_added_us=max(float(r['following_decode_combine_added_us_max'] or 0) for r in ri))
    b['all_delta_G_zero'] = b['delta_G_nonzero_count']==0
    b['all_gate_determining_kv_flows_unaffected'] = all(v==0 for v in b['gate_determining_kv_flow_delta_ns'].values())
    cs = json.loads((S / 'contention-summary.json').read_text(encoding='utf-8'))
    ris = json.loads((S / 'request-impact-summary.json').read_text(encoding='utf-8'))
    cross = dict(both_replay_max_ack_error_ns=max(abs(both[i]-live[i]) for i in range(n)),
        recorded_max_kv_added_us=cs['traffic_classes']['kv']['added_latency_us']['maximum'],
        recorded_max_ep_added_us=cs['traffic_classes']['collective']['added_latency_us']['maximum'],
        recorded_min_ep_added_us=cs['traffic_classes']['collective']['added_latency_us']['minimum'],
        recorded_max_stage_added_us=cs['ep_stage_groups']['decode.ep_combine']['added_latency_us']['maximum'],
        recorded_layer3_status=ris['layer3_quantified_end_to_end_slo_change']['status'])
    cross['kv_max_matches_144ns'] = cross['recorded_max_kv_added_us']*1000 == 144
    cross['ep_max_matches_136ns'] = cross['recorded_max_ep_added_us']*1000 == 136
    cross['ep_min_matches_minus_127ns'] = cross['recorded_min_ep_added_us']*1000 == -127
    cross['stage_max_matches_9ns'] = cross['recorded_max_stage_added_us']*1000 == 9
    cross['layer3_not_measured'] = cross['recorded_layer3_status'] == 'not_measured'
    checks = dict(flow_rows=n, stage_rows=len(stages), flow_cross_all_match=fcross['all_delta_rows_match'],
        stage_csv_max_abs_delta_us_difference=sdiff, stage_reconstruction_error_ns=serr,
        boundary_all_zero=(b['all_delta_G_zero'] and b['all_gate_determining_kv_flows_unaffected']
            and b['max_boundary_exposure_us']==0 and sinfo['final_layer_combine_nonzero_count']==0),
        cross_all_match=all(cross[k] for k in ('kv_max_matches_144ns','ep_max_matches_136ns',
            'ep_min_matches_minus_127ns','stage_max_matches_9ns','layer3_not_measured')))
    checks['all_passed'] = (checks['flow_cross_all_match'] and sdiff < 1e-9 and serr <= 1
        and checks['boundary_all_zero'] and checks['cross_all_match'])
    rep = dict(schema_version=1, task='T003-r02 static recomputation',
        evidence_class='WSL historical evidence; read-only server recomputation; no new simulation',
        sources={str(p.relative_to(A)): sha(p) for p in src},
        traffic_classes=cls, ep_stages=sinfo, request_necessary_boundaries=b,
        flow_comparison_cross_check=fcross, summary_cross_check=cross, checks=checks,
        one_microsecond_note='1 us is only a descriptive count threshold, not a statistical significance test; zero count above 1 us does not mean zero interference.',
        corrected_conclusion='Detected extremely weak network interference (KV->EP max 136 ns, EP->KV max 144 ns, one reverse delta -127 ns, one EP stage +9 ns); exposure at the specified frozen request-necessary boundaries is zero; end-to-end counterfactual performance change was not measured.',
        corrected_conclusion_zh='检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测。')
    O.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(all_passed=checks['all_passed'], kv_max_ns=cls['kv']['added_latency_ns']['maximum'],
        kv_positive=cls['kv']['positive_delta_count'], ep_max_ns=cls['collective']['added_latency_ns']['maximum'],
        ep_min_ns=cls['collective']['added_latency_ns']['minimum'], ep_positive=cls['collective']['positive_delta_count'],
        stage_max_ns=sinfo['max_added_ns'], stage_nonzero=sinfo['nonzero_stage_count'],
        delta_G_nonzero=b['delta_G_nonzero_count'], layer3=cross['recorded_layer3_status']), ensure_ascii=False))
    return 0 if checks['all_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
