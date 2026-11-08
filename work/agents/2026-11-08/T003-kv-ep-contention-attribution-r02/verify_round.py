# T003-r02: final consistency check of this round's deliverables. Read-only.
import hashlib
import json
import subprocess
from pathlib import Path

R = Path('/home/s223/m0nesy/Inference')
OUT = R / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02'
T3 = R / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
RES = OUT / 'results'


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def jload(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


problems = []
status = jload(OUT / 'STATUS.json')
acc = jload(RES / 'acceptance-summary.corrected.json')
dev = jload(RES / 'deviation-register.json')
rec = jload(RES / 'static-recomputation.json')
aud = jload(RES / 'archive-audit.json')
rev = jload(RES / 'server-static-review-2026-11-08.json')
before = jload(RES / 't003-original-tree-before.json')
after = jload(RES / 't003-original-tree-after.json')

if status['status'] != 'submitted':
    problems.append('STATUS.status != submitted')
if status.get('parent_task', '').endswith('T003-kv-ep-contention-attribution') is False:
    problems.append('STATUS.parent_task does not point at the original T003 directory')
if acc['all_passed'] is not False:
    problems.append('corrected acceptance should be all_passed=false')
if acc['items'][5]['status'] != 'failed':
    problems.append('A6 should be failed')
if acc['process_gate']['live_start_count'] != 2 or len(acc['process_gate']['violations']) != 4:
    problems.append('process gate counts unexpected')
if rec['traffic_classes']['kv']['added_latency_ns']['maximum'] != 144:
    problems.append('EP->KV max should be 144 ns')
if rec['traffic_classes']['collective']['added_latency_ns']['maximum'] != 136:
    problems.append('KV->EP max should be 136 ns')
if rec['traffic_classes']['collective']['added_latency_ns']['minimum'] != -127:
    problems.append('KV->EP min should be -127 ns')
if rec['ep_stages']['max_added_ns'] != 9:
    problems.append('stage max should be 9 ns')
if rec['summary_cross_check']['recorded_layer3_status'] != 'not_measured':
    problems.append('layer3 should be not_measured')
if not aud['all_passed'] or aud['per_file_hash_mismatches']:
    problems.append('archive audit not clean')
if not rev['all_passed']:
    problems.append('static review record not all_passed')
if before['tree_sha256'] != after['tree_sha256']:
    problems.append('original T003 tree changed')
if '未检出可测量的 KV' in (OUT / 'REPORT.md').read_text(encoding='utf-8'):
    problems.append('overreaching wording still present in REPORT')
retraction_markers = ('删除', '撤回', '不支持', '不得', '过度推断')
for phrase in ('互扰不存在', '任何算法差异都不可能来自竞争', '关闭整个研究主线'):
    for line in (OUT / 'REPORT.md').read_text(encoding='utf-8').splitlines():
        if phrase in line and not any(m in line for m in retraction_markers):
            problems.append('overreaching phrase stated as a claim: ' + phrase)

gs = subprocess.run(['git', '-C', str(R), 'status', '--short', '--untracked-files=all'],
                    capture_output=True, text=True).stdout.strip()

files = []
for p in sorted(OUT.rglob('*')):
    if p.is_file():
        files.append(dict(path=str(p.relative_to(OUT)), bytes=p.stat().st_size, sha256=sha(p)))

print(json.dumps(dict(
    checked_at='see logs/r02-07-verify.log',
    problems=problems,
    all_ok=not problems,
    git_status_lines=len(gs.splitlines()) if gs else 0,
    original_task_tree_unchanged=before['tree_sha256'] == after['tree_sha256'],
    original_task_files=before['file_count'],
    deliverable_files=len(files),
    deliverable_bytes=sum(f['bytes'] for f in files),
    status=status['status'],
    a6=acc['items'][5]['status'],
    gate=acc['process_gate']['status'],
    live_starts=acc['process_gate']['live_start_count'],
    conclusion=acc['process_gate']['note'],
), ensure_ascii=False, indent=2))
print('--- files ---')
for f in files:
    print(f"{f['bytes']:>9}  {f['sha256'][:12]}  {f['path']}")
raise SystemExit(0 if not problems else 1)
