"""Static experiment routing stays balanced, decoupled and reproducible."""
import copy
import random
import unittest
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from joint_scheduling.binding import BindingConfig
from joint_scheduling.binding.static import binding_plan_document, plan_bindings, validate_static_settings
from joint_scheduling.config import load_config
from joint_scheduling.experiments import plan_suite
from joint_scheduling.validation import ConfigError, read_settings


PACKAGE = Path(__file__).resolve().parents[2]


class StaticBindingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(PACKAGE/'configs/research/base.json')
        self.requests = [dict(request_id=f'r{i:04d}', arrival_seconds=i/100,
                              input_tokens=8, output_tokens=4) for i in range(515)]

    def test_balanced_cycles_and_multiple_destinations_per_prefiller(self):
        for name in ('base.json', 'base_160gpu.json'):
            with self.subTest(layout=name):
                config = load_config(PACKAGE/'configs/research'/name)
                plan = plan_bindings(self.requests, config.topology, config.binding)
                ps = [p.instance_id for p in config.topology.placements if p.role == 'prefill']
                ds = {p.instance_id for p in config.topology.placements if p.role == 'decode'}
                for start in range(0, len(plan)-len(ds)+1, len(ds)):
                    self.assertEqual({b.decode_instance_id for b in plan[start:start+len(ds)]}, ds)
                counts = Counter(b.decode_instance_id for b in plan)
                self.assertLessEqual(max(counts.values())-min(counts.values()), 1)
                targets = defaultdict(set)
                for i, binding in enumerate(plan):
                    self.assertEqual(binding.prefill_instance_id, ps[i%len(ps)])
                    targets[binding.prefill_instance_id].add(binding.decode_instance_id)
                self.assertTrue(all(len(destinations) > 1 for destinations in targets.values()))

    def test_plan_ignores_lengths_and_global_rng_and_preserves_arrival_order(self):
        before = random.getstate()
        a = binding_plan_document(self.requests, self.config.topology, self.config.binding)
        changed = copy.deepcopy(self.requests)
        for row in changed:
            row.update(input_tokens=14050, output_tokens=1000)
        b = binding_plan_document(list(reversed(changed)), self.config.topology, self.config.binding)
        self.assertEqual(a, b)
        self.assertEqual(before, random.getstate())
        other = binding_plan_document(self.requests, self.config.topology, replace(self.config.binding, seed=19))
        self.assertNotEqual(a['bindings_sha256'], other['bindings_sha256'])
        self.assertEqual(a['prefill_counts'], other['prefill_counts'])

    def test_cross_leaf_plan_and_legacy_local_pairing(self):
        document = binding_plan_document(self.requests, self.config.topology, self.config.binding)
        self.assertGreater(document['cross_leaf_requests'], 0)
        self.assertLess(document['cross_leaf_requests'], len(self.requests))
        self.assertEqual(sum(document['decode_counts'].values()), len(self.requests))
        for name in ('base.json', 'base_160gpu.json'):
            with self.subTest(layout=name):
                config = load_config(PACKAGE/'configs/research'/name)
                legacy = plan_bindings(self.requests, config.topology,
                                       replace(config.binding, policy='round_robin'))
                ps = [p.instance_id for p in config.topology.placements if p.role == 'prefill']
                ds = [p.instance_id for p in config.topology.placements if p.role == 'decode']
                self.assertTrue(all(b.prefill_instance_id == ps[i%len(ps)] and b.decode_instance_id == ds[i%len(ds)]
                                    for i,b in enumerate(legacy)))

    def test_static_policy_rejects_dynamic_dispatch_and_invalid_seeds(self):
        for field in ('window_enabled', 'joint_selection_enabled'):
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, 'at arrival'):
                validate_static_settings(replace(self.config.binding, **{field: True}))
        for seed in (-1, True, 2147483647):
            with self.subTest(seed=seed), self.assertRaises(ConfigError):
                read_settings(BindingConfig, {'policy':'balanced_random', 'seed':seed}, 'binding')

    def test_main_comparisons_pin_one_plan_across_cases_and_network_seeds(self):
        plans = [plan_suite(PACKAGE/f'configs/research/{name}.json')
                 for name in ('kv_policy', 'network_priority')]
        a, b = [p['comparison_audit']['static_binding'] for p in plans]
        self.assertEqual(a, b)
        self.assertEqual(a['request_count'], 19366)
        self.assertEqual(a['seed'], 7)
        self.assertLessEqual(max(a['decode_counts'].values())-min(a['decode_counts'].values()), 1)
        self.assertEqual({r['seed'] for r in plans[0]['runs']}, {7, 19})


if __name__ == '__main__':
    unittest.main()
