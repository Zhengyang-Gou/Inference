import json
import tempfile
import unittest
from pathlib import Path
from joint_scheduling.backend.profile_validation import holdout_curve,aggregate_curve,joint_coordinate_holdout
from joint_scheduling.backend.tests.test_chunked_prefill import profile_fixture
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.experiments.sensitivity import perturb_profile


class HoldoutTests(unittest.TestCase):
    def test_duplicate_coordinates_are_aggregated_and_interior_coordinates_are_tested_once(self):
        curve=aggregate_curve([dict(x=x,**{'time_stats.op.median':v}) for x,v in ((1,1),(2,3),(2,5),(3,9),(4,16),(5,25))],'x',('op',))
        self.assertEqual(curve[2],4)
        rows=holdout_curve(curve,'quadratic','all')
        self.assertEqual(sorted(r['coordinate'] for r in rows),[2,3,4])
        middle=next(r for r in rows if r['coordinate']==3)
        self.assertEqual(middle['observed_ms'],9)
        self.assertEqual(middle['predicted_ms'],10)
        self.assertEqual(middle['training_coordinates'],4)
        self.assertEqual(holdout_curve({1:1,2:2},'small','a'),[])

    def test_held_out_linear_values_are_reconstructed_without_using_them(self):
        curve={i:2*i+7 for i in range(1,50)}
        result=holdout_curve(curve,'linear','all')
        self.assertEqual(len(result),47)
        self.assertTrue(all(r['absolute_error_ms']==0 for r in result))
        with self.assertRaises(ValueError):holdout_curve(curve,'linear','all',1)

    def test_both_axes_and_corresponding_linear_samples_are_withheld_together(self):
        curves={q:{c:2*q+3*c+5 for c in range(0,13,2)} for q in range(1,8)}
        linear={q:4*q+1 for q in curves};embedding={q:q*.1 for q in curves}
        rows,summary=joint_coordinate_holdout(curves,linear,embedding,'affine',layers=2)
        self.assertEqual(summary['evaluated_points'],25)
        self.assertEqual(summary['skipped_points'],0)
        self.assertEqual(len({(r['query_tokens_or_batch'],r['cached_context']) for r in rows}),25)
        self.assertTrue(all(r['absolute_error_ms']<1e-10 and r['heldout_axes_absent_from_training'] for r in rows))
        self.assertFalse(summary['end_to_end_measurements'])


class SensitivityTests(unittest.TestCase):
    def test_scaling_and_once_per_step_overhead_preserve_all_bytes(self):
        original=profile_fixture();raw=perturb_profile(original,1.2,.001)
        self.assertNotIn('sensitivity',original['provenance'])
        self.assertEqual(raw['kv_bytes_per_token'],original['kv_bytes_per_token'])
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'p.json';p.write_text(json.dumps(original));base=ComputeProfile(p)
            p.write_text(json.dumps(raw));changed=ComputeProfile(p)
            a=base.prefill_chunks(9,2,4);b=changed.prefill_chunks(9,2,4)
            for before,after in zip(a,b):
                self.assertAlmostEqual(sum(s['compute_seconds'] for s in after['steps']),1.2*sum(s['compute_seconds'] for s in before['steps'])+.001)
                self.assertEqual([s['collective_bytes'] for s in after['steps']],[s['collective_bytes'] for s in before['steps']])
            for size in (1,2,3,4):
                before=base.lookup_decode_batch([10]*size,2);after=changed.lookup_decode_batch([10]*size,2)
                self.assertAlmostEqual(after['compute_seconds'],before['compute_seconds']*1.2+.001)
                self.assertEqual(after['collective_bytes'],before['collective_bytes'])
                self.assertEqual(after['collectives_per_step'],before['collectives_per_step'])
            before=base.prefill_steps(9,2);after=changed.prefill_steps(9,2)
            self.assertAlmostEqual(sum(s['compute_seconds'] for s in after),sum(s['compute_seconds'] for s in before)*1.2+.001)
        for scale,overhead in ((0,0),(1,-1),(float('nan'),0)):
            with self.assertRaises(ValueError):perturb_profile(original,scale,overhead)
