"""T003 step 2/4: frozen source + backend identity check against accepted T002-r03."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
DIAG = TASK / "results/diagnostic"
R03 = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/results/audit-run"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    from joint_scheduling.backend.build import require_backend
    from joint_scheduling.experiments.provenance import freeze_source

    reference = json.loads((R03 / "reproducibility.json").read_text(encoding="utf-8"))
    source_dir = DIAG / "inputs" / "source"
    if source_dir.exists():
        source = source_dir.resolve()
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        created = False
    else:
        source = freeze_source(source_dir, (DIAG,))
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        created = True
    state = require_backend()
    backend = json.loads(state["manifest"].read_text(encoding="utf-8"))
    expected_backend = reference["backend"]

    checks = {
        "source_tree_sha256_r03": reference["source"]["source_tree_sha256"],
        "source_tree_sha256_now": manifest["source_tree_sha256"],
        "source_identical": manifest["source_tree_sha256"] == reference["source"]["source_tree_sha256"],
        "source_git_head_now": manifest.get("git_head"),
        "source_git_head_r03": reference["source"]["git_head"],
        "source_manifest_file_count": len(manifest["files"]),
        "source_snapshot_created_this_step": created,
        "source_snapshot_path": str(source),
        "source_archive_sha256": sha(source / "source.tar.gz"),
        "backend_fingerprint_r03": expected_backend["fingerprint"],
        "backend_fingerprint_now": backend["fingerprint"],
        "backend_fingerprint_identical": backend["fingerprint"] == expected_backend["fingerprint"],
        "backend_executable_sha256_r03": expected_backend["executable_sha256"],
        "backend_executable_sha256_now": sha(Path(backend["executable"])),
        "backend_executable_identical": sha(Path(backend["executable"])) == expected_backend["executable_sha256"],
        "backend_ns3_revision_identical": backend["revision"] == expected_backend["revision"],
        "backend_protocol_version_identical": backend["protocol_version"] == expected_backend["protocol_version"],
        "backend_ready": bool(state.get("executable") and Path(state["executable"]).is_file()),
    }
    # Compare every research-core source file byte hash against r03's manifest.
    r03_files = {f["path"]: f.get("sha256") for f in
                 json.loads((R03 / "source/manifest.json").read_text(encoding="utf-8"))["files"]}
    now_files = {f["path"]: f.get("sha256") for f in manifest["files"]}
    core = sorted(p for p in r03_files if p.startswith("joint_scheduling/") and p.endswith(".py"))
    changed = [p for p in core if r03_files[p] != now_files.get(p)]
    checks["research_core_python_files"] = len(core)
    checks["research_core_changed_files"] = changed
    checks["research_core_identical"] = not changed

    checks["all_passed"] = all([
        checks["source_identical"], checks["source_git_head_now"] == checks["source_git_head_r03"],
        checks["backend_fingerprint_identical"], checks["backend_executable_identical"],
        checks["backend_ns3_revision_identical"], checks["backend_protocol_version_identical"],
        checks["backend_ready"], checks["research_core_identical"],
    ])
    write(TASK / "results" / "environment-check.json", checks)
    print(json.dumps({k: checks[k] for k in
                      ("all_passed", "source_identical", "research_core_identical",
                       "backend_fingerprint_identical", "backend_executable_identical",
                       "backend_ready", "source_snapshot_created_this_step",
                       "research_core_python_files")}, ensure_ascii=False))
    return 0 if checks["all_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
