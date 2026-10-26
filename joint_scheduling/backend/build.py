"""Build a verified, isolated copy of the vendored ns-3 network backend."""

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent
LOCK = json.loads((Path(__file__).parent / "backend.lock.json").read_text())
BUILD_ROOT = PACKAGE / "artifacts" / "backend"


class BackendError(RuntimeError):
    pass


def _command_output(command, cwd=None):
    try:
        process = subprocess.run(command, cwd=str(cwd or REPO), capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise BackendError("command timed out: {}".format(command[0])) from error
    if process.returncode:
        raise BackendError("{}: {}".format(" ".join(str(item) for item in command), process.stderr.strip()))
    return process.stdout.strip()


def find_cmake():
    # Resolve the CMake wheel from the interpreter selected by simai.
    probe = subprocess.run([sys.executable, "-c", "import cmake; print(cmake.CMAKE_BIN_DIR + '/cmake')"],
                           capture_output=True, text=True, timeout=15)
    if probe.returncode == 0 and Path(probe.stdout.strip()).is_file():
        return probe.stdout.strip()
    candidate = Path(sys.executable).parent / "cmake"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("cmake")
    if not found:
        raise BackendError("CMake missing; run joint_scheduling/bootstrap.sh")
    return found


def verify_vendored_source(source=None):
    """Verify every pinned source file without depending on Git metadata/tools.

    Only files listed here are copied into the isolated build. The source hash
    pins the canonical path/hash mapping; the upstream revision is provenance.
    """
    source = Path(source) if source is not None else REPO / "third_party" / "ns3"
    manifest_path = source / "SOURCE_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackendError("vendored ns-3 source manifest is missing or invalid: {}".format(manifest_path)) from error
    if not isinstance(manifest, dict) or manifest.get("format") != "inference-vendored-source-v1":
        raise BackendError("unsupported vendored ns-3 source manifest format")
    if (manifest.get("upstream_revision") != LOCK["ns3_revision"]
            or manifest.get("upstream_repository") != LOCK["ns3_repository"]):
        raise BackendError("vendored ns-3 provenance differs from backend.lock.json")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or "simulation/CMakeLists.txt" not in files:
        raise BackendError("vendored ns-3 manifest must list simulation/CMakeLists.txt and source hashes")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != name or "\\" in name
                or not isinstance(expected, str) or len(expected) != 64
                or any(char not in "0123456789abcdef" for char in expected)):
            raise BackendError("invalid vendored ns-3 manifest entry: {}".format(name))
        path = source / name
        if any((source / Path(*relative.parts[:index])).is_symlink()
               for index in range(1, len(relative.parts) + 1)):
            raise BackendError("vendored ns-3 source must not contain symlinks: {}".format(name))
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise BackendError("vendored ns-3 source is missing/unreadable: {}".format(name)) from error
        if actual != expected:
            raise BackendError("vendored ns-3 source hash mismatch: {}; review and update manifest/lock before rebuilding".format(name))
    source_digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if source_digest != manifest.get("source_sha256") or source_digest != LOCK["source_sha256"]:
        raise BackendError("vendored ns-3 manifest digest differs from backend.lock.json; review and update both")
    return {"source": source, "revision": manifest["upstream_revision"],
            "source_sha256": source_digest, "source_files": tuple(sorted(files))}


def source_state():
    state = verify_vendored_source()
    compiler = shutil.which("g++") or shutil.which("c++")
    if not compiler:
        raise BackendError("a C++ compiler is required (g++ or c++)")
    cmake = find_cmake()
    cmake_version = _command_output([cmake, "--version"]).splitlines()[0]
    if cmake_version != "cmake version " + LOCK["cmake_version"]:
        raise BackendError("use CMake {} from bootstrap.sh for this pinned backend".format(LOCK["cmake_version"]))
    compiler_version = _command_output([compiler, "--version"]).splitlines()[0]
    digest = hashlib.sha256()
    digest.update(json.dumps({"lock": LOCK, "source_sha256": state["source_sha256"],
                              "compiler": compiler_version, "cmake": cmake_version,
                              "platform": platform.platform(), "repo": str(REPO)}, sort_keys=True).encode())
    for path in sorted(Path(__file__).parent.glob("native/*")) + [Path(__file__)]:
        digest.update(path.read_bytes())
    state.update(compiler=compiler, compiler_version=compiler_version, cmake=cmake,
                 cmake_version=cmake_version, fingerprint=digest.hexdigest()[:20])
    return state


def backend_paths():
    state = source_state()
    root = BUILD_ROOT / "current"
    state.update(root=root, source_copy=root / "source", build_dir=root / "cmake",
                 executable=root / "source" / "build" / "bin" / "joint-scheduling-ns3",
                 manifest=root / "build_manifest.json")
    return state


def require_backend():
    state = backend_paths()
    if not state["executable"].is_file() or not state["manifest"].is_file():
        raise BackendError("backend is not built for the current source/toolchain; run ./joint_scheduling/jsim backend build")
    manifest = json.loads(state["manifest"].read_text())
    if manifest.get("fingerprint") != state["fingerprint"]:
        raise BackendError("backend build manifest is stale; rebuild")
    digest = hashlib.sha256(state["executable"].read_bytes()).hexdigest()
    if manifest.get("executable_sha256") != digest:
        raise BackendError("backend executable differs from its build manifest; rebuild")
    return state


