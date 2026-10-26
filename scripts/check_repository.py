#!/usr/bin/env python3
"""Check that an Inference source checkout is complete without upstream Git."""

import json
import os
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]


def require_path(relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise ValueError("Invalid repository-relative path: {}".format(relative))
    local = ROOT / relative
    if not local.exists():
        raise ValueError("Required project path is missing: {}".format(relative))
    return local


def main():
    for name in ("inference", "environment.yml", "scripts/simai-python", "README.md",
                 "joint_scheduling/backend/backend.lock.json", "third_party/manifest.json"):
        require_path(name)
    manifest = json.loads((ROOT / "third_party/manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "inference-third-party-v1" or not manifest.get("components"):
        raise ValueError("Invalid third-party component manifest")
    for component in manifest["components"]:
        require_path(component["path"])
        require_path(component["license_file"])
        for name in component.get("active_modules", []) + component.get("future_modules", []):
            require_path(component["path"] + "/simulation/src/" + name + "/CMakeLists.txt")
        revision = component["upstream_revision"]
        if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
            raise ValueError("Invalid upstream revision: {}".format(component["name"]))

    for directory, subdirs, files in os.walk(ROOT):
        current = Path(directory)
        if current == ROOT:
            subdirs[:] = [name for name in subdirs if name != ".git"]
        for name in subdirs + files:
            if current == ROOT and name == ".git":
                continue
            if name in {".git", ".gitmodules"}:
                raise ValueError("Nested upstream Git metadata: {}".format((current / name).relative_to(ROOT)))
        subdirs[:] = [name for name in subdirs if name not in {"artifacts", "__pycache__", "work"}]

    sys.path.insert(0, str(ROOT))
    from joint_scheduling.backend.build import verify_vendored_source
    state = verify_vendored_source()
    print("Repository layout and {} third-party components verified; {} network source files checked.".format(
        len(manifest["components"]), len(state["source_files"])))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print("Repository check failed: {}".format(error), file=sys.stderr)
        sys.exit(1)
