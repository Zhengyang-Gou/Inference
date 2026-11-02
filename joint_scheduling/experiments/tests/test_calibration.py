import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.experiments.calibration import assess_calibration


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root/'plan.json'
        self.results = self.root/'runs'
        self.rows = []
        (self.root/'baseline.json').write_text(json.dumps(dict(topology=dict(gpus_per_node=2))))
        protocol = dict(minimum_measurement_seconds=240,network_seeds=[7,19],baseline_acceptance=dict(
            minimum_slo_fraction_lower=.95,maximum_positive_backlog_growth_fraction=.05))
        (self.root/'protocol.json').write_text(json.dumps(protocol))
        for rate in (1,2):
            for seed in (7,19):
                self.rows.append(dict(run_id=f'rate-{rate}-seed-{seed}',dataset='sample',target_rps=rate,
                    seed=seed,case='baseline_whole',warmup_seconds=60,injection_seconds=300,request_count=2,
                    config='baseline.json',
                    workload_sha256=hashlib.sha256(b'workload').hexdigest(),
                    profile_sha256=hashlib.sha256(b'profile').hexdigest()))
        self.write_plan()

    def write_plan(self):
        self.plan.write_text(json.dumps(dict(protocol='protocol.json',runs=self.rows)))

    def write_results(self, rate, passed=True, unknown=0):
        for row in self.rows:
            if row['target_rps'] != rate:
                continue
            root = self.results/row['run_id']
            (root/'result').mkdir(parents=True)
            (root/'workload.json').write_bytes(b'workload')
            (root/'compute_profile.json').write_bytes(b'profile')
            (root/'resolved_config.json').write_text(json.dumps(dict(topology=dict(gpus_per_node=2),effective_baseline=dict(
                kv='whole_ready_immediate',endpoint_selection='round_robin'))))
            pressure = dict(measurement_start_seconds=60,measurement_end_seconds=300,backlog_change=0,
                measured_arrivals=2,cohort_count=2,cohort_slo_fraction_lower=1.0 if passed else .5,
                cohort_slo_unknown=unknown)
            (root/'result/summary.json').write_text(json.dumps(dict(pressure=pressure)))
            rows=[dict(arrived_at_seconds=100,completed_at_seconds=101,failed_at_seconds='',status='completed'),
                  dict(arrived_at_seconds=200,completed_at_seconds=201,failed_at_seconds='',status='completed')]
            with (root/'result/requests.csv').open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)

    def test_absent_runs_never_become_measurements(self):
        report=assess_calibration(self.plan,self.results)
        self.assertEqual(report['completed_trial_count'],0)
        self.assertIsNone(report['selections']['sample']['measured_reference_rps'])

    def test_all_seeds_and_higher_rejection_are_required_for_reference(self):
        self.write_results(1)
        partial=assess_calibration(self.plan,self.results)
        self.assertIsNone(partial['selections']['sample']['measured_reference_rps'])
        self.write_results(2,passed=False)
        report=assess_calibration(self.plan,self.results)
        self.assertEqual(report['selections']['sample']['measured_reference_rps'],1)
        self.assertEqual(report['selections']['sample']['higher_rejected_rps'],2)
        self.assertEqual(report['selections']['sample']['status'],'finite_trace_baseline_bracket')

    def test_unknown_slos_do_not_count_as_acceptance(self):
        self.write_results(1,unknown=1)
        report=assess_calibration(self.plan,self.results)
        self.assertTrue(all(t['accepted'] is False for t in report['trials'] if t['status']=='observed'))

    def test_missing_seed_is_rejected(self):
        self.rows.pop()
        self.write_plan()
        with self.assertRaisesRegex(ValueError,'seeds'):
            assess_calibration(self.plan,self.results)

    def test_one_second_measurement_is_not_accepted_as_capacity_calibration(self):
        self.write_results(1)
        for row in self.rows:
            if row['target_rps']==1:
                row['injection_seconds']=61
                path=self.results/row['run_id']/'result/summary.json'
                summary=json.loads(path.read_text())
                summary['pressure']['measurement_end_seconds']=61
                path.write_text(json.dumps(summary))
        self.write_plan()
        with self.assertRaisesRegex(ValueError,'shorter'):
            assess_calibration(self.plan,self.results)

    def test_other_hardware_cannot_be_used_for_this_reference(self):
        self.write_results(1)
        path=self.results/self.rows[0]['run_id']/'resolved_config.json'
        config=json.loads(path.read_text())
        config['topology']['gpus_per_node']=8
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError,'topology'):
            assess_calibration(self.plan,self.results)


if __name__=='__main__':
    unittest.main()