def build_backend(jobs=2):
    if type(jobs) is not int or not 1 <= jobs <= 32:
        raise BackendError("jobs must be in [1, 32]")
    state = backend_paths()
    root, source = state["root"], state["source_copy"]
    if state["manifest"].exists():
        previous = json.loads(state["manifest"].read_text())
        if (previous.get("source_copy") != str(source)
                or previous.get("compiler_version") != state["compiler_version"]
                or previous.get("source_sha256") != state["source_sha256"]):
            root.rename(root.with_name("previous-" + uuid.uuid4().hex[:12]))
    root.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.mkdir()
        for filename in state["source_files"]:
            relative = Path(filename)
            if relative.parts[0] != "simulation":
                continue
            destination = source / relative.relative_to("simulation")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state["source"] / relative, destination, follow_symlinks=False)
        (source / ".upstream_revision").write_text(state["revision"] + "\n")
        (source / ".source_sha256").write_text(state["source_sha256"] + "\n")
    marker = source / ".source_sha256"
    if not marker.is_file() or marker.read_text().strip() != state["source_sha256"]:
        raise BackendError("isolated source tree has another source digest; preserve/remove artifacts/backend/current and rebuild")
    # Overlay only the isolated build tree; never mutate the vendored sources.
    cmake_file = source / "CMakeLists.txt"
    cmake_file.write_text((state["source"] / "simulation" / "CMakeLists.txt").read_text().replace("VERSION 3.10..3.10", "VERSION 3.10...3.31"))
    # The upstream point-to-point sources include two application headers but
    # do not declare that dependency. Include those exact upstream definitions
    # in this isolated minimal build, without linking the full applications stack.
    p2p = source / "src" / "point-to-point"
    for stem in ("seq-ts-header", "simple-seq-ts-header"):
        for extension in ("h", "cc"):
            shutil.copyfile(state["source"] / "simulation" / "src" / "applications" / "model" / (stem + "." + extension),
                            p2p / "model" / (stem + "." + extension))
    p2p_cmake = (state["source"] / "simulation" / "src" / "point-to-point" / "CMakeLists.txt").read_text()
    p2p_cmake = p2p_cmake.replace("  SOURCE_FILES\n", "  SOURCE_FILES\n    model/seq-ts-header.cc\n    model/simple-seq-ts-header.cc\n")
    p2p_cmake = p2p_cmake.replace("  HEADER_FILES\n", "  HEADER_FILES\n    model/seq-ts-header.h\n    model/simple-seq-ts-header.h\n")
    (p2p / "CMakeLists.txt").write_text(p2p_cmake)
    shutil.copyfile(Path(__file__).parent / "native" / "driver.cc", source / "scratch" / "joint_scheduling_driver.cc")
    (source / "scratch" / "CMakeLists.txt").write_text(
        'add_executable(joint-scheduling-ns3 joint_scheduling_driver.cc)\n'
        'target_link_libraries(joint-scheduling-ns3 ${libcore} ${libnetwork} ${libinternet} ${libpoint-to-point})\n'
        'set_target_properties(joint-scheduling-ns3 PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_OUTPUT_DIRECTORY}/bin")\n')
    command = [state["cmake"], "-S", str(source), "-B", str(state["build_dir"]), "-G", "Unix Makefiles",
               "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_COMPILER=" + state["compiler"],
               "-DCMAKE_CXX_STANDARD=17", "-DNS3_ENABLED_MODULES=core;network;internet;point-to-point",
               "-DNS3_WARNINGS_AS_ERRORS=OFF", "-DNS3_ASSERT=ON", "-DNS3_TESTS=OFF", "-DNS3_EXAMPLES=OFF",
               "-DNS3_PYTHON_BINDINGS=OFF", "-DNS3_GSL=OFF", "-DNS3_GTK3=OFF", "-DNS3_MPI=OFF", "-DNS3_MTP=OFF",
               "-DNS3_VISUALIZER=OFF", "-DNS3_PRECOMPILE_HEADERS=OFF", "-DNS3_SQLITE=OFF"]
    log_path = root / "build.log"
    with log_path.open("a", encoding="utf-8") as log:
        for invocation in (command, [state["cmake"], "--build", str(state["build_dir"]),
                                    "--target", "joint-scheduling-ns3", "--parallel", str(jobs)]):
            log.write("\nCOMMAND " + " ".join(invocation) + "\n"); log.flush()
            process = subprocess.run(invocation, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-35:])
                raise BackendError("backend build failed; see {}\n{}".format(log_path, tail))
    if not state["executable"].is_file():
        raise BackendError("build returned successfully but executable is missing")
    manifest = {key: str(value) if isinstance(value, Path) else value for key, value in state.items()
                if key != "source_files"}
    manifest.update(protocol_version=LOCK["protocol_version"], completion_semantics=LOCK["completion_semantics"])
    manifest["executable_sha256"] = hashlib.sha256(state["executable"].read_bytes()).hexdigest()
    state["manifest"].write_text(json.dumps(manifest, indent=2) + "\n")
    return state


def doctor():
    report = {"python": sys.version.split()[0], "python_executable": sys.executable,
              "vendored_source_present": (REPO / "third_party" / "ns3" / "simulation" / "CMakeLists.txt").is_file(),
              "upstream_ns3_revision": LOCK["ns3_revision"],
              "pinned_source_sha256": LOCK["source_sha256"], "backend_ready": False}
    try:
        state = require_backend()
        if _command_output([str(state["executable"]), "--protocol-version"]) != "JSB_PROTOCOL {}".format(LOCK["protocol_version"]):
            raise BackendError("backend ABI/protocol self-check failed")
        report.update(backend_ready=True, executable=str(state["executable"]),
                      fingerprint=state["fingerprint"], compiler=state["compiler_version"])
    except (BackendError, OSError, ValueError) as error:
        report["action_required"] = str(error)
    return report
