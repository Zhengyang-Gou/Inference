import json
from pathlib import Path

m = json.loads(Path('/home/s223/m0nesy/Inference/coordination/history/2026-11-07/ARCHIVE.json').read_text(encoding='utf-8'))
t3 = [f for f in m['files'] if '/T003-kv-ep-contention-attribution/' in f['path']]
tracked = [f for f in t3 if f['tracked_snapshot']]
untracked = [f for f in t3 if not f['tracked_snapshot']]
print('T003 entries:', len(t3), 'tracked:', len(tracked), 'untracked:', len(untracked))
print('--- untracked (only in package) ---')
for f in untracked:
    print(f"{f['bytes']:>9}  {f['path']}")
need = ['results/diagnostic/flows.csv', 'results/diagnostic/stages.json', 'results/diagnostic/native_schedule.jsonl']
for n in need:
    hit = [f for f in t3 if f['path'].endswith(n)]
    print(n, '->', 'in package' if hit else 'MISSING', hit[0]['tracked_snapshot'] if hit else '')
root = Path('/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution')
print('--- live task dir presence ---')
for n in need + ['results/attempt1-failed', 'results/attempt1-failed/README.md']:
    print(n, (root / n).exists())
