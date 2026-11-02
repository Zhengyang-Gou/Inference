"""EP routing conservation, GPU exclusivity and real shared-link execution."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.expert_parallel import ExpertParallelConfig, validate_config, validate_profile
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.binding.fast import FastPlanPredictor
from joint_scheduling.binding.predictor import WholePlanPredictor
from joint_scheduling.config import load_config
from joint_scheduling.experiments.capacity import estimate_capacity
from joint_scheduling.experiments.sensitivity import perturb_profile
from joint_scheduling.topology import InstancePlacement


def events(output):
    return [json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]


class ExpertInputTests(unittest.TestCase):
    def test_periodic_top_k_counts_offsets_and_huge_prompt(self):
        profile = ComputeProfile(PACKAGE/'backend/fixtures/ep_compute_profile.json')
        expert = profile.expert
        for tokens in range(1, 25):
            for offset in range(7):
                expected = [0]*4
                for i in range(offset, offset+tokens):
                    for e in expert.raw['routing_pattern'][i % 4]:
                        expected[e] += 1
                self.assertEqual(expert.counts(tokens, offset), expected)
        self.assertEqual(expert.counts(10**12), [5*10**11]*4)
        self.assertEqual(expert.destinations(5, ('a','b')), {'a': 5, 'b': 5})
        changed = perturb_profile(profile.raw, 1.5)
        self.assertEqual(changed['expert_parallel']['expert_compute_seconds_per_assignment']['prefill'],
                         1.5*expert.raw['expert_compute_seconds_per_assignment']['prefill'])
        self.assertEqual(changed['expert_parallel']['routing_pattern'], expert.raw['routing_pattern'])

    def test_invalid_groups_profiles_and_unsupported_predictions_fail_explicitly(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        profile = ComputeProfile(config.profile_path())
        for groups in ((('p0','p0'),('d0','d1')), (('p0','d0'),('p1','d1')),
                       (('p0','p1'),), (('p0','unknown'),('d0','d1'))):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                validate_config(replace(config, expert_parallel=ExpertParallelConfig(True, groups)))
        for name in ('prediction_enabled', 'joint_selection_enabled'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'binding'):
                validate_config(replace(config, binding=replace(config.binding, **{name: True})))
        misplaced = replace(config.topology, placements=(InstancePlacement('p0','prefill',(0,4)),
            InstancePlacement('d0','decode',(2,3)), InstancePlacement('p1','prefill',(1,5)),
            InstancePlacement('d1','decode',(6,7))))
        with self.assertRaisesRegex(ValueError, 'within one node'):
            validate_config(replace(config, topology=misplaced))
        for predictor in (FastPlanPredictor, WholePlanPredictor):
            with self.assertRaisesRegex(ValueError, 'EP'):
                predictor(config, profile)
        dense = ComputeProfile(PACKAGE/'backend/fixtures/composed_compute_profile.json')
        with self.assertRaisesRegex(ValueError, 'both enable EP'):
            validate_profile(config, dense)
        with self.assertRaisesRegex(ValueError, 'both enable EP'):
            validate_profile(replace(config, expert_parallel=ExpertParallelConfig()), profile)
        with self.assertRaisesRegex(ValueError, 'does not model EP'):
            estimate_capacity(dict(requests=[dict(input_tokens=8,output_tokens=4)]), profile, config)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'profile.json'
            for field, value in [('top_k',5), ('routing_pattern',[[0,0]]), ('routing_pattern',[[0,4]]),
                                 ('prefill_layer_ids',[2]), ('decode_layers',1),
                                 ('dense_compute_excludes_experts',False),
                                 ('expert_compute_seconds_per_assignment',{'prefill':0, 'decode':1})]:
                raw = copy.deepcopy(profile.raw)
                raw['expert_parallel'][field] = value
                path.write_text(json.dumps(raw))
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    ComputeProfile(path)

    def test_128_gpu_layout_tp4_ep8_covers_all_gpus(self):
        config = load_config(PACKAGE/'configs/cluster_128gpu_tp4_ep8_8gpu_per_node.json')
        self.assertEqual(config.topology.num_gpus, 128)
        self.assertEqual(config.topology.num_nodes, 16)
        self.assertEqual(len(config.expert_parallel.groups), 4)
        placements = {p.instance_id:p for p in config.topology.placements}
        for group in config.expert_parallel.groups:
            self.assertEqual(len(group), 8)
            nodes = {g//8 for i in group for g in placements[i].gpu_ids}
            self.assertEqual(len(nodes), 4)
            for i in group:
                self.assertEqual(len(placements[i].gpu_ids), 4)
                self.assertEqual(len({g//8 for g in placements[i].gpu_ids}), 1)


@unittest.skipUnless(backend_available(), 'build native backend first')
class ExpertNetworkTests(unittest.TestCase):
    def check_lifecycle(self, result, output, config):
        log = events(output)
        self.assertTrue(all(r['status']=='completed' for r in result.requests))
        summary = json.loads((output/'expert_parallel_summary.json').read_text())
        self.assertEqual(summary['started_calls'], summary['completed_calls'])
        self.assertEqual(summary['active_calls_at_end'], {})
        self.assertEqual(summary['active_gpu_tasks_at_end'], {})
        self.assertEqual(summary['queued_gpu_tasks_at_end'], {})
        starts = {e['ep_call_id']:e for e in log if e['event']=='ep_call_started'}
        for cid in starts:
            call = [e for e in log if e.get('ep_call_id')==cid]
            dispatch = next(e for e in call if e['event']=='collective_completed' and e.get('collective_type')=='ep_dispatch')
            expert_starts = [e for e in call if e['event']=='compute_segment_started']
            self.assertTrue(expert_starts)
            self.assertTrue(all(e['time_seconds']>=dispatch['time_seconds'] for e in expert_starts))
            expert_ends = [e for e in call if e['event']=='ep_expert_completed']
            combine = next(e for e in call if e['event']=='collective_started' and e.get('collective_type')=='ep_combine')
            self.assertGreaterEqual(combine['time_seconds'], max(e['time_seconds'] for e in expert_ends))
            self.assertEqual(sum(e['assignments'] for e in expert_ends), sum(starts[cid]['expert_assignments']))
        busy, communicating, collective_instances = set(), set(), {}
        owners = {g:p.instance_id for p in config.topology.placements for g in p.gpu_ids}
        for e in log:
            if e['event']=='compute_segment_started':
                self.assertNotIn(e['instance_id'], busy)
                self.assertNotIn(e['instance_id'], communicating)
                busy.add(e['instance_id'])
            elif e['event']=='compute_segment_completed':
                busy.remove(e['instance_id'])
            elif e['event']=='collective_started' and e.get('collective_type')=='tp_allreduce':
                instance = owners[e['gpu_ids'][0]]
                self.assertNotIn(instance, busy)
                self.assertNotIn(instance, communicating)
                communicating.add(instance)
                collective_instances[e['collective_id']] = instance
            elif e['event']=='collective_completed' and e.get('collective_type')=='tp_allreduce':
                communicating.remove(collective_instances.pop(e['collective_id']))
        self.assertFalse(busy)
        self.assertFalse(communicating)
        sends = [e for e in log if e['event']=='transfer_started']
        for e in sends:
            if e.get('collective_type')=='tp_allreduce':
                self.assertEqual(e['source_gpu_id']//config.topology.gpus_per_node,
                                 e['destination_gpu_id']//config.topology.gpus_per_node)
        for stage in ('dispatch','combine'):
            total = sum(e['size_bytes'] for e in sends if e.get('collective_type')=='ep_'+stage)
            self.assertEqual(summary[stage+'_bytes'], total)
        resources = json.loads((output/'final_resources.json').read_text())
        self.assertEqual(sum(v['reserved_bytes'] for v in resources.values()), 0)
        self.assertEqual(sum(v['occupied_bytes'] for v in resources.values()), result.summary['prefix_cache']['resident_bytes'])
        return log, summary

    def test_five_kv_modes_with_packed_and_single_chunk_execution(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as folder:
            assignment_totals = None
            for packed in (False, True):
                for mode in ('whole_ready_immediate','whole_ready_chunked','layer_ready_immediate','layer_ready_fifo','layer_ready_slo'):
                    with self.subTest(packed=packed, mode=mode):
                        cfg = replace(config, runtime=replace(config.runtime, kv_transfer_mode=mode,
                            prefill_batch_token_budget=6 if packed else 0, prefill_batch_size=3 if packed else 1))
                        output = Path(folder)/f'{packed}-{mode}'
                        result = run_baseline(cfg, output)
                        log, summary = self.check_lifecycle(result, output, cfg)
                        self.assertEqual(len(result.tokens), 24)
                        self.assertEqual(summary['routed_assignments'], 264)
                        if assignment_totals is None:
                            assignment_totals = summary['expert_assignments']
                        self.assertEqual(summary['expert_assignments'], assignment_totals)
                        # New-token count, never padded Decode context length.
                        d = [e for e in log if e['event']=='ep_call_started' and e['phase']=='decode']
                        self.assertEqual(sum(sum(e['expert_assignments']) for e in d), 18*2*2)
                        self.assertGreater(summary['inter_node_dispatch_bytes'], 0)
                        if packed and mode=='layer_ready_slo':
                            network = json.loads((output/'observation/network_summary.json').read_text())
                            shared = [key for key, p in network['resources'].items()
                                      if not key.startswith('local:') and p['kv_bytes'] and p['collective_bytes']]
                            self.assertTrue(shared)
                            # Overlapping outstanding EP/KV sends on the exact same source NIC.
                            active, overlap = {}, False
                            for e in log:
                                if e['event']=='transfer_started' and e['source_gpu_id']//2 != e['destination_gpu_id']//2:
                                    host = e['source_gpu_id']//2
                                    kind = e['transfer_kind']
                                    overlap |= any(h==host and k!=kind for h,k in active.values())
                                    active[e['transfer_id']] = (host,kind)
                                elif e['event']=='transfer_completed':
                                    active.pop(e['transfer_id'],None)
                            self.assertTrue(overlap)
                            essential = replace(cfg, recording=replace(cfg.recording, mode='essential'))
                            again = run_baseline(essential, Path(folder)/'essential')
                            self.assertEqual(result.tokens, again.tokens)
                            self.assertEqual(result.summary['expert_parallel'], again.summary['expert_parallel'])

    def test_prefix_reuse_skew_and_odd_channel_shards(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = json.loads(config.profile_path().read_text())
            raw['expert_parallel'].update(hidden_size=5, routing_pattern=[[0,1]])
            for layer in raw['phases']['prefill']['layers']:
                layer['cached_points'] = [dict(cached_tokens=n, points=[dict(tokens=t,
                    compute_seconds=.00005,collective_bytes=128) for t in (n+1,64)]) for n in (5,6)]
            profile = root/'profile.json'
            profile.write_text(json.dumps(raw))
            workload = json.loads((PACKAGE/'backend/fixtures/ep_workload.json').read_text())
            for r in workload['requests']:
                r['prefix_blocks'] = [dict(id='shared',tokens=6)]
            for r in workload['requests'][4:]:
                r['input_tokens'] = 6  # Full hit still routes one final query.
            path = root/'workload.json'
            path.write_text(json.dumps(workload))
            cfg = replace(config, feedback=replace(config.feedback, compute_profile_path=str(profile)),
                runtime=replace(config.runtime, workload_path=str(path)), prefix_cache=replace(config.prefix_cache,enabled=True))
            for chunked in (False,True):
                case = replace(cfg, runtime=replace(cfg.runtime,prefill_chunk_tokens=4 if chunked else 0,
                    prefill_batch_size=3 if chunked else 1,prefill_batch_token_budget=6 if chunked else 0))
                output = root/str(chunked)
                result = run_baseline(case, output)
                log, summary = self.check_lifecycle(result,output,case)
                hits = [e for e in log if e['event']=='prefix_cache_lookup' and e['request_id'] in ('ep-4','ep-5')]
                self.assertTrue(all(e['prefill_hit_tokens']==6 for e in hits))
                later = [e for e in log if e['event']=='ep_call_started' and e['phase']=='prefill' and e['request_id'] in ('ep-4','ep-5')]
                self.assertEqual(sum(sum(e['expert_assignments']) for e in later), 2*1*2*2)
                self.assertEqual(summary['expert_assignments']['p1'], 0)
                self.assertEqual(summary['expert_assignments']['d1'], 0)
                remote = [e for e in log if e['event']=='transfer_started' and e.get('collective_type')=='ep_dispatch']
                for cid in {e['ep_call_id'] for e in remote}:
                    pair = [e['size_bytes'] for e in remote if e['ep_call_id']==cid]
                    self.assertEqual(len(pair),2)
                    self.assertEqual(pair[0]*2, pair[1]*3)  # 5 channels split 3+2.

    def test_cutoff_retains_live_dependencies_and_rejects_ep_prediction_before_output(self):
        config = load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = replace(config, runtime=replace(config.runtime,time_limit_seconds=.012),
                evaluation=replace(config.evaluation,window_end_seconds=.012))
            result = run_baseline(cfg, root/'cutoff')
            self.assertTrue(any(r['status']=='incomplete' for r in result.requests))
            summary = json.loads((root/'cutoff/expert_parallel_summary.json').read_text())
            self.assertTrue(summary['active_calls_at_end'] or summary['active_gpu_tasks_at_end'])
            resources = json.loads((root/'cutoff/final_resources.json').read_text())
            self.assertGreater(sum(v['occupied_bytes']+v['reserved_bytes'] for v in resources.values()), 0)
            invalid = replace(config,binding=replace(config.binding,prediction_enabled=True))
            with self.assertRaisesRegex(ValueError,'binding'):
                run_baseline(invalid, root/'invalid')
            self.assertFalse((root/'invalid').exists())
