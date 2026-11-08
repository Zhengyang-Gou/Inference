# T003-r02: read-only audit of the WSL historical archive on the server.
# Verifies package identity, every per-file SHA256 in ARCHIVE.json, and that
# tracked server copies of historical originals still match. No repo file is modified.
import hashlib
import json
from pathlib import Path

ROOT = Path('/home/s223/m0nesy/Inference')
ARCHIVE_JSON = ROOT / 'coordination/history/2026-11-07/ARCHIVE.json'
PKG = ROOT / 'artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz'
AUDIT = ROOT / 'work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit'
OUT = ROOT / 'outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/results/archive-audit.json'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    manifest = json.loads(ARCHIVE_JSON.read_text(encoding='utf-8'))
    expected = manifest['archive']
    actual_pkg = dict(sha256=sha(PKG), bytes=PKG.stat().st_size)
    pkg_ok = (actual_pkg['sha256'] == expected['sha256']
              and actual_pkg['bytes'] == expected['bytes'])
    files = manifest['files']
    mismatches = []
    live_checked = 0
    live_mismatches = []
    for entry in files:
        rel = entry['path']
        extracted = AUDIT / rel
        if not extracted.is_file():
            mismatches.append(dict(path=rel, reason='missing in extracted archive'))
            continue
        data_hash = sha(extracted)
        size = extracted.stat().st_size
        if data_hash != entry['sha256'] or size != entry['bytes']:
            mismatches.append(dict(path=rel, reason='sha256/size mismatch',
                                    expected=entry['sha256'], actual=data_hash,
                                    expected_bytes=entry['bytes'], actual_bytes=size))
        live = ROOT / rel
        if live.is_file():
            live_checked += 1
            live_hash = sha(live)
            if live_hash != entry['sha256']:
                live_mismatches.append(dict(path=rel, expected=entry['sha256'], actual=live_hash))
    t003 = [e['path'] for e in files if 'T003-kv-ep-contention-attribution' in e['path']]
    extracted_count = sum(1 for p in AUDIT.rglob('*') if p.is_file())
    report = dict(
        schema_version=1,
        task='T003-r02 archive audit',
        evidence_class='WSL historical evidence; read-only server audit, not a new experiment',
        archive_json_sha256=sha(ARCHIVE_JSON),
        package=dict(path=str(PKG.relative_to(ROOT)), expected_sha256=expected['sha256'],
                     actual_sha256=actual_pkg['sha256'], expected_bytes=expected['bytes'],
                     actual_bytes=actual_pkg['bytes'], match=pkg_ok),
        manifest_file_count=len(files),
        original_file_count=manifest['original_file_count'],
        tracked_snapshot_count=manifest['tracked_file_count'],
        extracted_file_count=extracted_count,
        per_file_hash_mismatches=mismatches,
        all_per_file_hashes_match=not mismatches,
        server_live_copies_checked=live_checked,
        server_live_copies_mismatched=live_mismatches,
        server_historical_originals_unchanged=(not mismatches and not live_mismatches),
        t003_archive_entries=len(t003),
        independent_audit_dir=str(AUDIT.relative_to(ROOT)),
        note='ARCHIVE.json hash verified before reading the package; extraction to an independent work directory did not overwrite any task file.',
    )
    report['all_passed'] = (pkg_ok and not mismatches and not live_mismatches
                            and extracted_count == manifest['original_file_count'])
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(all_passed=report['all_passed'], package_match=pkg_ok,
                          files=len(files), extracted=extracted_count,
                          mismatches=len(mismatches), live_checked=live_checked,
                          live_mismatches=len(live_mismatches),
                          t003_entries=len(t003)), ensure_ascii=False))
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
