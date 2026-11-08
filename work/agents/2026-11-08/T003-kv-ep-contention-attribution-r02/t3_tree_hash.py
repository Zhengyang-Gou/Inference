import hashlib
import json
import sys
from pathlib import Path

root = Path('/home/s223/m0nesy/Inference')
t3 = root / 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution'
out = Path(sys.argv[1])
rows = []
for p in sorted(t3.rglob('*')):
    if p.is_file():
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append({'path': str(p.relative_to(root)), 'bytes': p.stat().st_size, 'sha256': h})
agg = hashlib.sha256()
for r in rows:
    agg.update((r['path'] + ':' + r['sha256'] + '\n').encode())
doc = {
    'tree': 'outputs/agents/2026-11-07/T003-kv-ep-contention-attribution',
    'file_count': len(rows),
    'tree_sha256': agg.hexdigest(),
    'files': rows,
}
out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'file_count': len(rows), 'tree_sha256': agg.hexdigest()}, ensure_ascii=False))
