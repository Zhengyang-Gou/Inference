# T003-r02 revised acceptance logic. Read-only re-evaluation of WSL historical evidence.
# Adds a process gate (live start count, stop conditions, prior report) to A6.
# No simulation, no modification of historical originals, no rewriting of original TASK/REPORT.
import hashlib, json
from pathlib import Path
R = Path('/home/s223/m0nesy/Inference')
T3 = R / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
A = R / 'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit'
H = A / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
O = R / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/results'
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def jload(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def text(p): return Path(p).read_text(encoding='utf-8')
def dump(name, obj):
    (O / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def main():
    task = text(T3 / 'TASK.md')
    rules = dict(
        live_cap_rule='启动次数上限=1次live+3次network replay' in task,
        stop_rule='保留现场并停止；不补跑' in task,
        restart_rule='已经启动的网络阶段不得借' in task,
        prior_report_rule='需要返工则先报告' in task,
        static_error_rule='普通静态脚本错误可在网络开跑前修正并记录' in task,
        a6_rule='预算与范围符合' in task)
    rules['all_rules_present'] = all(rules.values())
    a1 = jload(H / 'results/attempt1-failed/live-stage.json')
    rpc = jload(H / 'results/attempt1-failed/run/network/backend_rpc_summary.json')
    c04 = text(H / 'logs/04-live-console.log')
    c07 = text(H / 'logs/07-live-console.log')
    ev1 = [x for x in text(H / 'results/attempt1-failed/run/events.jsonl').splitlines() if x.strip()]
    ep1 = jload(H / 'results/attempt1-failed/run/expert_parallel_summary.json')
    live2 = jload(T3 / 'results/live-stage.json')
    commands = text(T3 / 'logs/commands.md')
    original = jload(T3 / 'results/acceptance-summary.json')
    mech = jload(T3 / 'results/mechanism-checks.json')
    fid = jload(T3 / 'results/replay-fidelity.json')
    ris = jload(T3 / 'results/request-impact-summary.json')
    replays = {k: jload(T3 / ('results/replay-stage-' + k + '.json')) for k in ('both','collective','kv')}
    review_present = (T3 / 'REVIEW.md').is_file()
    first = dict(exit_code=1 if 'LIVE_EXIT=1' in c04 else None,
        simulation_time_ns=rpc['time_ns'], native_registrations=rpc['submission_commands'],
        hook_events=a1['hook_events'], cross_node_flows=a1['cross_node_flows'],
        wall_clock_seconds=a1['wall_clock_seconds'], recorded_lifecycle_events=len(ev1),
        ep_calls_started=ep1['started_calls'], status=a1['status'], error=a1['error'],
        started_at=a1['started_at'])
    second = dict(exit_code=0 if 'LIVE2_EXIT=0' in c07 else None,
        wall_clock_seconds=live2['wall_clock_seconds'], status=live2['status'],
        flows=live2['flows'], stages=live2['stages'], native_registrations=live2['native_registrations'])
    first['is_started_network_stage'] = (first['simulation_time_ns'] > 0
        and first['native_registrations'] > 0 and first['cross_node_flows'] > 0)
    violations = []
    if first['is_started_network_stage']:
        violations.append(dict(id='V1', rule='TASK.md line 224: 启动次数上限=1次live+3次network replay',
            finding='live was started twice (19:35:40 failed at sim 472621 ns with 12 native registrations; 19:42:41 second start succeeded)',
            expected_live_starts=1, actual_live_starts=2, passed=False,
            evidence=['logs/04-live-console.log','logs/07-live-console.log','results/attempt1-failed/live-stage.json','results/attempt1-failed/run/network/backend_rpc_summary.json','logs/commands.md']))
    if first['status'] == 'failed' and all(replays[k]['status'] == 'completed' for k in replays):
        violations.append(dict(id='V2', rule='TASK.md line 224: 前置失败、live机制失败/未完成…保留现场并停止；不补跑',
            finding='after the first live start failed, the run was not stopped: a second live start and 3 network replays plus analysis were launched',
            stop_condition_triggered=True, stop_observed=False, passed=False,
            evidence=['logs/04-live-console.log','results/replay-stage-both.json','results/replay-stage-collective.json','results/replay-stage-kv.json','logs/12-analyze.log']))
    if not review_present and first['is_started_network_stage']:
        violations.append(dict(id='V3', rule='TASK.md line 225: 已经启动的网络阶段不得借脚本修复静默重启；需要返工则先报告',
            finding='the draft hook bug was fixed (logs 05/06) and the already-started live stage was restarted ~7 minutes later without any commander report or REVIEW.md',
            prior_report_found=False, review_md_present=False, passed=False,
            evidence=['logs/05-static-hook-diag.log','logs/06-static-hook-diag-fixed.log','logs/04-live-console.log','logs/07-live-console.log','results/attempt1-failed/README.md']))
    if '静态草稿失败启动' in commands or '网络开跑前' in text(H / 'results/attempt1-failed/README.md'):
        violations.append(dict(id='V4', rule='TASK.md line 225: 普通静态脚本错误可在网络开跑前修正并记录',
            finding='original records classified the first live start as a pre-network static error; the 472621 ns simulation time and 12 native registrations show the network stage had already started, so that exclusion does not apply',
            classification_in_original='pre-network static script error',
            classification_corrected='started network stage that failed', passed=False,
            evidence=['results/attempt1-failed/README.md','results/attempt1-failed/run/network/backend_rpc_summary.json','logs/commands.md','REPORT.md']))
    wall = dict(live_failed_start=first['wall_clock_seconds'], live_valid=second['wall_clock_seconds'],
        both=replays['both']['wall_clock_seconds'], collective=replays['collective']['wall_clock_seconds'],
        kv=replays['kv']['wall_clock_seconds'])
    wall['effective_stage_total'] = sum(v for k, v in wall.items() if k != 'live_failed_start')
    wall['all_starts_total'] = wall['effective_stage_total'] + wall['live_failed_start']
    gate = dict(status='failed' if violations else 'passed',
        live_start_cap=1, live_start_count=2, failed_live_starts=1,
        first_start_is_started_network_stage=first['is_started_network_stage'],
        stop_condition_triggered=first['status'] == 'failed',
        stop_observed=False, prior_report_found=False, review_md_present=review_present,
        violation_count=len(violations), violations=violations,
        note='A6 must evaluate start count and stop conditions, not only effective-stage wall clock.')
    items = [
      dict(id='A1', status='passed', note='Frozen identity, 8 request fields, parent binding projection and permitted config diffs were static checks independent of the live run; archive audit confirms historical originals are byte-identical.', evidence=['results/identity-check.json','results/environment-check.json','inputs/sample-manifest.json','results/archive-audit.json']),
      dict(id='A2', status='passed_with_deviation', note='Artifact criteria (8 requests / 550 tokens / mechanism checks / EP audit) are met, but all live evidence comes from the second live start, which violated the start cap and the stop condition. Admissibility requires commander ruling.', evidence=['results/mechanism-checks.json','results/live-stage.json','results/metrics-summary.json','results/diagnostic/run']),
      dict(id='A3', status='passed_with_deviation', note='Both replay drained with 0 ns ACK/stage error, but its calendar derives from the same out-of-process live start.', evidence=['results/replay-fidelity.json','results/diagnostic/replay-both']),
      dict(id='A4', status='passed_with_deviation', note='Signed flow/stage deltas and shared-resource evidence are complete and reproducible; evidence provenance is still WSL historical, not a server experiment.', evidence=['results/contention-summary.json','results/flow-comparison.csv','results/stage-comparison.csv','results/static-recomputation.json']),
      dict(id='A5', status='passed_with_deviation', note='8 KV gates and 550 token final-layer combine stages mapped exactly, with zero conditional exposure; end-to-end counterfactual remains not measured.', evidence=['results/event-links.jsonl','results/request-impact.csv','results/request-impact-summary.json']),
      dict(id='A6', status='failed' if violations else 'passed', note='Budget and scope are NOT compliant: 2 live starts (cap 1), stop condition after failed live not observed, already-started network stage restarted after a script fix without prior commander report. Effective-stage wall clock alone was insufficient.', evidence=['TASK.md','logs/commands.md','logs/04-live-console.log','logs/07-live-console.log','results/attempt1-failed/','results/acceptance-summary.json','REPORT.md'])]
    corrected = dict(schema_version=1, task_id='T003-r02', parent_task=str(T3),
        status='deviation_registered', all_passed=False,
        evidence_class='WSL historical evidence; server-side read-only static recheck; no new simulation',
        original_acceptance=dict(path=str(T3 / 'results/acceptance-summary.json'),
            all_passed=original['all_passed'], status='superseded_by_this_correction',
            file_preserved_unchanged=True),
        rules=rules, first_live_start=first, second_live_start=second,
        process_gate=gate, items=items, wall_clock_seconds=wall,
        ns3_simulation_time_limit_seconds=30.0,
        mechanism_checks_passed=mech['all_passed'], replay_fidelity_passed=fid['all_passed'],
        layer3_status=ris['layer3_quantified_end_to_end_slo_change']['status'],
        limitations=['8 requests, all batches size 1; single seed 7; estimated profile and synthetic routing; artificial SLO.',
                     'End-to-end counterfactual SLO/goodput change not measured.',
                     'This recheck did not run live, replay, or any long experiment; it cannot upgrade WSL evidence to a server experiment.'],
        commander_decisions=['Whether the T003 evidence chain is admissible despite 2 live starts and the unobserved stop condition.',
                             'Whether to require a fresh, budget-compliant rework (new task number or -r03 directory) on the server.',
                             'Whether to accept the narrowed research conclusion and how to record it in PROGRESS.md (commander-owned; execution agent did not edit it).'])
    dump('acceptance-summary.corrected.json', corrected)
    dump('deviation-register.json', dict(schema_version=1, task_id='T003-r02', parent_task=str(T3),
        evidence_class='WSL historical evidence; server static recheck',
        first_live_start=first, second_live_start=second, process_gate=gate,
        failed_scene_preserved=dict(path=str(H / 'results/attempt1-failed'),
            note='complete partial run directory, event-links, port-intervals, README and logs preserved; not modified by this recheck'),
        original_records_with_misclassification=['results/attempt1-failed/README.md','logs/commands.md','REPORT.md'],
        no_script_or_evidence_was_altered=True))
    print(json.dumps(dict(all_passed=False, gate=gate['status'], violations=len(violations),
        live_starts=gate['live_start_count'], first_sim_ns=first['simulation_time_ns'],
        first_native=first['native_registrations'], a6=items[5]['status'],
        rules_all_present=rules['all_rules_present']), ensure_ascii=False))
    return 0 if rules['all_rules_present'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
