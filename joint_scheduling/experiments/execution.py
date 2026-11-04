"""One isolated experiment task; workers never write suite-level results."""
import copy
import hashlib
import os
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..binding.static import binding_plan_document
from ..config import load_config
from ..validation import read_json
from ..workload import write_workload
from ..workload.inputs import load_workload
from .metrics import extract_metrics
from .parameters import set_override
from .provenance import write_json


@dataclass(frozen=True)
class RunTask:
    planned: dict
    base_config: dict
    generator_spec: Optional[dict]
    output: str
    source_snapshot: str
    make_plots: bool = False
    binding_plan_path: Optional[str] = None

    @property
    def case_dir(self):
        return Path(self.output) / 'runs' / self.planned['run_id']

    def result_row(self, status):
        return dict(self.planned, output_dir='runs/' + self.planned['run_id'],
                    experiment_type='end_to_end', status=status, worker_pid=None)


def freeze_binding_plan(plan, inputs):
    """Publish the audited binding plan once, before any workers are started."""
    audited = plan.get('comparison_audit', {}).get('static_binding')
    if not audited:
        return None
    raw = copy.deepcopy(plan['base_config'])
    for key, value in plan['runs'][0]['parameters'].items():
        set_override(raw, key, value)
    raw['feedback']['compute_profile_path'] = str(inputs / 'compute_profile.json')
    raw['runtime']['workload_path'] = str(inputs / 'workload.json')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'config.json'
        write_json(path, raw)
        config = load_config(path)
    requests, _ = load_workload(inputs / 'workload.json')
    document = binding_plan_document(requests, config.topology, config.binding)
    if document['bindings_sha256'] != audited['bindings_sha256']:
        raise ValueError('binding plan changed after comparison preflight')
    path = inputs / 'binding_plan.json'
    write_json(path, document)
    return str(path)


def skipped_run(task):
    return dict(task.result_row('skipped'), wall_seconds=0.,
                error='not attempted after an earlier failure (--fail-fast)')


def failed_run(task, error, row=None):
    """Also handles process/serialization failures observed by the parent."""
    row = task.result_row('failed') if row is None else row
    row.update(status='failed', error=f'{type(error).__name__}: {error}')
    try:
        task.case_dir.mkdir(parents=True, exist_ok=True)
        write_json(task.case_dir / 'error.json', dict(row, traceback=traceback.format_exc()))
    except OSError as record_error:
        row['error_record_error'] = f'{type(record_error).__name__}: {record_error}'
    row.setdefault('wall_seconds', None)
    return row


def execute_run(task):
    """Spawn-safe entry point, also used directly by the serial runner."""
    row = dict(task.result_row('failed'), worker_pid=os.getpid())
    began = time.monotonic()
    case_dir = task.case_dir
    inputs = Path(task.output) / 'inputs'
    try:
        case_dir.mkdir(parents=True)
        raw = copy.deepcopy(task.base_config)
        raw['feedback']['compute_profile_path'] = str(inputs / 'compute_profile.json')
        generator = copy.deepcopy(task.generator_spec)
        for key, value in row['parameters'].items():
            if key.startswith('workload.'):
                set_override(generator, key[len('workload.'):], value)
            else:
                set_override(raw, key, value)
        raw.setdefault('runtime', {})['seed'] = row['seed']
        if generator is not None:
            write_workload(generator, case_dir / 'workload.json', row['workload_seed'])
        else:
            (case_dir / 'workload.json').write_bytes((inputs / 'workload.json').read_bytes())
        raw['runtime']['workload_path'] = str(case_dir / 'workload.json')
        write_json(case_dir / 'config.json', raw)
        config = load_config(case_dir / 'config.json')
        binding = read_json(task.binding_plan_path) if task.binding_plan_path else None
        from ..backend.runner import run_baseline
        result = run_baseline(config, case_dir / 'run', make_plots=task.make_plots,
                              source_snapshot=task.source_snapshot, binding_plan=binding)
        identity = read_json(case_dir / 'run/reproducibility.json')
        row['source_tree_sha256'] = identity['source']['source_tree_sha256']
        row['backend_fingerprint'] = identity['backend']['fingerprint']
        row.update(extract_metrics(result.summary), status='completed')
        binding_summary = result.summary.get('binding', {})
        row['binding_candidate_scores'] = binding_summary.get('candidate_scores', 0)
        row['binding_budget_exhaustions'] = binding_summary.get('budget_exhaustions', 0)
        timing_path = case_dir / 'run/observation/binding_timing.json'
        if timing_path.is_file():
            timing = read_json(timing_path)
            for q in ('p50', 'p95', 'p99'):
                row['binding_round_' + q + '_us'] = timing['round_us'][q + '_us']
            row['binding_inclusive_p99_us'] = timing['inclusive_us']['p99_us']
        for name in ('workload', 'compute_profile'):
            key = 'profile' if name == 'compute_profile' else name
            row[key + '_sha256'] = hashlib.sha256((case_dir / 'run' / (name + '.json')).read_bytes()).hexdigest()
        if 'static_binding' in result.summary:
            row['binding_plan_sha256'] = result.summary['static_binding']['bindings_sha256']
        row['measurement'] = result.summary['measurement']
        row['effective_baseline'] = result.summary['run']['metadata']['effective_baseline']
        row['synthetic'] = result.summary['run']['metadata']['synthetic']
    except Exception as error:
        row = failed_run(task, error, row)
    row['wall_seconds'] = time.monotonic() - began
    return row
