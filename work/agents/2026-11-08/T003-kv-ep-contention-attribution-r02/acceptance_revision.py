# T003-r02 revised acceptance logic (v2).
# Read-only re-evaluation of WSL historical evidence. No simulation.
# v2 changes vs v1:
#   - live start count is derived from console-log start markers, not hard-coded;
#   - the stop condition is judged by "was another stage started after the failed start";
#   - a budget gate (per-stage caps + 70 min aggregate) is evaluated separately from
#     the effective-stage wall clock;
#   - evidence-location findings are recorded because several cited raw paths exist
#     only inside the server history package (ARCHIVE.json tracked_snapshot=false);
#   - exit code 3 means "recheck completed and a process deviation was recorded",
#     distinct from 1 (tooling/input problem) and 0 (verdict computed, no deviation).
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

R = Path('/home/s223/m0nesy/Inference')
T3 = R / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
A = R / 'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit'
H = A / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
O = R / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/results'
ARCHIVE_JSON = R / 'coordination/history/2026-11-07/ARCHIVE.json'

STAGE_CAPS_MIN = {'live-baseline': 25.0, 'replay-both': 15.0, 'replay-collective': 15.0, 'replay-kv': 15.0}
TOTAL_CAP_MIN = 70.0
START_RE = re.compile(r'^(LIVE\d*_START)=(\S+)$')
EXIT_RE = re.compile(r'^(LIVE\d*_EXIT)=(-?\d+)$')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def jload(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def text(p):
    return Path(p).read_text(encoding='utf-8')


def dump(name, obj):
    (O / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def observed_starts():
    """Derive every live start from console logs; do not trust the report's count."""
    starts = []
    for log in sorted((T3 / 'logs').glob('*console*.log')):
        ts = code = None
        for line in text(log).splitlines():
            m = START_RE.match(line.strip())
            if m:
                ts = m.group(2)
            m = EXIT_RE.match(line.strip())
            if m:
                code = int(m.group(2))
        if ts:
            starts.append({'log': str(log.relative_to(T3)), 'started_at': ts, 'exit_code': code})
    starts.sort(key=lambda s: datetime.fromisoformat(s['started_at']))
    return starts


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
    replays = {k: jload(T3 / ('results/replay-stage-' + k + '.json')) for k in ('both', 'collective', 'kv')}
    review_present = (T3 / 'REVIEW.md').is_file()

    starts = observed_starts()
    first = dict(
        exit_code=1 if 'LIVE_EXIT=1' in c04 else None,
        simulation_time_ns=rpc['time_ns'],
        native_registrations=rpc['submission_commands'],
        hook_events=a1['hook_events'],
        cross_node_flows=a1['cross_node_flows'],
        wall_clock_seconds=a1['wall_clock_seconds'],
        recorded_lifecycle_events=len(ev1),
        ep_calls_started=ep1['started_calls'],
        status=a1['status'],
        error=a1['error'],
        started_at=a1['started_at'])
    second = dict(
        exit_code=0 if 'LIVE2_EXIT=0' in c07 else None,
        wall_clock_seconds=live2['wall_clock_seconds'],
        status=live2['status'],
        flows=live2['flows'],
        stages=live2['stages'],
        native_registrations=live2['native_registrations'])
    first['is_started_network_stage'] = (first['simulation_time_ns'] > 0
                                         and first['native_registrations'] > 0
                                         and first['cross_node_flows'] > 0)

    live_start_count = len(starts)
    restarts_after_failure = [s for s in starts
                              if first['started_at'] and s['started_at'] > first['started_at']]

    violations = []
    if first['is_started_network_stage'] and live_start_count > 1:
        violations.append(dict(
            id='V1',
            rule='TASK.md line 224: 启动次数上限=1次live+3次network replay',
            finding=('live was started %d times (first %s failed at sim %d ns with %d native '
                     'registrations; second %s succeeded)' % (
                         live_start_count, starts[0]['started_at'], first['simulation_time_ns'],
                         first['native_registrations'], starts[1]['started_at'])),
            expected_live_starts=1, actual_live_starts=live_start_count, passed=False,
            evidence=['logs/04-live-console.log', 'logs/07-live-console.log',
                      'results/attempt1-failed/live-stage.json',
                      'results/attempt1-failed/run/network/backend_rpc_summary.json',
                      'logs/commands.md']))
    if first['status'] == 'failed' and restarts_after_failure and all(
            replays[k]['status'] == 'completed' for k in replays):
        violations.append(dict(
            id='V2',
            rule='TASK.md line 224: 前置失败、live机制失败/未完成…保留现场并停止；不补跑',
            finding=('after the first live start failed the run was not stopped: another live start '
                     'and 3 network replays plus analysis followed'),
            stop_condition_triggered=True, stop_observed=False, passed=False,
            evidence=['logs/04-live-console.log', 'results/replay-stage-both.json',
                      'results/replay-stage-collective.json', 'results/replay-stage-kv.json',
                      'logs/12-analyze.log']))
    if restarts_after_failure and not review_present:
        violations.append(dict(
            id='V3',
            rule='TASK.md line 225: 已经启动的网络阶段不得借脚本修复静默重启；需要返工则先报告',
            finding=('the draft hook bug was reproduced/fixed (logs 05/06) after the failed start and '
                     'the already-started live stage was restarted, without a prior commander report '
                     'and without REVIEW.md'),
            prior_report_found=False, review_md_present=review_present, passed=False,
            evidence=['logs/05-static-hook-diag.log', 'logs/06-static-hook-diag-fixed.log',
                      'logs/04-live-console.log', 'logs/07-live-console.log',
                      'results/attempt1-failed/README.md']))
    if '静态草稿失败启动' in commands or '网络开跑前' in text(H / 'results/attempt1-failed/README.md'):
        violations.append(dict(
            id='V4',
            rule='TASK.md line 225: 普通静态脚本错误可在网络开跑前修正并记录',
            finding=('original records classified the first live start as a pre-network static error; '
                     'the 472621 ns simulation time, 12 native registrations and 1 cross-node flow show '
                     'the network stage had already started, so that exclusion does not apply'),
            classification_in_original='pre-network static script error',
            classification_corrected='started network stage that failed', passed=False,
            evidence=['results/attempt1-failed/README.md',
                      'results/attempt1-failed/run/network/backend_rpc_summary.json',
                      'logs/commands.md', 'REPORT.md']))

    wall = dict(live_failed_start=first['wall_clock_seconds'], live_valid=second['wall_clock_seconds'],
                both=replays['both']['wall_clock_seconds'],
                collective=replays['collective']['wall_clock_seconds'],
                kv=replays['kv']['wall_clock_seconds'])
    wall['effective_stage_total'] = sum(v for k, v in wall.items() if k != 'live_failed_start')
    wall['all_starts_total'] = wall['effective_stage_total'] + wall['live_failed_start']
    stage_minutes = {
        'live-baseline': wall['live_valid'] / 60,
        'replay-both': wall['both'] / 60,
        'replay-collective': wall['collective'] / 60,
        'replay-kv': wall['kv'] / 60,
    }
    budget = dict(
        per_stage_minutes=stage_minutes,
        per_stage_caps_min=STAGE_CAPS_MIN,
        per_stage_within_cap={k: stage_minutes[k] <= STAGE_CAPS_MIN[k] for k in stage_minutes},
        total_minutes=wall['all_starts_total'] / 60,
        total_cap_min=TOTAL_CAP_MIN,
        total_within_cap=wall['all_starts_total'] / 60 <= TOTAL_CAP_MIN,
        note=('wall clock alone cannot clear the gate: the failed start is counted in all_starts_total, '
              'and start count / stop conditions are judged independently of time.'))

    # Evidence-location findings: several cited raw paths are not in the Git snapshot.
    manifest = jload(ARCHIVE_JSON)
    tracked = {f['path']: f['tracked_snapshot'] for f in manifest['files']}
    cited = [
        'results/attempt1-failed/live-stage.json',
        'results/attempt1-failed/run/network/backend_rpc_summary.json',
        'results/diagnostic/flows.csv',
        'results/diagnostic/stages.json',
        'results/flow-comparison.csv',
        'results/stage-comparison.csv',
        'results/port-intervals.csv',
        'results/event-links.jsonl',
    ]
    rel_t3 = 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/'
    location = []
    for c in cited:
        in_live = (T3 / c).is_file()
        in_pkg = rel_t3 + c in tracked
        location.append(dict(
            cited_as=c,
            present_in_task_dir=in_live,
            present_in_history_package=in_pkg,
            tracked_in_git_snapshot=tracked.get(rel_t3 + c),
            resolves_without_unpacking=in_live))
    docs = dict(
        D1=dict(finding=('原 REPORT 称首轮现场完整保留于 results/attempt1-failed/，但该目录不在 Git 快照也不在'
                         '服务器任务目录内，只存在于完整历史包中；引用路径须先解包才可解析。'),
                impact='不影响证据存在性；影响审核可解析性与 REPORT 引用的准确性。',
                action='已在 r02 报告中给出包内绝对路径、ARCHIVE.json 条目与 SHA256。'),
        D2=dict(finding=('REPORT 引用的 results/diagnostic/flows.csv、stages.json、native_schedule.jsonl 同样仅存在于'
                         '完整历史包，ARCHIVE.json 记 tracked_snapshot=false。'),
                impact='逐流/逐阶段复算必须先解包；本轮已在独立目录解包并逐文件核对哈希。',
                action='复算脚本只读 archive-audit 解包副本，不写回任务目录。'),
        D3=dict(finding=('logs/commands.md 把首次 live 记为“静态草稿失败启动”并称“无补跑”，同时在同一节记录 472621 ns、'
                         '12 条 native 注册；表述与自身证据矛盾，且表格编号与时间顺序不一致（#7/#8 修复晚于 #9 启动）。'),
                impact='原验收据此把失败启动排除在判定外，导致 A6 误判 passed。',
                action='已在 V4 登记，并在修正验收中改为按启动次数与停止条件判定。'))
    gate = dict(
        status='failed' if violations else 'passed',
        live_start_cap=1,
        live_start_count=live_start_count,
        observed_starts=starts,
        failed_live_starts=sum(1 for s in starts if s['exit_code'] not in (0, None)),
        first_start_is_started_network_stage=first['is_started_network_stage'],
        stop_condition_triggered=first['status'] == 'failed',
        stop_observed=not restarts_after_failure,
        prior_report_found=False,
        review_md_present=review_present,
        violation_count=len(violations),
        violations=violations,
        budget=budget,
        note=('A6 must evaluate start count and stop conditions, not only effective-stage wall clock; '
              'the failed start also counts toward the aggregate budget.'))

    items = [
        dict(id='A1', status='passed',
             note=('Frozen identity, 8 request fields, parent binding projection and permitted config diffs '
                   'were static checks independent of the live run; archive audit confirms historical '
                   'originals are byte-identical.'),
             evidence=['results/identity-check.json', 'results/environment-check.json',
                       'inputs/sample-manifest.json', 'results/archive-audit.json']),
        dict(id='A2', status='passed_with_deviation',
             note=('Artifact criteria (8 requests / 550 tokens / 31 mechanism checks / EP audit) are met, but '
                   'all live evidence comes from the second live start, which violated the start cap and the '
                   'stop condition. Admissibility requires commander ruling.'),
             evidence=['results/mechanism-checks.json', 'results/live-stage.json',
                       'results/metrics-summary.json', 'results/diagnostic/run']),
        dict(id='A3', status='passed_with_deviation',
             note=('Both replay drained 201677/201677 with 0 ns ACK/stage error, but its calendar derives from '
                   'the same out-of-process live start.'),
             evidence=['results/replay-fidelity.json', 'results/diagnostic/replay-both']),
        dict(id='A4', status='passed_with_deviation',
             note=('Signed flow/stage deltas and shared-resource evidence are complete and reproducible from '
                   'the unpacked raw files; provenance is still WSL historical, not a server experiment.'),
             evidence=['results/contention-summary.json', 'results/flow-comparison.csv',
                       'results/stage-comparison.csv', 'results/static-recomputation.json']),
        dict(id='A5', status='passed_with_deviation',
             note=('8 KV gates and 550 token final-layer combine stages mapped exactly, with zero conditional '
                   'exposure; end-to-end counterfactual remains not measured.'),
             evidence=['results/event-links.jsonl', 'results/request-impact.csv',
                       'results/request-impact-summary.json']),
        dict(id='A6', status='failed' if violations else 'passed',
             note=('Budget and scope are NOT compliant: 2 live starts (cap 1), stop condition after the failed '
                   'live not observed, already-started network stage restarted after a script fix without a '
                   'prior commander report. Effective-stage wall clock alone is insufficient; time caps '
                   'themselves were not exceeded.'),
             evidence=['TASK.md', 'logs/commands.md', 'logs/04-live-console.log', 'logs/07-live-console.log',
                       'results/attempt1-failed/', 'results/acceptance-summary.json', 'REPORT.md'])]

    corrected = dict(
        schema_version=2, task_id='T003-r02', parent_task=str(T3),
        status='deviation_registered', all_passed=False,
        evidence_class='WSL historical evidence; server-side read-only static recheck; no new simulation',
        original_acceptance=dict(path=str(T3 / 'results/acceptance-summary.json'),
                                 all_passed=original['all_passed'],
                                 status='superseded_by_this_correction',
                                 file_preserved_unchanged=True),
        rules=rules, first_live_start=first, second_live_start=second,
        process_gate=gate, items=items, wall_clock_seconds=wall, budget=budget,
        evidence_location=location, documentation_findings=docs,
        ns3_simulation_time_limit_seconds=30.0,
        mechanism_checks_passed=mech['all_passed'], replay_fidelity_passed=fid['all_passed'],
        layer3_status=ris['layer3_quantified_end_to_end_slo_change']['status'],
        limitations=[
            '8 requests, all batches size 1; single seed 7; estimated profile and synthetic routing; artificial SLO.',
            'End-to-end counterfactual SLO/goodput change not measured.',
            ('This recheck did not run live, replay, or any long experiment; it cannot upgrade WSL evidence '
             'to a server experiment.')],
        commander_decisions=[
            'Whether the T003 evidence chain is admissible despite 2 live starts and the unobserved stop condition.',
            'Whether to require a fresh, budget-compliant rework (new task number or -r03 directory) on the server.',
            ('Whether to accept the narrowed research conclusion and how to record it in PROGRESS.md '
             '(commander-owned; execution agent did not edit it).'),
            'Whether the citation gap (raw evidence only inside the history package) needs a REPORT erratum.'])

    dump('acceptance-summary.corrected.json', corrected)
    dump('deviation-register.json', dict(
        schema_version=2, task_id='T003-r02', parent_task=str(T3),
        evidence_class='WSL historical evidence; server static recheck',
        first_live_start=first, second_live_start=second, process_gate=gate, budget=budget,
        failed_scene_preserved=dict(
            path=str(H / 'results/attempt1-failed'),
            package='artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz',
            note=('complete partial run directory, event links, port intervals, README and logs preserved '
                  'inside the history package; not modified by this recheck'),
            resolved_in_task_dir=False),
        evidence_location=location,
        original_records_with_misclassification=['results/attempt1-failed/README.md', 'logs/commands.md',
                                                 'REPORT.md'],
        no_script_or_evidence_was_altered=True))

    print(json.dumps(dict(all_passed=False, gate=gate['status'], violations=len(violations),
                          live_starts=live_start_count,
                          start_markers=[s['started_at'] for s in starts],
                          stop_observed=gate['stop_observed'],
                          first_sim_ns=first['simulation_time_ns'],
                          first_native=first['native_registrations'],
                          a6=items[5]['status'], rules_all_present=rules['all_rules_present'],
                          total_minutes=round(budget['total_minutes'], 3),
                          total_within_cap=budget['total_within_cap']), ensure_ascii=False))
    if not rules['all_rules_present']:
        return 1
    return 3 if violations else 0


if __name__ == '__main__':
    raise SystemExit(main())
