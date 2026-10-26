"""Source integrity stays reproducible without an upstream Git checkout."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from joint_scheduling.backend import build


class VendoredSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.source = self.repo / "third_party" / "ns3"
        (self.source / "simulation").mkdir(parents=True)
        (self.source / "simulation" / "CMakeLists.txt").write_text("project(fixture)\n")
        (self.source / "simulation" / "driver.cc").write_text("int main() { return 0; }\n")
        self.lock = dict(build.LOCK)
        self.write_manifest()
        self.addCleanup(patch.stopall)
        patch.object(build, "REPO", self.repo).start()
        patch.object(build, "LOCK", self.lock).start()
        # Even an accidental Git or other subprocess invocation fails the test.
        patch.object(build.subprocess, "run", side_effect=AssertionError("source verification invoked a command")).start()

    def write_manifest(self, files=None, update_lock=True):
        if files is None:
            files = {path.relative_to(self.source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((self.source / "simulation").rglob("*")) if path.is_file()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest = {"format": "inference-vendored-source-v1", "upstream_repository": self.lock["ns3_repository"],
                    "upstream_revision": self.lock["ns3_revision"], "source_sha256": digest, "files": files}
        (self.source / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest))
        if update_lock:
            self.lock["source_sha256"] = digest

    def test_verifies_without_git_and_ignores_own_repository_metadata(self):
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        state = build.verify_vendored_source()
        self.assertEqual(state["source_sha256"], self.lock["source_sha256"])
        self.assertEqual(state["source_files"], ("simulation/CMakeLists.txt", "simulation/driver.cc"))

    def test_detects_modified_and_missing_source_files(self):
        path = self.source / "simulation" / "driver.cc"
        path.write_text("changed\n")
        with self.assertRaisesRegex(build.BackendError, "hash mismatch: simulation/driver.cc"):
            build.verify_vendored_source()
        path.unlink()
        with self.assertRaisesRegex(build.BackendError, "missing/unreadable: simulation/driver.cc"):
            build.verify_vendored_source()

    def test_manifest_cannot_repin_a_changed_source_without_lock_update(self):
        (self.source / "simulation" / "driver.cc").write_text("reviewed change\n")
        self.write_manifest(update_lock=False)
        with self.assertRaisesRegex(build.BackendError, "manifest digest differs"):
            build.verify_vendored_source()

    def test_reviewed_source_changes_invalidate_build_fingerprint(self):
        with patch.object(build.shutil, "which", return_value="/fixture/g++"), \
                patch.object(build, "find_cmake", return_value="/fixture/cmake"), \
                patch.object(build.platform, "platform", return_value="fixture-platform"), \
                patch.object(build, "_command_output", side_effect=lambda command: (
                    "cmake version " + self.lock["cmake_version"] if command[0].endswith("cmake")
                    else "g++ fixture version")):
            before = build.source_state()
            (self.source / "simulation" / "driver.cc").write_text("reviewed change\n")
            self.write_manifest()
            after = build.source_state()
        self.assertNotEqual(before["source_sha256"], after["source_sha256"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_rejects_parent_paths_and_symlink_sources(self):
        files = {"simulation/CMakeLists.txt": hashlib.sha256((self.source / "simulation" / "CMakeLists.txt").read_bytes()).hexdigest(),
                 "../escape.cc": "0" * 64}
        self.write_manifest(files)
        with self.assertRaisesRegex(build.BackendError, "invalid .* manifest entry"):
            build.verify_vendored_source()
        path = self.source / "simulation" / "driver.cc"
        path.unlink()
        path.symlink_to("CMakeLists.txt")
        self.write_manifest()
        with self.assertRaisesRegex(build.BackendError, "must not contain symlinks"):
            build.verify_vendored_source()


if __name__ == "__main__":
    unittest.main()
