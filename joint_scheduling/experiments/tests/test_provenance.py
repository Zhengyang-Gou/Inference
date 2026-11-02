"""A dirty source snapshot is reproducible independently of its Git HEAD."""
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from joint_scheduling.experiments.provenance import freeze_source


class ProvenanceTests(unittest.TestCase):
    def test_archive_contains_dirty_and_untracked_source_and_excludes_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
            git('init','-q');(root/'tracked.py').write_text('before\n');(root/'.gitignore').write_text('ignored/\n')
            git('add','.');git('-c','user.name=fixture','-c','user.email=fixture@example.test','commit','-qm','fixture')
            (root/'tracked.py').write_text('dirty\n');(root/'new.py').write_text('untracked\n')
            (root/'ignored').mkdir();(root/'ignored/build').write_text('ignore')
            (root/'result').mkdir();(root/'result/output').write_text('generated')
            with patch('joint_scheduling.experiments.provenance.REPO',root):
                snapshot=freeze_source(root/'result/source',(root/'result',))
            manifest=json.loads((snapshot/'manifest.json').read_text())
            self.assertEqual({x['path'] for x in manifest['files']},{'.gitignore','tracked.py','new.py'})
            with tarfile.open(snapshot/'source.tar.gz') as archive:
                self.assertEqual(archive.extractfile('tracked.py').read(),b'dirty\n')
                for item in manifest['files']:
                    self.assertEqual(hashlib.sha256(archive.extractfile(item['path']).read()).hexdigest(),item['sha256'])
                exported=root/'exported';exported.mkdir();archive.extractall(exported,filter='fully_trusted')
            with patch('joint_scheduling.experiments.provenance.REPO',exported):
                again=freeze_source(exported/'new-result/source',(exported/'new-result',))
            restored=json.loads((again/'manifest.json').read_text())
            self.assertIsNone(restored['git_head'])
            self.assertEqual(restored['source_tree_sha256'],manifest['source_tree_sha256'])
