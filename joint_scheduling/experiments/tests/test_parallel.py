"""Process isolation, bounded scheduling and serial/parallel equivalence."""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.experiments import plan_suite, run_suite
from joint_scheduling.experiments.execution import RunTask
from joint_scheduling.experiments.parallel import execution_options, run_tasks


def rendezvous_worker(task):
    """Cannot finish successfully unless two child processes run concurrently."""
    marker = Path(task.output) / ('started-' + task.planned['run_id'])
    marker.write_text(str(os.getpid()))
    deadline = time.monotonic() + 10
    while len(list(Path(task.output).glob('started-*'))) < 2:
        if time.monotonic() > deadline:
            raise RuntimeError('second worker never started')
        time.sleep(.01)
    return dict(task.result_row('completed'), worker_pid=os.getpid())


def crashing_worker(task):
    os._exit(3)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = [RunTask(dict(run_id=f'case-{i}', case_id=f'case-{i}', seed=7,
                                   workload_seed=None, parameters={}), {}, None,
                              str(self.root), str(self.root / 'source')) for i in range(5)]

    def pool(self, failure=None, submission_error=False):
        submitted = []

        class Pool:
            def __init__(self, **kwargs):
                self.options = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, worker, task):
                submitted.append(task.planned['run_id'])
                if submission_error:
                    raise BrokenProcessPool('worker pool unavailable')
                future = Future()
                if task is self_tasks[0] and isinstance(failure, Exception):
                    future.set_exception(failure)
                else:
                    status = 'failed' if task is self_tasks[0] and failure else 'completed'
                    future.set_result(task.result_row(status))
                return future

        self_tasks = self.tasks
        return Pool, submitted

    def test_invalid_jobs_fail_before_reading_inputs_or_creating_output(self):
        for jobs in (0, -1, True, 1.5, '2'):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, 'positive integer'):
                run_suite(self.root / 'missing.json', self.root / 'out', jobs=jobs)
        self.assertFalse((self.root / 'out').exists())
        self.assertEqual(execution_options(8, 2)['max_workers'], 2)

    def test_serial_default_uses_no_pool_and_keeps_fail_fast_order(self):
        def worker(task):
            raise ValueError('invalid run')

        with patch('joint_scheduling.experiments.parallel.ProcessPoolExecutor') as pool:
            results = list(run_tasks(self.tasks, fail_fast=True, worker=worker))
        pool.assert_not_called()
        self.assertEqual([row['status'] for _, row in results], ['failed'] + ['skipped'] * 4)

    def test_spawn_workers_are_distinct_and_really_overlap(self):
        results = list(run_tasks(self.tasks[:2], jobs=2, worker=rendezvous_worker))
        self.assertTrue(all(row['status'] == 'completed' for _, row in results))
        pids = {row['worker_pid'] for _, row in results}
        self.assertEqual(len(pids), 2)
        self.assertNotIn(os.getpid(), pids)

    def test_submission_is_bounded_and_progress_does_not_wait_for_all_runs(self):
        pool, submitted = self.pool()
        with patch('joint_scheduling.experiments.parallel.ProcessPoolExecutor', pool):
            stream = run_tasks(self.tasks, jobs=2)
            first = next(stream)
            self.assertEqual(len(submitted), 2)
            results = [first, *stream]
        self.assertEqual(sorted(i for i, _ in results), list(range(5)))

    def test_fail_fast_drains_submitted_runs_without_starting_remaining_runs(self):
        pool, submitted = self.pool(failure=True)
        with patch('joint_scheduling.experiments.parallel.ProcessPoolExecutor', pool):
            results = dict(run_tasks(self.tasks, jobs=2, fail_fast=True))
        self.assertEqual(submitted, ['case-0', 'case-1'])
        self.assertEqual([results[i]['status'] for i in range(5)],
                         ['failed', 'completed', 'skipped', 'skipped', 'skipped'])
        self.assertFalse(self.tasks[2].case_dir.exists())

    def test_future_errors_are_recorded_and_other_runs_continue(self):
        pool, _ = self.pool(failure=RuntimeError('worker lost'))
        with patch('joint_scheduling.experiments.parallel.ProcessPoolExecutor', pool):
            results = dict(run_tasks(self.tasks, jobs=2))
        self.assertEqual(results[0]['status'], 'failed')
        self.assertTrue(all(results[i]['status'] == 'completed' for i in range(1, 5)))
        error = json.loads((self.tasks[0].case_dir / 'error.json').read_text())
        self.assertIn('worker lost', error['error'])

    def test_broken_pool_submission_obeys_fail_fast_without_hanging(self):
        pool, submitted = self.pool(submission_error=True)
        with patch('joint_scheduling.experiments.parallel.ProcessPoolExecutor', pool):
            results = dict(run_tasks(self.tasks, jobs=2, fail_fast=True))
        self.assertEqual(submitted, ['case-0'])
        self.assertEqual([results[i]['status'] for i in range(5)], ['failed'] + ['skipped'] * 4)

    def test_abrupt_child_exit_produces_failures_and_skipped_records(self):
        results = dict(run_tasks(self.tasks, jobs=2, fail_fast=True, worker=crashing_worker))
        self.assertEqual([results[i]['status'] for i in range(5)],
                         ['failed', 'failed', 'skipped', 'skipped', 'skipped'])
        self.assertIn('BrokenProcessPool', results[0]['error'])
        self.assertTrue((self.tasks[0].case_dir / 'error.json').is_file())

    def test_broken_pool_without_fail_fast_records_every_remaining_run(self):
        tasks = [replace(task, output=str(self.root / 'continued')) for task in self.tasks]
        results = dict(run_tasks(tasks, jobs=2, worker=crashing_worker))
        self.assertEqual(len(results), len(tasks))
        self.assertTrue(all(row['status'] == 'failed' for row in results.values()))
        self.assertTrue(all((task.case_dir / 'error.json').is_file() for task in tasks))


class ParallelSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'suite.json'

    def save(self, spec):
        self.path.write_text(json.dumps(spec))
        return self.path

    def generated_spec(self):
        base = json.loads((PACKAGE / 'configs/baseline_smoke.json').read_text())
        base['feedback']['compute_profile_path'] = str(PACKAGE / 'backend/fixtures/compute_profile.json')
        (self.root / 'base.json').write_text(json.dumps(base))
        generator = json.loads((PACKAGE / 'configs/workload_example.json').read_text())
        generator['arrival'].update(pattern='periodic', count=3, rate_rps=300)
        (self.root / 'generator.json').write_text(json.dumps(generator))
        return dict(schema_version=1, base_config='base.json', workload_spec='generator.json',
                    seeds=[7, 19], sweep={'topology.nic_bandwidth_gbps': [100, 200]})

    def rows(self, output):
        return {row['run_id']: row for row in
                map(json.loads, (output / 'runs.jsonl').read_text().splitlines())}

    def assert_equivalent(self, left, right, files):
        a, b = self.rows(left), self.rows(right)
        self.assertEqual(set(a), set(b))
        for run_id in a:
            with self.subTest(run=run_id):
                for key in a[run_id]:
                    if key == 'wall_seconds' or key == 'worker_pid' or key.startswith(('binding_round_', 'binding_inclusive_')):
                        continue
                    self.assertEqual(a[run_id][key], b[run_id][key], key)
                for filename in files:
                    self.assertEqual((left / a[run_id]['output_dir'] / 'run' / filename).read_bytes(),
                                     (right / b[run_id]['output_dir'] / 'run' / filename).read_bytes(), filename)

    def test_parallel_dry_run_and_invalid_jobs_have_no_output_side_effect(self):
        path = self.save(self.generated_spec())
        command = [sys.executable, '-m', 'joint_scheduling', 'run-experiments',
                   '--spec', str(path), '--output', str(self.root / 'out'), '--dry-run']
        result = subprocess.run([*command, '--jobs', '2'], cwd=PACKAGE.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['execution']['max_workers'], 2)
        result = subprocess.run([*command, '--jobs', '0'], cwd=PACKAGE.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / 'out').exists())

    @unittest.skipUnless(backend_available(), 'native backend not built')
    def test_generated_workloads_match_and_parent_owns_progress_and_summary_order(self):
        path = self.save(self.generated_spec())
        serial, parallel = self.root / 'serial', self.root / 'parallel'
        run_suite(path, serial)
        seen = []
        report = run_suite(path, parallel, jobs=2, make_plots=True,
                           progress=lambda row: seen.append((os.getpid(), row['run_id'])))
        self.assertEqual(report['successful_runs'], 4)
        self.assertEqual({pid for pid, _ in seen}, {os.getpid()})
        self.assertEqual(len({run_id for _, run_id in seen}), 4)
        self.assertEqual(len({row['worker_pid'] for row in self.rows(parallel).values()}), 2)
        self.assertEqual({row['worker_pid'] for row in self.rows(serial).values()}, {os.getpid()})
        self.assert_equivalent(serial, parallel, ['result/tokens.csv', 'result/requests.csv',
                                                 'result/timeseries.csv', 'final_resources.json'])
        self.assertEqual((serial / 'comparison.csv').read_bytes(), (parallel / 'comparison.csv').read_bytes())
        with (parallel / 'runs.csv').open() as handle:
            order = [row['run_id'] for row in csv.DictReader(handle)]
        self.assertEqual(order, [row['run_id'] for row in plan_suite(path)['runs']])
        self.assertEqual(json.loads((parallel / 'execution.json').read_text()), report['execution'])
        self.assertTrue(list((parallel / 'runs').glob('*/run/figures/*.png')))

    @unittest.skipUnless(backend_available(), 'native backend not built')
    def test_ep_policies_share_frozen_bindings_source_and_identical_serial_results(self):
        raw = json.loads((PACKAGE / 'configs/ep_mlu_smoke.json').read_text())
        raw['feedback']['compute_profile_path'] = str(PACKAGE / 'backend/fixtures/ep_compute_profile.json')
        raw['runtime']['workload_path'] = str(PACKAGE / 'backend/fixtures/ep_workload.json')
        raw['binding'].update(policy='balanced_random', seed=7, decision_overhead_us=0,
                              wall_clock_diagnostics=False)
        raw['runtime']['network_priority_enabled'] = False
        (self.root / 'base.json').write_text(json.dumps(raw))
        profile = PACKAGE / 'backend/fixtures/ep_compute_profile.json'
        workload = PACKAGE / 'backend/fixtures/ep_workload.json'
        spec = dict(schema_version=1, base_config='base.json', profile=str(profile), workload=str(workload),
                    seeds=[7], cases=[dict(id='immediate', overrides={
                        'runtime.kv_transfer_mode': 'layer_ready_immediate', 'kv_scheduler.policy': 'slo_slack',
                        'kv_scheduler.slo_priority_enabled': False, 'kv_scheduler.collective_protection_enabled': False}),
                        dict(id='mlu', overrides={})], comparison=dict(purpose='validation', allowed_changes=[
                            'runtime.kv_transfer_mode', 'kv_scheduler.policy', 'kv_scheduler.slo_priority_enabled',
                            'kv_scheduler.collective_protection_enabled'], fixed={'binding.policy': 'balanced_random'},
                            input_sha256={'profile': hashlib.sha256(profile.read_bytes()).hexdigest(),
                                          'workload': hashlib.sha256(workload.read_bytes()).hexdigest()}))
        path = self.save(spec)
        serial, parallel = self.root / 'serial', self.root / 'parallel'
        run_suite(path, serial)
        report = run_suite(path, parallel, jobs=2)
        self.assertEqual(report['successful_runs'], 2)
        self.assert_equivalent(serial, parallel, ['result/tokens.csv', 'result/requests.csv',
                                                 'expert_parallel_summary.json', 'final_resources.json', 'binding_plan.json'])
        self.assertEqual((serial / 'inputs/binding_plan.json').read_bytes(),
                         (parallel / 'inputs/binding_plan.json').read_bytes())
        for row in self.rows(parallel).values():
            identity = json.loads((parallel / row['output_dir'] / 'run/reproducibility.json').read_text())
            source = parallel / row['output_dir'] / 'run' / identity['source']['archive']
            self.assertEqual(source.resolve(), (parallel / 'inputs/source/source.tar.gz').resolve())

    @unittest.skipUnless(backend_available(), 'native backend not built')
    def test_parallel_cli_from_another_directory_reports_failure_and_continues(self):
        spec = self.generated_spec()
        spec.update(seeds=[7], sweep={'topology.nic_bandwidth_gbps': [0, 200]})
        path = self.save(spec)
        output = self.root / 'cli'
        result = subprocess.run([str(PACKAGE.parent / 'inference'), 'run-experiments', '--spec', str(path),
                                 '--output', str(output), '--jobs', '2'], cwd=self.root,
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads((output / 'summary.json').read_text())
        self.assertEqual((report['successful_runs'], report['failed_runs']), (1, 1))
        self.assertEqual(report['execution']['mode'], 'multiprocess')
        self.assertTrue((output / 'runs/case-0000-seed-7/error.json').is_file())


if __name__ == '__main__':
    unittest.main()
