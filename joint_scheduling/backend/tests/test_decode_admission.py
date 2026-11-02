"""Downstream slot lifetime and complete, uncensored SLO admission checks."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.kv_scheduler import KVBlock, KVConfig
from joint_scheduling.kv_scheduler.adaptive import AdaptiveKVPolicy, NetworkResources, RequestBudget
from joint_scheduling.topology import InstancePlacement


class DecodeAdmissionTests(unittest.TestCase):
    def policy(self):
        ports={k:dict(node_id=a,peer_id=b,capacity_bps=8000000,queue_bytes=0,
            utilization=0,collective_bytes_per_second=0,other_bytes_per_second=0)
            for a,b,k in ((0,1,'0:1'),(1,0,'1:0'))}
        resources=NetworkResources(SimpleNamespace(gpus_per_node=1),ports)
        policy=AdaptiveKVPolicy(KVConfig(max_block_bytes=100,max_resource_inflight_bytes=1000,
            max_inflight_bytes=10000,collective_protection_enabled=False,inflight_budget_mode='fixed',
            decode_admission_limit=1),resources)
        network=dict(sequence=1,time_seconds=0,visible_at_seconds=0,resources=ports,flows=[])
        policy.refresh(0,network,[])
        return policy,network

    def budget(self,destination):
        return RequestBudget(0,1,.01,.1,200,0,.5,.5,True,destination,1)

    def test_slot_survives_kv_ack_and_nonfinal_layers_can_overlap(self):
        p,n=self.policy();state=dict(network=n,collectives=[],requests={'a':self.budget('d0'),'b':self.budget('d0')})
        p.on_layer_ready([KVBlock('a-tail','a',1,0,1,100,0,None)])
        self.assertEqual(len(p.admit(.1,state)),1)
        p.completed('a-tail',.11)
        p.on_layer_ready([KVBlock('b-head','b',0,0,1,100,0,None),KVBlock('b-tail','b',1,0,1,100,0,None)])
        self.assertEqual([x.block_id for x in p.admit(.2,state)],['b-head'])
        self.assertEqual(p.last_decision['blocked']['decode_admission'],1)
        p.completed('b-head',.21)
        self.assertEqual(p.admit(.3,state),())
        p.request_completed('a')
        self.assertEqual([x.block_id for x in p.admit(.3,state)],['b-tail'])
        self.assertEqual(p.summary['peak_decode_owners'],{'d0':1})

    def test_reservations_are_per_destination_and_acquired_once_per_request(self):
        p,n=self.policy();state=dict(network=n,collectives=[],requests={'a':self.budget('d0'),'b':self.budget('d1'),'c':self.budget('d0')})
        p.on_layer_ready([KVBlock('a0','a',1,0,1,100,0,None),KVBlock('a1','a',1,0,1,100,0,None),
                         KVBlock('b','b',1,0,1,100,0,None),KVBlock('c','c',1,0,1,100,0,None)])
        self.assertEqual([x.block_id for x in p.admit(.1,state)],['a0','a1','b'])
        self.assertEqual(p.summary['decode_slots_acquired'],2)
        for name in ('a0','a1','b'):p.completed(name,.11)
        p.request_completed('a');p.request_completed('b')
        self.assertEqual([x.block_id for x in p.admit(.2,state)],['c'])
        p.completed('c',.21);p.request_completed('c')
        self.assertEqual(p.decode_owners,{})
        self.assertEqual(p.summary['decode_slots_acquired'],p.summary['decode_slots_released'])


@unittest.skipUnless(backend_available(),'build the native backend first')
class DecodeAdmissionNetworkTests(unittest.TestCase):
    def test_protects_existing_streams_without_dropping_or_hiding_delayed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=load_config(PACKAGE/'configs/layered_smoke.json')
            profile=dict(schema_version=2,provenance=dict(synthetic=True,description='Synthetic lifecycle test'),
                network_communication_excluded=True,model='guard-test',weights_bytes_per_gpu=0,
                kv_bytes_per_token=2048,phases=dict(
                    prefill=dict(tp_size=1,layers=[dict(kv_bytes_per_token=1024,collectives_per_step=0,
                        points=[dict(tokens=1,compute_seconds=.00005,collective_bytes=0),dict(tokens=64,compute_seconds=.00005,collective_bytes=0)]) for _ in range(2)]),
                    decode=dict(tp_size=1,collectives_per_step=0,points=[dict(tokens=1,compute_seconds=.03,collective_bytes=0),dict(tokens=64,compute_seconds=.03,collective_bytes=0)])))
            (root/'profile.json').write_text(json.dumps(profile))
            (root/'workload.json').write_text(json.dumps(dict(schema_version=1,provenance=dict(synthetic=True),requests=[
                dict(request_id=f'r{i}',arrival_seconds=0,input_tokens=4,output_tokens=5,ttft_slo_seconds=2,itl_slo_seconds=.1) for i in range(4)])))
            config=replace(base,config_path=root/'config.json',
                topology=replace(base.topology,num_leaves=1,num_spines=1,nodes_per_leaf=2,gpus_per_node=1,
                    placements=(InstancePlacement('p0','prefill',(0,)),InstancePlacement('d0','decode',(1,)))),
                feedback=replace(base.feedback,compute_profile_path=str(root/'profile.json'),network_observation_enabled=False,
                    state_observation_enabled=False,scheduling_overhead_us=0),
                runtime=replace(base.runtime,workload_path=str(root/'workload.json'),time_limit_seconds=1,kv_transfer_mode='layer_ready_slo'),
                evaluation=replace(base.evaluation,window_start_seconds=0,window_end_seconds=1,default_ttft_slo_seconds=2,default_itl_slo_seconds=.1),
                kv_scheduler=replace(base.kv_scheduler,max_block_bytes=1024,max_resource_inflight_bytes=8192,
                    collective_protection_enabled=False,inflight_budget_mode='fixed'))
            results=[]
            for limit in (0,3):
                target=root/f'limit-{limit}'
                result=run_baseline(replace(config,kv_scheduler=replace(config.kv_scheduler,decode_admission_limit=limit)),target)
                self.assertEqual(len(result.tokens),20)
                self.assertTrue(all(r['status']=='completed' for r in result.requests))
                self.assertEqual(sum(x['occupied_bytes']+x['reserved_bytes'] for x in json.loads((target/'final_resources.json').read_text()).values()),0)
                results.append(result.summary['pressure'])
            self.assertEqual(results[0]['cohort_slo_passed'],0)
            self.assertEqual(results[1]['cohort_slo_passed'],3)
            self.assertEqual(results[1]['cohort_slo_missed'],1,'the deferred handoff must count as a SLO miss')
            self.assertEqual(results[1]['cohort_slo_unknown'],0)
