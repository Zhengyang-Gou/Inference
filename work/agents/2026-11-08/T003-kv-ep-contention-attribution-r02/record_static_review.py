# T003-r02: assemble the server-side static review record for this round.
# Reads only logs/results produced by run_static_recheck.sh; writes one JSON summary.
import hashlib
import json
import re
from pathlib import Path

R = Path('/home/s223/m0nesy/Inference')
OUT = R / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02'
LOG = OUT / 'logs'
RES = OUT / 'results'
PKG = R / 'artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz'


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def jload(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def main():
    env = {}
    for line in (LOG / 'r02-00-environment.log').read_text(encoding='utf-8').splitlines():
        m = re.match(r'^([a-z_]+)=(.*)$', line)
        if m:
            env[m.group(1)] = m.group(2)
    codes = {}
    for line in (LOG / 'r02-05-exit-codes.txt').read_text(encoding='utf-8').splitlines():
        if line.strip():
            name, rc = line.split()
            codes[name] = int(rc)

    before = jload(RES / 't003-original-tree-before.json')
    after = jload(RES / 't003-original-tree-after.json')
    audit = jload(RES / 'archive-audit.json')
    rec = jload(RES / 'static-recomputation.json')
    acc = jload(RES / 'acceptance-summary.corrected.json')

    scripts = {}
    for name in ('archive_audit.py', 'interference_recompute.py', 'acceptance_revision.py',
                 't3_tree_hash.py', 'record_static_review.py'):
        p = R / 'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02' / name
        scripts[name] = dict(sha256=sha(p), bytes=p.stat().st_size)

    unchanged = (before['file_count'] == after['file_count']
                 and before['tree_sha256'] == after['tree_sha256'])

    report = dict(
        schema_version=1,
        task_id='T003-r02',
        parent_task=str(R / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'),
        round='2026-11-08 server static recheck',
        evidence_class=('WSL historical evidence re-audited and statically recomputed on the server; '
                        'no live run, no network replay, no new experiment'),
        environment=dict(
            host=env.get('host'), repo=env.get('repo'), branch=env.get('branch'),
            head=env.get('head'), python=env.get('python'), conda_exe=env.get('conda_exe'),
            utc_start=env.get('utc_start'), utc_end=env.get('utc_end'),
            git_status_lines_before=int(env.get('git_status_lines') or -1)),
        commands=[
            dict(step=1, log='logs/r02-01-archive-audit.log',
                 command='export CONDA_EXE=/home/s223/miniconda3/bin/conda; '
                         './scripts/simai-python work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive_audit.py',
                 exit_code=codes.get('r02-01-archive-audit')),
            dict(step=2, log='logs/r02-02-static-recompute.log',
                 command='export CONDA_EXE=/home/s223/miniconda3/bin/conda; '
                         './scripts/simai-python work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/interference_recompute.py',
                 exit_code=codes.get('r02-02-static-recompute')),
            dict(step=3, log='logs/r02-03-acceptance-revision.log',
                 command='export CONDA_EXE=/home/s223/miniconda3/bin/conda; '
                         './scripts/simai-python work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/acceptance_revision.py',
                 exit_code=codes.get('r02-03-acceptance-revision'),
                 exit_code_meaning='3 = recheck completed and a process deviation was recorded (not a crash)'),
            dict(step=4, log='logs/r02-04-tree-hash-after.log',
                 command='./scripts/simai-python work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/t3_tree_hash.py '
                         'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/results/t003-original-tree-after.json',
                 exit_code=codes.get('r02-04-tree-hash-after')),
        ],
        scripts=scripts,
        archive_audit=dict(
            package=str(PKG.relative_to(R)),
            package_sha256=sha(PKG),
            package_bytes=PKG.stat().st_size,
            package_matches_manifest=audit['package']['match'],
            manifest_files=audit['manifest_file_count'],
            per_file_hash_mismatches=len(audit['per_file_hash_mismatches']),
            tracked_snapshot_copies_checked=audit['server_live_copies_checked'],
            tracked_snapshot_mismatches=len(audit['server_live_copies_mismatched']),
            audit_passed=audit['all_passed']),
        static_recomputation=dict(
            passed=rec['checks']['all_passed'],
            flow_rows=rec['checks']['flow_rows'],
            stage_rows=rec['checks']['stage_rows'],
            kv_max_added_ns=rec['traffic_classes']['kv']['added_latency_ns']['maximum'],
            ep_max_added_ns=rec['traffic_classes']['collective']['added_latency_ns']['maximum'],
            ep_min_added_ns=rec['traffic_classes']['collective']['added_latency_ns']['minimum'],
            stage_max_added_ns=rec['ep_stages']['max_added_ns'],
            stage_nonzero_count=rec['ep_stages']['nonzero_stage_count'],
            delta_G_nonzero_count=rec['request_necessary_boundaries']['delta_G_nonzero_count'],
            final_layer_combine_nonzero_count=rec['ep_stages']['final_layer_combine_nonzero_count'],
            layer3_status=rec['summary_cross_check']['recorded_layer3_status']),
        acceptance=dict(
            all_passed=acc['all_passed'],
            process_gate_status=acc['process_gate']['status'],
            live_start_count=acc['process_gate']['live_start_count'],
            live_start_cap=acc['process_gate']['live_start_cap'],
            violations=[v['id'] for v in acc['process_gate']['violations']],
            a6=acc['items'][5]['status'],
            total_minutes=round(acc['budget']['total_minutes'], 3),
            total_within_cap=acc['budget']['total_within_cap']),
        original_task_tree=dict(
            path=before['tree'], file_count_before=before['file_count'],
            file_count_after=after['file_count'],
            tree_sha256_before=before['tree_sha256'], tree_sha256_after=after['tree_sha256'],
            unchanged=unchanged),
        simulation_started=False,
        simulation_note=('only read-only scripts were executed: hash audit, CSV/JSON recomputation, '
                         'acceptance re-evaluation and tree hashing; no ns-3 backend, no live baseline, '
                         'no replay, no new run directory'),
        git=dict(committed=False, pushed=False,
                 note='no commit/push in this round; changes stay in the working tree'),
    )
    report['all_passed'] = (bool(audit['all_passed']) and bool(rec['checks']['all_passed'])
                            and unchanged and codes.get('r02-01-archive-audit') == 0
                            and codes.get('r02-02-static-recompute') == 0
                            and codes.get('r02-03-acceptance-revision') == 3)
    (RES / 'server-static-review-2026-11-08.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(all_passed=report['all_passed'], tree_unchanged=unchanged,
                          audit=audit['all_passed'], recompute=rec['checks']['all_passed'],
                          gate=acc['process_gate']['status'], codes=codes),
                     ensure_ascii=False))
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
