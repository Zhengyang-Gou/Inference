"""Build a verified, isolated copy of the vendored flowSim fluid backend.

Mirrors :mod:`joint_scheduling.backend.build` for the ns-3 backend: the vendored
source is hash-pinned, the reviewed overlays are applied only to an isolated
copy, and the resulting executable is fingerprinted together with compiler,
lock file and adapter sources.
"""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

from .flow_patch import apply_flowsim_overlay

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent
LOCK = json.loads((Path(__file__).parent / "flowsim.lock.json").read_text())
BUILD_ROOT = PACKAGE / "artifacts" / "backend-flowsim"
PATCH = Path(__file__).with_name("flow_patch.py")
DRIVER = Path(__file__).with_name("flowsim_native") / "flow_driver.cc"
SOURCES = ("Chunk.cpp", "Device.cpp", "Event.cpp", "EventList.cpp", "EventQueue.cpp", "Topology.cpp")


class FlowSimError(RuntimeError):
    pass


def _command_output(command, cwd=None):
    try:
        process = subprocess.run(command, cwd=str(cwd or REPO), capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise FlowSimError("command timed out: {}".format(command[0])) from error
    if process.returncode:
        raise FlowSimError("{}: {}".format(" ".join(str(item) for item in command), process.stderr.strip()))
    return process.stdout.strip()


def verify_vendored_source(source=None):
    """Verify every pinned flowSim file without depending on Git metadata."""
    source = Path(source) if source is not None else REPO / "third_party" / "flowsim"
    manifest_path = source / "SOURCE_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FlowSimError("vendored flowSim manifest is missing or invalid: {}".format(manifest_path)) from error
    if not isinstance(manifest, dict) or manifest.get("format") != "inference-vendored-source-v1":
        raise FlowSimError("unsupported vendored flowSim manifest format")
    if (manifest.get("upstream_revision") != LOCK["flowsim_revision"]
            or manifest.get("upstream_repository") != LOCK["flowsim_repository"]):
        raise FlowSimError("vendored flowSim provenance differs from flowsim.lock.json")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or "Topology.cpp" not in files:
        raise FlowSimError("vendored flowSim manifest must list Topology.cpp and source hashes")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != name or "\\" in name
                or not isinstance(expected, str) or len(expected) != 64
                or any(char not in "0123456789abcdef" for char in expected)):
            raise FlowSimError("invalid vendored flowSim manifest entry: {}".format(name))
        path = source / name
        if path.is_symlink():
            raise FlowSimError("vendored flowSim source must not contain symlinks: {}".format(name))
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise FlowSimError("vendored flowSim source is missing/unreadable: {}".format(name)) from error
        if actual != expected:
            raise FlowSimError(
                "vendored flowSim source hash mismatch: {}; review and update manifest/lock before rebuilding".format(name))
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != manifest.get("source_sha256") or digest != LOCK["source_sha256"]:
        raise FlowSimError("vendored flowSim manifest digest differs from flowsim.lock.json; review and update both")
    return {"source": source, "revision": manifest["upstream_revision"],
            "source_sha256": digest, "source_files": tuple(sorted(files))}


def source_state():
    state = verify_vendored_source()
    compiler = shutil.which("g++") or shutil.which("c++")
    if not compiler:
        raise FlowSimError("a C++ compiler is required (g++ or c++)")
    compiler_version = _command_output([compiler, "--version"]).splitlines()[0]
    digest = hashlib.sha256()
    digest.update(json.dumps({"lock": LOCK, "source_sha256": state["source_sha256"],
                              "compiler": compiler_version, "platform": platform.platform(),
                              "repo": str(REPO)}, sort_keys=True).encode())
    for path in (PATCH, DRIVER):
        digest.update(path.read_bytes())
    state.update(compiler=compiler, compiler_version=compiler_version,
                 fingerprint=digest.hexdigest()[:20])
    return state


def backend_paths():
    state = source_state()
    root = BUILD_ROOT / "current"
    state.update(root=root, source_copy=root / "source", build_dir=root / "build",
                 executable=root / "build" / "joint-scheduling-flowsim",
                 manifest=root / "build_manifest.json")
    return state


def require_flowsim():
    state = backend_paths()
    if not state["executable"].is_file() or not state["manifest"].is_file():
        raise FlowSimError("flowSim backend is not built for the current source/toolchain; run ./joint_scheduling/jsim backend build-flowsim")
    manifest = json.loads(state["manifest"].read_text())
    if manifest.get("fingerprint") != state["fingerprint"]:
        raise FlowSimError("flowSim build manifest is stale; rebuild")
    digest = hashlib.sha256(state["executable"].read_bytes()).hexdigest()
    if manifest.get("executable_sha256") != digest:
        raise FlowSimError("flowSim executable differs from its build manifest; rebuild")
    return state


def build_flowsim(jobs=2):
    if type(jobs) is not int or not 1 <= jobs <= 32:
        raise FlowSimError("jobs must be in [1, 32]")
    state = backend_paths()
    root, source = state["root"], state["source_copy"]
    if root.exists():
        root.rename(root.with_name("previous-" + uuid.uuid4().hex[:12]))
    source.mkdir(parents=True)
    for filename in state["source_files"]:
        destination = source / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(state["source"] / filename, destination)
    (source / ".source_sha256").write_text(state["source_sha256"] + "\n")
    (source / ".upstream_revision").write_text(state["revision"] + "\n")
    apply_flowsim_overlay(source)
    shutil.copyfile(DRIVER, source / "flow_driver.cc")
    build_dir = state["build_dir"]
    build_dir.mkdir(parents=True, exist_ok=True)
    command = [state["compiler"], "-O2", "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter",
               "-I", str(source), "-o", str(state["executable"])]
    command += [str(source / name) for name in SOURCES] + [str(source / "flow_driver.cc")]
    log_path = root / "build.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\nCOMMAND " + " ".join(command) + "\n")
        log.flush()
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if process.returncode:
        tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-35:])
        raise FlowSimError("flowSim backend build failed; see {}\n{}".format(log_path, tail))
    version = _command_output([str(state["executable"]), "--protocol-version"])
    if version != "JSB_PROTOCOL {}".format(LOCK["protocol_version"]):
        raise FlowSimError("flowSim backend protocol self-check failed: {}".format(version))
    manifest = {key: str(value) if isinstance(value, Path) else value for key, value in state.items()
                if key != "source_files"}
    manifest.update(protocol_version=LOCK["protocol_version"],
                    completion_semantics=LOCK["completion_semantics"])
    manifest["executable_sha256"] = hashlib.sha256(state["executable"].read_bytes()).hexdigest()
    (state["manifest"]).write_text(json.dumps(manifest, indent=2) + "\n")
    return state


def doctor():
    report = {"python": sys.version.split()[0],
              "vendored_source_present": (REPO / "third_party" / "flowsim" / "Topology.cpp").is_file(),
              "upstream_revision": LOCK["flowsim_revision"],
              "pinned_source_sha256": LOCK["source_sha256"], "backend_ready": False}
    try:
        state = require_flowsim()
        report.update(backend_ready=True, executable=str(state["executable"]),
                      fingerprint=state["fingerprint"], compiler=state["compiler_version"])
    except (FlowSimError, OSError, ValueError) as error:
        report["action_required"] = str(error)
    return report
