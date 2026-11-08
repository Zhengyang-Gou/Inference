import json
from pathlib import Path

p = Path('/home/s223/m0nesy/Inference/coordination/history/2026-11-07/ARCHIVE.json')
m = json.loads(p.read_text(encoding='utf-8'))
print('top-level keys:', list(m.keys()))
print('archive:', json.dumps(m['archive'], ensure_ascii=False))
print('files:', len(m['files']))
print('first entry:', json.dumps(m['files'][0], ensure_ascii=False))
a = [f for f in m['files'] if 'attempt1-failed' in f['path']]
print('attempt1 entries:', len(a))
for f in a:
    print(json.dumps(f, ensure_ascii=False))
print('original_file_count:', m.get('original_file_count'), 'tracked_file_count:', m.get('tracked_file_count'))
t3 = [f for f in m['files'] if '/T003-' in f['path']]
print('t003 entries:', len(t3), 'tracked:', sum(1 for f in t3 if f.get('tracked')))
print('t003 sample:', json.dumps(t3[:2], ensure_ascii=False))
