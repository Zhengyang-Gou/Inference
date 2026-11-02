"""Public ports, state ownership and frozen-output migration acceptance."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.backend.execution import ExecutionCoordinator, RuntimeHooks
from joint_scheduling.backend.execution.requests import RequestRecord
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.config import load_config


class ComponentTests(unittest.TestCase):
    def test_request_inputs_do_not_change_with_progress_or_predictor_snapshot(self):
        source=dict(request_id='r',input_tokens=8,output_tokens=3,prefix_blocks=[dict(id='x',tokens=4)])
        row=RequestRecord(source,p='p',d='d',state='queued_prefill',generated=0)
        row['kv_received_bytes']=4;row['prefill_computed_tokens']=4;row['segment_index']=1
        self.assertEqual(row.kv,{'kv_received_bytes':4})
        self.assertEqual(row.prefill,{'prefill_computed_tokens':4})
        self.assertEqual(row.execution,{'segment_index':1})
        with self.assertRaises(TypeError):row['input_tokens']=9
        with self.assertRaises(TypeError):del row['output_tokens']
        snapshot=copy.deepcopy(row);snapshot['prefix_blocks'][0]['tokens']=2;snapshot['generated']=9
        self.assertEqual(row['generated'],0);self.assertEqual(row['prefix_blocks'][0]['tokens'],4)
        source['prefix_blocks'][0]['tokens']=1
        self.assertEqual(row['prefix_blocks'][0]['tokens'],4)
        row['diagnostic_alias']=row
        snapshot=copy.deepcopy(row)
        self.assertIs(snapshot['diagnostic_alias'],snapshot)

    def test_component_state_has_one_owner_and_compatibility_calls_route_to_it(self):
        engine=ExecutionCoordinator.__new__(ExecutionCoordinator);engine._install_components()
        self.assertEqual(ExecutionCoordinator.__bases__,(object,))
        engine.dispatch_generation=3
        self.assertEqual(engine.components['kv'].dispatch_generation,3)
        self.assertNotIn('dispatch_generation',engine.__dict__)
        engine.components['expert'].collective_counter=5
        self.assertEqual(engine.components['network'].collective_counter,5)
        self.assertEqual(engine.kick_decode.__self__,engine.components['decode'])
        with self.assertRaisesRegex(AttributeError,'undeclared'):
            engine.components['kv'].accidental_typo=1

    @unittest.skipUnless(backend_available(),'native backend not built')
    def test_frozen_dense_and_moe_outputs_survive_component_migration(self):
        golden=json.loads((PACKAGE/'backend/fixtures/runtime_equivalence.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            for name,case in golden['cases'].items():
                output=Path(directory)/name
                run_baseline(load_config(PACKAGE/case['config']),output)
                for file,digest in case['sha256'].items():
                    with self.subTest(case=name,file=file):
                        self.assertEqual(hashlib.sha256((output/file).read_bytes()).hexdigest(),digest)
                if name=='moe':
                    moved=output.rename(Path(directory)/'moved')
                    replayed=Path(directory)/'reproduced'
                    run_baseline(load_config(moved/'config.json'),replayed)
                    for file,digest in case['sha256'].items():
                        self.assertEqual(hashlib.sha256((replayed/file).read_bytes()).hexdigest(),digest,file)

    @unittest.skipUnless(backend_available(),'native backend not built')
    def test_observation_callbacks_receive_copies_and_add_no_simulator_work(self):
        golden=json.loads((PACKAGE/'backend/fixtures/runtime_equivalence.json').read_text())['cases']['moe']
        seen=set()
        def event(name,now,details):
            seen.add(name);details.clear()
        def sample(state):
            state['resources'].clear();state['flows'].clear()
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'run'
            run_baseline(load_config(PACKAGE/golden['config']),output,hooks=RuntimeHooks(event,sample))
            self.assertIn('transfer_started',seen)
            for file,digest in golden['sha256'].items():
                self.assertEqual(hashlib.sha256((output/file).read_bytes()).hexdigest(),digest,file)
