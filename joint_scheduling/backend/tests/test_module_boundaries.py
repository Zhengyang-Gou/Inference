"""Guard dependency direction and the public compatibility entry points."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


class ModuleBoundaryTests(unittest.TestCase):
    def test_input_config_profile_and_planning_tools_do_not_load_execution(self):
        # Fresh interpreters detect accidental transitive imports regardless of
        # which integration tests have already loaded the engine in this one.
        script = """
import importlib, json, sys
from pathlib import Path
module = importlib.import_module(sys.argv[1])
if sys.argv[1] == 'joint_scheduling.experiments.planning':
    for name in ('kv_policy', 'network_priority'):
        module.plan_suite(Path('joint_scheduling/configs/research') / (name + '.json'))
blocked = ('joint_scheduling.backend.runtime',
           'joint_scheduling.backend.expert_parallel',
           'joint_scheduling.backend.execution',
           'joint_scheduling.backend.client',
           'joint_scheduling.backend.runner',
           'joint_scheduling.experiments.runner')
print(json.dumps([name for name in blocked if name in sys.modules]))
"""
        for module in ('joint_scheduling.config', 'joint_scheduling.workload',
                       'joint_scheduling.backend.compute_profile',
                       'joint_scheduling.experiments.planning', 'joint_scheduling.cli'):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-c', script, module],
                                        cwd=ROOT, capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout), [])

    def test_legacy_imports_reference_the_same_domain_objects(self):
        from joint_scheduling.backend import expert_parallel, inputs, runtime, settings
        from joint_scheduling.backend.expert_config import ExpertParallelConfig
        from joint_scheduling.backend.expert_profile import ExpertProfile
        from joint_scheduling.validation import number, read_json
        from joint_scheduling.workload import load_workload, validate_requests

        self.assertIs(runtime.RuntimeConfig, settings.RuntimeConfig)
        self.assertIs(runtime.parse_config, settings.parse_config)
        self.assertIs(expert_parallel.ExpertParallelConfig, ExpertParallelConfig)
        self.assertIs(expert_parallel.ExpertProfile, ExpertProfile)
        for name, original in (('number', number), ('read_json', read_json),
                               ('load_workload', load_workload),
                               ('validate_requests', validate_requests)):
            self.assertIs(getattr(inputs, name), original)
            self.assertIs(getattr(runtime, name), original)

    def test_backend_commands_do_not_read_the_experiment_config(self):
        from joint_scheduling.cli import main

        report = {'backend_ready': True}
        with patch('joint_scheduling.backend.build.doctor', return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config', '/missing/experiment.json', 'backend', 'doctor'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_override_values_are_detached_and_invalid_paths_leave_input_intact(self):
        from joint_scheduling.experiments.parameters import set_override

        value = [{'gpu_ids': [0]}]
        raw = {'topology': {}}
        set_override(raw, 'topology.placements', value)
        value[0]['gpu_ids'].append(1)
        self.assertEqual(raw['topology']['placements'], [{'gpu_ids': [0]}])
        with self.assertRaisesRegex(ValueError, 'override requires an object'):
            set_override(raw, 'runtime.seed', 7)
        self.assertNotIn('runtime', raw)


if __name__ == '__main__':
    unittest.main()
