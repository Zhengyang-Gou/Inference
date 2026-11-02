"""The repository boundary is CPU simulation plus offline input processing."""
import ast
import hashlib
import json
import unittest
from pathlib import Path
from joint_scheduling.backend.build import REPO, PACKAGE
from joint_scheduling.backend.compute_profile import profile_kind
from joint_scheduling.experiments import plan_suite
from joint_scheduling.config import load_config


class SimulationScopeTests(unittest.TestCase):
    def test_retained_python_sources_do_not_import_device_execution_libraries(self):
        blocked={'torch','tensorflow','cupy','vllm','deep_gemm','flash_attn','flashinfer','pynvml'}
        roots=[PACKAGE,REPO/'third_party/aicb',REPO/'third_party/vidur/vidur',REPO/'third_party/simccl']
        for root in roots:
            for path in root.rglob('*.py'):
                if 'artifacts' in path.parts:continue
                tree=ast.parse(path.read_text(),filename=str(path))
                for node in ast.walk(tree):
                    modules=([a.name for a in node.names] if isinstance(node,ast.Import)
                        else [node.module or ''] if isinstance(node,ast.ImportFrom) else [])
                    for module in modules:
                        with self.subTest(path=str(path.relative_to(REPO)),module=module):
                            self.assertNotIn(module.split('.')[0],blocked)

    def test_simulation_profile_identity_is_shared_by_all_research_specs(self):
        path=load_config(PACKAGE/'configs/research/base.json').profile_path()
        raw=json.loads(path.read_text());digest=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(raw['provenance']['synthetic'])
        self.assertEqual(profile_kind(raw['provenance']),'estimated_simulation_profile')
        for name in ('kv_policy','network_priority','validation','contention_diagnostic'):
            with self.subTest(spec=name):
                plan=plan_suite(PACKAGE/('configs/research/'+name+'.json'))
                audit=plan['comparison_audit']
                self.assertEqual(audit['input_sha256']['profile'],digest)
                self.assertEqual(audit['model']['status'],'estimated_simulation_profile')
