"""Attribution, byte accounting and execution of anchor-derived MoE profiles."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.moe_profile import build_moe_profile,load_aiob_anchors,extract_public_anchors,ROLES,file_identity
from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.backend.expert_parallel import validate_profile
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.experiments.sensitivity import perturb_profile
from joint_scheduling.workload.trace import profile_coverage


MODEL=PACKAGE.parent/'third_party/vidur/data/hf_configs/deepseek_v3_config.json'


def anchors():
    observations={}
    for phase,tokens,n,x,ep in [('prefill',8192,65536,8,32),('decode',64,512,2,128)]:
        roles={r:.001 for r in ROLES}
        roles['norm']=.000009
        roles['expert']=n*1e-6+x*1e-5
        observations[phase]=dict(tp_size=1,expert_timing_tp=1,ep_size=ep,tokens=tokens,
            context_tokens=4096,expert_assignments=n,active_experts=x,roles_seconds=roles)
    return dict(model='deepseek-ai/DeepSeek-V3',gpu='NVIDIA H800',source_kind='synthetic_test_anchors',
                sources=[],observations=observations)


class MoEProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def load(self,raw,name='profile.json'):
        path=self.root/name;path.write_text(json.dumps(raw));return ComputeProfile(path)

    def test_architecture_memory_and_all_azure_length_bounds(self):
        raw,audit=build_moe_profile(MODEL,anchors())
        profile=self.load(raw)
        self.assertEqual(len(profile.phases['prefill']['layers']),61)
        self.assertEqual(profile.expert.raw['prefill_layer_ids'],list(range(3,61)))
        self.assertEqual(profile.expert.raw['top_k'],8)
        self.assertEqual(profile.kv_bytes_per_token,61*576*2*4)
        self.assertEqual(audit['kv_bytes_per_token_per_gpu'],61*576*2)
        self.assertGreater(raw['weights_bytes_per_gpu'],20*2**30)
        self.assertLess(raw['weights_bytes_per_gpu'],30*2**30)
        self.assertGreater(audit['parameter_count'],670e9)
        self.assertLess(audit['parameter_count'],672e9)
        self.assertEqual(audit['status'],'estimated_simulation_profile')
        self.assertEqual(raw['provenance']['profile_kind'],'estimated_simulation_profile')
        self.assertTrue(raw['provenance']['synthetic'])
        coverage=profile_coverage(dict(requests=[dict(request_id='long',input_tokens=14050,output_tokens=39)]),profile)
        self.assertEqual(coverage['out_of_coverage_count'],0)
        profile.validate_prefill_batch_coverage(14050,512,2048,4)
        profile.lookup_decode_batch([14087]*64,4)
        with self.assertRaisesRegex(ValueError,'outside'):
            profile.lookup_decode_batch([16385],4)
        validate_profile(load_config(PACKAGE/'configs/cluster_128gpu_tp4_ep8_mlu.json'),profile)

    def test_expert_fit_actual_distinct_experts_and_chunk_conservation(self):
        profile=self.load(build_moe_profile(MODEL,anchors())[0])
        ep=profile.expert
        self.assertAlmostEqual(ep.raw['expert_compute_seconds_per_assignment']['prefill'],.25e-6)
        self.assertAlmostEqual(ep.raw['expert_compute_seconds_per_active_expert']['decode'],2.5e-6)
        self.assertAlmostEqual(ep.compute_seconds('decode',[2,0,1]),5.75e-6)
        self.assertAlmostEqual(ep.compute_seconds('decode',[3,0,0]),3.25e-6)
        self.assertEqual(ep.compute_seconds('decode',[0]*32),0)
        total=ep.counts(14050)
        pieces=[ep.counts(min(512,14050-i),i) for i in range(0,14050,512)]
        self.assertEqual(total,[sum(p[i] for p in pieces) for i in range(256)])
        self.assertEqual(sum(total),14050*8)
        modified=perturb_profile(profile.raw,2.,.0001)
        after=self.load(modified,'after.json')
        self.assertAlmostEqual(after.expert.compute_seconds('decode',[2,0,1]),2*5.75e-6)
        self.assertEqual(after.kv_bytes_per_token,profile.kv_bytes_per_token)

    def test_tp1_asymmetric_ep_has_distinct_phase_weights_and_one_kv_copy(self):
        raw,audit=build_moe_profile(MODEL,anchors(),tp_size=1,prefill_ep_size=32,decode_ep_size=128)
        profile=self.load(raw)
        self.assertEqual(profile.kv_bytes_per_token,70272)
        self.assertEqual(raw['provenance']['expert_parallel_sizes'],{'prefill':32,'decode':128})
        self.assertGreater(profile.weights_for_phase('prefill'),profile.weights_for_phase('decode'))
        self.assertEqual(profile.weights_bytes_per_gpu,max(profile.weights_bytes_per_gpu_by_phase.values()))
        self.assertLess(profile.weights_for_phase('prefill'),76*2**30)
        self.assertEqual(audit['ep_sizes_by_phase'],{'prefill':32,'decode':128})
        validate_profile(load_config(PACKAGE/'configs/research/base_160gpu.json'),profile)
        malformed=copy.deepcopy(raw);malformed['weights_bytes_per_gpu_by_phase']['decode']=-1
        with self.assertRaises(ValueError):self.load(malformed,'invalid.json')

    def test_dense_layer_overrides_cached_attention_and_serial_overhead_once(self):
        raw=build_moe_profile(MODEL,anchors())[0]
        # Give the dense first-layer family an independently measured-like cost.
        dense=raw['phases']['prefill']['chunked']['layer_overrides'][0]['profile']
        for point in dense['linear_points']:point['compute_seconds']*=3
        p=self.load(raw)
        one=p.prefill_batch_steps([dict(chunk_tokens=512,cached_tokens=4096)],4)
        two=p.prefill_batch_steps([dict(chunk_tokens=256,cached_tokens=0),dict(chunk_tokens=256,cached_tokens=8192)],4)
        self.assertGreater(one[0]['compute_seconds'],one[3]['compute_seconds'])
        self.assertGreater(two[3]['compute_seconds'],p.prefill_batch_steps([dict(chunk_tokens=256,cached_tokens=0)]*2,4)[3]['compute_seconds'])
        changed=self.load(perturb_profile(raw,1,.0001),'overhead.json')
        for chunks in ([dict(chunk_tokens=512,cached_tokens=4096)],
                       [dict(chunk_tokens=256,cached_tokens=0),dict(chunk_tokens=256,cached_tokens=8192)]):
            before=sum(s['compute_seconds'] for s in p.prefill_batch_steps(chunks,4))
            after=sum(s['compute_seconds'] for s in changed.prefill_batch_steps(chunks,4))
            self.assertAlmostEqual(after-before,.0001)
        invalid=copy.deepcopy(raw)
        invalid['phases']['prefill']['chunked']['layer_overrides'][0]['layer_ids']=[61]
        with self.assertRaisesRegex(ValueError,'outside'):self.load(invalid,'bad.json')

    def test_aiob_units_identity_missing_stages_and_default_refusal(self):
        samples=[]
        for phase,obs in anchors()['observations'].items():
            path=self.root/(phase+'.txt')
            path.write_text('inference_term:'+phase+'\n'+''.join(r+':\n    time_gpu_avg: '+str(t*1e6)+'\n' for r,t in obs['roles_seconds'].items()))
            sample={k:v for k,v in obs.items() if k!='roles_seconds'}
            sample.update(phase=phase,path=path.name,time_unit='microseconds',roles={r:[r] for r in ROLES})
            samples.append(sample)
        spec=dict(schema_version=1,model='deepseek-ai/DeepSeek-V3',gpu='NVIDIA H800',samples=samples)
        path=self.root/'manifest.json';path.write_text(json.dumps(spec))
        imported=load_aiob_anchors(path)
        self.assertAlmostEqual(imported['observations']['decode']['roles_seconds']['expert'],.000532)
        self.assertTrue(all(len(s['sha256'])==64 for s in imported['sources']))
        samples[0]['roles']['expert']=['missing']
        path.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError,'no default'):load_aiob_anchors(path)
        samples[0]['roles']['expert']=['expert'];samples[0].pop('time_unit')
        path.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError,'explicit'):load_aiob_anchors(path)
        with self.assertRaisesRegex(ValueError,'pinned'):extract_public_anchors(path,path)

    def test_nonphysical_fit_or_mismatched_shapes_rejected(self):
        bad=anchors();bad['observations']['decode']['roles_seconds']['expert']=1.
        with self.assertRaisesRegex(ValueError,'nonnegative'):build_moe_profile(MODEL,bad)
        shape=json.loads(MODEL.read_text());shape['hidden_size']=4096
        path=self.root/'wrong_model.json';path.write_text(json.dumps(shape))
        with self.assertRaisesRegex(ValueError,'shapes'):build_moe_profile(path,anchors())

    def test_cuda_compute_excludes_network_cpu_waits_and_merges_kernel_variants(self):
        paths=[];digests={}
        for phase,ep in [('prefill',32),('decode',128)]:
            names=['void fp8_gemm_kernel<2112u, 7168u, shape_a, (GemmType)0>',
                'void fp8_gemm_kernel<2112u, 7168u, shape_a, (GemmType)0>',
                'void fp8_gemm_kernel<2112u, 7168u, shape_a, (GemmType)0>',
                'void fp8_gemm_kernel<2112u, 7168u, shape_b, (GemmType)0>',
                'void fp8_gemm_kernel<36864u, 7168u, tile, (GemmType)0>',
                'void fp8_gemm_kernel<4096u, 7168u, tile, (GemmType)0>',
                'void fp8_gemm_kernel<4096u, 7168u, tile, (GemmType)1>',
                'void flash::flash_fwd_splitkv_mla_kernel<test>',
                'void cuda::top2_sum_gate<test>','_layer_norm_kernel',
                'void dpsk::ep::internode::dispatch<test>','nccl_allreduce']
            events=[dict(name=n,cat='kernel',ph='X',ts=i,dur=100 if i==3 else 10,tid=7) for i,n in enumerate(names)]
            events[-1]['dur']=events[-2]['dur']=1e9
            events.append(dict(name='aten::wait',cat='cpu_op',ph='X',ts=0,dur=1e12))
            data=dict(deviceProperties=[dict(name='NVIDIA H800')],distributedInfo=dict(world_size=ep),traceEvents=events)
            path=self.root/(phase+'.json');path.write_text(json.dumps(data));paths.append(path)
            digests[phase]=file_identity(path)['sha256']
        with patch.dict('joint_scheduling.backend.moe_profile.PUBLIC_SHA256',digests):
            parsed=extract_public_anchors(*paths)
        for phase in ('prefill','decode'):
            obs=parsed['observations'][phase]
            self.assertAlmostEqual(obs['roles_seconds']['attention_projection'],10e-6)
            self.assertAlmostEqual(obs['roles_seconds']['norm'],30e-6)
            self.assertEqual(parsed['extraction'][phase]['excluded_communication_kernels'],2)


@unittest.skipUnless(backend_available(),'native network backend not built')
class ExpertActiveWeightsRuntimeTests(unittest.TestCase):
    def test_simulated_gpu_tasks_charge_only_the_selected_experts(self):
        cfg=load_config(PACKAGE/'configs/ep_smoke.json')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=json.loads(cfg.profile_path().read_text())
            raw['expert_parallel']['expert_compute_seconds_per_active_expert']={'prefill':.0003,'decode':.0004}
            path=root/'profile.json';path.write_text(json.dumps(raw))
            profile=ComputeProfile(path)
            output=root/'run';result=run_baseline(cfg,output,profile_path=path)
            events=[json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]
            calls={e['ep_call_id']:e for e in events if e['event']=='ep_call_started'}
            observed=0
            for e in events:
                if e['event']!='compute_segment_started' or e.get('task_kind')!='expert':continue
                call=calls[e['ep_call_id']]
                group=next(g for g in cfg.expert_parallel.groups if e['instance_id'] in g)
                rank=group.index(e['instance_id']);width=4//len(group)
                counts=call['expert_assignments'][rank*width:(rank+1)*width]
                self.assertAlmostEqual(e['modeled_compute_seconds'],profile.expert.compute_seconds(call['phase'],counts))
                observed+=1
            self.assertGreater(observed,0)
            self.assertEqual(len(result.tokens),24)
            summary=json.loads((output/'expert_parallel_summary.json').read_text())
            self.assertEqual(summary['started_calls'],summary['completed_calls'])
