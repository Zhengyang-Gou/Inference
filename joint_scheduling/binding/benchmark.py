"""Actual backend round timings for cold 1/8/16 request windows.

Run separately from regression/other experiments. Uses the simulation ns-3
runner, selected-batch resource reservations and diagnostic wall clock. Exact
simulated output is one token to keep this scheduler benchmark small, while
selection only sees max_output_tokens=16 and predicts the configured horizon.
This measures cold-state scheduling, not serving throughput or warmed queues.
"""
import argparse
import json
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path

from ..backend.runner import run_baseline
from ..config import load_config
from ..backend.runtime import ComputeProfile
from . import Binding
from .predictor import WholePlanPredictor


def quantiles(values):
    values = sorted(values)
    result = dict(samples=len(values))
    for name, q in (('p50_us', .5), ('p95_us', .95), ('p99_us', .99)):
        index = (len(values)-1)*q
        lo, hi = int(index), min(len(values)-1, int(index)+1)
        result[name] = values[lo]+(values[hi]-values[lo])*(index-lo) if values else None
    return result


def benchmark(config_path, output, repetitions=20):
    config = load_config(config_path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = replace(config,
        feedback=replace(config.feedback, network_observation_enabled=False, state_observation_enabled=False),
        binding=replace(config.binding, joint_selection_enabled=True, policy='joint_lexicographic',
                        window_enabled=True, prediction_enabled=False, decision_overhead_us=5),
        runtime=replace(config.runtime, time_limit_seconds=.02),
        evaluation=replace(config.evaluation, window_end_seconds=.02))
    rows = []
    for count in (1, 8, 16):
        workload = output/('workload-'+str(count)+'.json')
        workload.write_text(json.dumps(dict(schema_version=1, provenance=dict(synthetic=True,
            description='Cold scheduler benchmark, simulator EOS hidden from selector'),
            requests=[dict(request_id=str(i), arrival_seconds=0, input_tokens=128, output_tokens=1,
                           max_output_tokens=16, ttft_slo_seconds=1, itl_slo_seconds=1) for i in range(count)])))
        for trial in range(repetitions):
            path = output/('batch-{}-trial-{}'.format(count, trial))
            result = run_baseline(config, path, workload_path=workload)
            timing = json.loads((path/'observation/binding_timing.json').read_text())
            summary = result.summary['binding']
            first = next(json.loads(line) for line in (path/'events.jsonl').read_text().splitlines()
                         if json.loads(line)['event'] == 'binding_selection')
            rows.append(dict(batch_size=count, trial=trial, **timing['rounds'][0],
                candidate_scores=first['candidate_scores'], budget_exhaustions=int(first['budget_exhausted']),
                first_round_committed=first['committed_requests'], total_rounds=summary['rounds']))
        print('batch {}: {} real ns-3 runs complete'.format(count, repetitions), flush=True)
    full = {}
    predictor = WholePlanPredictor(config, ComputeProfile(config.profile_path()))
    for count in (1, 8, 16):
        state = predictor.empty_state()
        state['requests'] = {str(i): dict(state='pending_binding', p=None, d=None,
            input_tokens=128, max_output_tokens=16, prefix_blocks=[], arrival_seconds=0) for i in range(count)}
        plan = [Binding(str(i), next(p for p in predictor.placements if predictor.placements[p].role == 'prefill'),
                        next(d for d in predictor.placements if predictor.placements[d].role == 'decode')) for i in range(count)]
        samples = []
        for trial in range(repetitions):
            started = time.perf_counter_ns()
            predictor.predict(plan, state)
            samples.append((time.perf_counter_ns()-started)/1000)
        full[str(count)] = quantiles(samples)
    report = dict(python=sys.version, platform=platform.platform(), simulated_decision_overhead_us=5,
        profile_sha256=result.summary['run']['metadata']['compute_profile_sha256'],
        scope='cold simulation callback including state processing, pruning, scoring, atomic reservation and dispatch timer; live ns-3 backend',
        limitations='No warmed queue/cache; one actual token keeps execution small; selection sees cap 16. Startup/serving outside round timer. Run without concurrent regression.',
        target_p99_us=50, target_is_guarantee=False, rows=rows,
        whole_predictor_us=full, whole_predictor_scope='one fixed all-on-first-P/D plan, pure prediction only; not an equal-work end-to-end comparison',
        batches={str(count): dict(round_us=quantiles([r['round_us'] for r in rows if r['batch_size'] == count]),
            inclusive_us=quantiles([r['inclusive_us'] for r in rows if r['batch_size'] == count]),
            selection_us=quantiles([r['selection_us'] for r in rows if r['batch_size'] == count]),
            reservation_us=quantiles([r['reservation_us'] for r in rows if r['batch_size'] == count]),
            candidate_scores=sum(r['candidate_scores'] for r in rows if r['batch_size'] == count),
            budget_exhaustions=sum(r['budget_exhaustions'] for r in rows if r['batch_size'] == count)) for count in (1,8,16)})
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--repetitions', type=int, default=20)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error('repetitions must be positive')
    benchmark(args.config, args.output, args.repetitions)
