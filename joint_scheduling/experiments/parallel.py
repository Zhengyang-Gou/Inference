"""Bounded spawn-based scheduling; the caller owns journaling and aggregation."""
import multiprocessing
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait


def execution_options(jobs, run_count, fail_fast=False):
    if type(jobs) is not int or jobs < 1:
        raise ValueError('jobs must be a positive integer')
    workers = min(jobs, run_count)
    return dict(requested_jobs=jobs, max_workers=workers,
                mode='multiprocess' if workers > 1 else 'serial',
                start_method='spawn' if workers > 1 else None,
                journal_order='completion' if workers > 1 else 'plan',
                summary_order='plan',
                fail_fast_enabled=bool(fail_fast),
                fail_fast='Stop submitting after an observed failure; submitted runs finish; unsubmitted runs are skipped.')


def run_tasks(tasks, jobs=1, fail_fast=False, worker=None):
    """Yield (plan index, result) without queuing more than jobs runs at once."""
    from .execution import execute_run, failed_run, skipped_run
    worker = execute_run if worker is None else worker
    options = execution_options(jobs, len(tasks), fail_fast)
    stopped = False
    if options['max_workers'] <= 1:
        for index, task in enumerate(tasks):
            if stopped:
                row = skipped_run(task)
            else:
                try:
                    row = worker(task)
                except Exception as error:
                    row = failed_run(task, error)
            stopped = stopped or (fail_fast and row['status'] == 'failed')
            yield index, row
        return

    pending, next_index = {}, 0
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=options['max_workers'], mp_context=context) as pool:
        while pending or next_index < len(tasks):
            while not stopped and next_index < len(tasks) and len(pending) < options['max_workers']:
                index = next_index
                next_index += 1
                try:
                    pending[pool.submit(worker, tasks[index])] = index
                except Exception as error:
                    row = failed_run(tasks[index], error)
                    stopped = fail_fast
                    yield index, row
            if stopped:
                while next_index < len(tasks):
                    yield next_index, skipped_run(tasks[next_index])
                    next_index += 1
            if not pending:
                continue
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            batch = []
            for future in sorted(completed, key=lambda item: pending[item]):
                index = pending.pop(future)
                try:
                    row = future.result()
                except Exception as error:
                    row = failed_run(tasks[index], error)
                stopped = stopped or (fail_fast and row['status'] == 'failed')
                batch.append((index, row))
            yield from batch
