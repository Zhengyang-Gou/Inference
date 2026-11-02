"""MLU decisions and priority enforcement at real NIC/switch bottlenecks."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from joint_scheduling.backend.build import PACKAGE, BackendError
from joint_scheduling.backend.client import Ns3Backend
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config
from joint_scheduling.kv_scheduler import KVBlock, KVConfig
from joint_scheduling.kv_scheduler.adaptive import NetworkResources, RequestBudget
from joint_scheduling.kv_scheduler.mlu import MLUKVPolicy
from joint_scheduling.backend.slo import MLUBudgetMixin


class MLUAdmissionTests(unittest.TestCase):
    def setUp(self):
        topology=replace(load_config(PACKAGE/'configs/ep_mlu_smoke.json').topology,
            num_leaves=1,nodes_per_leaf=2,gpus_per_node=1)
        self.ports={f'{s}:{d}':dict(node_id=s,peer_id=d,capacity_bps=8000,queue_bytes=0,
            utilization=0,other_bytes_per_second=0,collective_bytes_per_second=0)
            for s,d in ((0,2),(2,0),(1,2),(2,1))}
        self.resources=NetworkResources(topology,self.ports)
        self.keys=self.resources.for_gpus(0,1)
        self.network=dict(sequence=0,time_seconds=0,visible_at_seconds=0,resources=self.ports,flows=[])
        self.config=KVConfig(policy='slo_mlu',max_block_bytes=100,max_inflight_bytes=300,
            max_concurrent_transfers=3,max_resource_inflight_bytes=300,safety_margin_fraction=0,
            inflight_budget_mode='fixed',mlu_deadline_guard_us=0,collective_guard_us=0)
        self.policy=MLUKVPolicy(self.config,self.resources)
        self.policy.refresh(0,self.network,[])

    def budget(self,deadline,size=100,**kwargs):
        return RequestBudget(0,1,0,deadline,size,0,0,0,True,
            remaining_resource_bytes={k:size for k in self.keys},**kwargs)

    def test_configuration_rejects_inert_or_conflicting_mlu_switches(self):
        raw=json.loads((PACKAGE/'configs/ep_mlu_smoke.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            for section,key,value in [('runtime','kv_transfer_mode','whole_ready_immediate'),
                ('kv_scheduler','slo_priority_enabled',False),('kv_scheduler','mlu_medium_threshold',.8),
                ('binding','prediction_enabled',True),('runtime','network_priority_enabled',1)]:
                candidate=copy.deepcopy(raw);candidate[section][key]=value
                path.write_text(json.dumps(candidate))
                with self.subTest(section=section,key=key),self.assertRaises(ValueError):load_config(path)

    def block(self,rid):
        return KVBlock(rid,rid,0,0,1,100,0,None)

    def state(self,requests,collectives=()):
        return dict(network=self.network,requests=requests,collectives=collectives)

    def test_three_kv_tiers_and_urgent_bypasses_only_soft_ep_guard(self):
        budgets={'loose':self.budget(1.1),'medium':self.budget(.6),'urgent':self.budget(.25)}
        self.policy.on_layer_ready([self.block(r) for r in budgets])
        phase=dict(resources=self.keys,resource_bytes={k:100 for k in self.keys},
            starts_at_seconds=.1,duration_seconds=.1,stage='dispatch')
        permits=self.policy.admit(.1,self.state(budgets,[phase]))
        self.assertEqual([(p.block_id,p.priority) for p in permits],[('urgent',0)])
        rows=self.policy.last_decision['mlu_requests']
        self.assertEqual([rows[r]['priority'] for r in ('loose','medium','urgent')],[3,2,0])
        self.assertEqual(self.policy.last_decision['blocked']['collective_protection'],2)
        self.policy.completed('urgent',.1)
        self.assertEqual(self.policy.admit(.1,self.state(budgets,[phase])),())
        self.assertTrue(all(sum(x)==0 for x in self.policy.class_bytes.values()))

    def test_link_max_aggregate_overload_and_monotone_promotion(self):
        self.ports[self.keys[-1]]['capacity_bps']=4000
        self.resources.capacities[self.keys[-1]]=500
        requests={'a':self.budget(.5,size=180),'b':self.budget(.5,size=180)}
        self.policy.on_layer_ready([self.block('a'),self.block('b')])
        self.policy.admit(.1,self.state(requests))
        row=self.policy.last_decision['mlu_requests']['a']
        self.assertAlmostEqual(row['mlu'],.9)
        self.assertTrue(self.policy.last_decision['overloaded_resources'])
        self.assertTrue(row['feasible'])
        # Lower remaining demand cannot demote pending messages.
        requests={r:self.budget(2,size=10) for r in requests}
        self.policy.admit(.11,self.state(requests))
        self.assertEqual(self.policy.last_decision['mlu_requests']['b']['priority'],0)
        self.policy.request_completed('b')
        self.assertNotIn('b',self.policy.levels)

    def test_expired_or_unproduced_work_best_effort_and_finite_audit(self):
        requests={'expired':self.budget(.01),'future':self.budget(1,production_ready_seconds=2),'live':self.budget(5)}
        self.policy.on_layer_ready([self.block(r) for r in requests])
        self.assertEqual([p.block_id for p in self.policy.admit(.2,self.state(requests))],['live'])
        self.assertIn('best_effort_quota',self.policy.last_decision['blocked'])
        row=self.policy.last_decision['mlu_requests']['expired']
        self.assertIsNone(row['mlu']);self.assertFalse(row['feasible']);self.assertEqual(row['priority'],3)
        json.dumps(self.policy.last_decision,allow_nan=False)
        self.policy.completed('live',.2);requests.pop('live')
        permits=self.policy.admit(.3,self.state(requests))
        self.assertEqual([p.block_id for p in permits],['expired','future'])
        self.assertEqual(self.policy.summary['admitted_best_effort_borrowed_bytes'],200)

    def test_compute_gap_releases_ordinary_budget_until_next_phase_guard(self):
        requests={'a':self.budget(5)}
        self.policy.on_layer_ready([self.block('a')])
        future=dict(resources=self.keys,resource_bytes={k:100 for k in self.keys},
            starts_at_seconds=2,duration_seconds=.1,stage='combine_pending')
        self.assertEqual(len(self.policy.admit(.2,self.state(requests,[future]))),1)
        self.assertTrue(all(not s['collective_reserved'] for s in self.policy.states.values()))
        self.policy.completed('a',.2)
        self.policy.on_layer_ready([self.block('b')]);requests['b']=self.budget(5)
        self.assertEqual(self.policy.admit(1.99,self.state(requests,[future])),())
        self.assertIn('collective_protection',self.policy.last_decision['blocked'])

    def test_hard_caps_and_delayed_observation_cannot_be_bypassed_by_urgent_kv(self):
        self.policy.config=replace(self.config,max_inflight_bytes=100)
        req={'a':self.budget(.5),'b':self.budget(.5)}
        self.policy.on_layer_ready([self.block(r) for r in req])
        permits=self.policy.admit(.3,self.state(req))
        self.assertEqual(len(permits),1)
        self.assertIn('global_inflight_limit',self.policy.last_decision['blocked'])
        with self.assertRaisesRegex(ValueError,'visible'):
            self.policy.refresh(.4,dict(self.network,visible_at_seconds=.5),[])
        with self.assertRaisesRegex(ValueError,'backwards'):
            self.policy.refresh(.2,self.network,[])

    def test_handoff_anchor_shared_expert_wait_and_eos_independence(self):
        from collections import deque
        from types import SimpleNamespace
        fake=MLUBudgetMixin();fake.ep_enabled=True;fake.slo_budget_cache={}
        fake.backend=SimpleNamespace(now_seconds=lambda:.1)
        fake.config=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        fake.ep_groups={'d':('d','dx'),'p':('p','px')}
        fake.ep_lane_active={'dx':dict(compute_end_seconds=.3,tp_seconds=.01)}
        fake.ep_lanes={i:deque() for i in ('p','px','d','dx')}
        fake.ep_lanes['dx'].append(('peer',.1,'expert',None,None,dict(tp_seconds=.01),0))
        fake.active_decode_batches={};fake.queues={'d':deque()}
        fake.mlu_decode_cost=lambda instance,rows:.02
        row=dict(request_id='r',p='p',d='d',prefill_finished=True,first_token_at_seconds=.1,
                 input_tokens=8,generated=1,itl_slo_seconds=.5,output_tokens=3)
        fake.requests={'r':row}
        base=RequestBudget(0,1,.1,.5,100,.05,0,0,True)
        first=fake.mlu_budget(row,base)
        self.assertAlmostEqual(first.expert_queue_wait_seconds,.32)
        self.assertAlmostEqual(first.latest_kv_delivery_seconds,.31)
        row['output_tokens']=999
        self.assertEqual(fake.mlu_budget(row,base),first)
        fake.ep_lane_active={};fake.ep_lanes['dx'].clear();fake.slo_budget_cache={}
        free=fake.mlu_budget(row,base)
        self.assertAlmostEqual(free.latest_kv_delivery_seconds,.58)
        fake.backend.now_seconds=lambda:.2
        self.assertAlmostEqual(fake.mlu_budget(row,base).latest_kv_delivery_seconds,.58)


@unittest.skipUnless(backend_available(),'build native backend first')
class MLUNetworkTests(unittest.TestCase):
    def test_native_priority_preempts_long_flow_at_nic_and_at_switch(self):
        config=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        topo=replace(config.topology,num_leaves=1,num_spines=1,nodes_per_leaf=3,gpus_per_node=1,
                     nic_bandwidth_gbps=10,uplink_bandwidth_gbps=10)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for shared_source in (True,False):
                durations={}
                for enabled in (False,True):
                    with Ns3Backend(topo,root/f'{shared_source}-{enabled}',congestion_control='none',network_priority_enabled=enabled) as backend:
                        done={}
                        backend.send_hosts(0,2,8*1024*1024,lambda:done.update(low=backend.now_seconds()),priority=3)
                        backend.call_at(.0002,lambda:backend.send_hosts(0 if shared_source else 1,2,2*1024*1024,
                            lambda:done.update(high=backend.now_seconds()),priority=0))
                        backend.run_until(.05)
                        self.assertEqual(set(done),{'low','high'})
                        durations[enabled]=done
                        snapshot=backend.network_snapshot()
                        if enabled:
                            self.assertTrue(any(r['data_priority_bytes'][0]>0 and r['data_priority_bytes'][3]>0
                                for r in snapshot['resources']))
                        with self.assertRaises(BackendError):
                            backend.send_hosts(0,2,100,lambda:None,priority=4)
                self.assertLess(durations[True]['high'],durations[False]['high']*.8)
                self.assertLess(durations[True]['high'],durations[True]['low'])

    def test_ep_mlu_lifetimes_levels_phase_windows_and_recording_equivalence(self):
        config=load_config(PACKAGE/'configs/ep_mlu_smoke.json')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);captured=[]
            from unittest.mock import patch
            from joint_scheduling.backend.expert_parallel import ExpertParallelMixin
            original=ExpertParallelMixin.ep_phase_demand
            def inspect(runtime,cid,stage,flows,starts):
                original(runtime,cid,stage,flows,starts)
                captured.append(dict(runtime.collective_demands[cid],now_seconds=runtime.backend.now_seconds()))
            with patch.object(ExpertParallelMixin,'ep_phase_demand',inspect):
                result=run_baseline(config,root/'debug')
            self.assertEqual(len(result.tokens),24)
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            self.assertTrue(any(d.get('stage')=='combine_pending' and d['starts_at_seconds']>d['now_seconds'] for d in captured))
            self.assertTrue(any(d.get('stage')=='dispatch' for d in captured))
            self.assertTrue(any(d.get('stage')=='dispatch_pending' and d['starts_at_seconds']>d['now_seconds'] for d in captured))
            log=[json.loads(x) for x in (root/'debug/events.jsonl').read_text().splitlines()]
            decisions=[e for e in log if e['event']=='kv_scheduling_decision']
            self.assertTrue(all('mlu_requests' in e for e in decisions))
            self.assertTrue(any(r['decode_step_seconds']>0 for e in decisions for r in e['mlu_requests'].values()))
            resources=json.loads((root/'debug/final_resources.json').read_text())
            self.assertEqual(sum(r['occupied_bytes']+r['reserved_bytes'] for r in resources.values()),0)
            summary=json.loads((root/'debug/observation/kv_scheduler_summary.json').read_text())
            self.assertEqual(summary['pending_blocks']+summary['inflight_blocks']+summary['decode_owners_remaining'],0)
            self.assertEqual(summary['tracked_mlu_requests'],0)
            again=run_baseline(replace(config,recording=replace(config.recording,mode='essential')),root/'essential')
            self.assertEqual(result.tokens,again.tokens)
            self.assertEqual(summary,json.loads((root/'essential/observation/kv_scheduler_summary.json').read_text()))

    def test_prefix_hits_single_chunks_decode_admission_and_cutoff(self):
        from joint_scheduling.backend.tests import test_composed_execution as helpers
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            config=helpers.ComposedExecutionTests().config(root,packed=False,joint=False)
            # Reuse the established odd-prefix lifecycle with a cross-node EP
            # group, adding one peer TP team per role.
            from joint_scheduling.topology import InstancePlacement
            profile=json.loads((PACKAGE/'backend/fixtures/ep_compute_profile.json').read_text())
            profile['kv_bytes_per_token']=8
            profile['phases']['prefill']['layers'][0]['kv_bytes_per_token']=3
            profile['phases']['prefill']['layers'][1]['kv_bytes_per_token']=5
            path=root/'ep.json';path.write_text(json.dumps(profile))
            topology=replace(config.topology,num_leaves=1,nodes_per_leaf=4,gpus_per_node=2,placements=(
                InstancePlacement('p0','prefill',(0,1)),InstancePlacement('d0','decode',(2,3)),
                InstancePlacement('p1','prefill',(4,5)),InstancePlacement('d1','decode',(6,7))))
            from joint_scheduling.backend.expert_parallel import ExpertParallelConfig
            cfg=replace(config,topology=topology,binding=replace(config.binding,prediction_enabled=False),
                expert_parallel=ExpertParallelConfig(True,(('p0','p1'),('d0','d1'))),
                feedback=replace(config.feedback,compute_profile_path=str(path)),
                runtime=replace(config.runtime,network_priority_enabled=True,kv_transfer_mode='layer_ready_slo'),
                kv_scheduler=replace(config.kv_scheduler,policy='slo_mlu',decode_admission_limit=1,slo_priority_enabled=True))
            result=run_baseline(cfg,root/'complete')
            self.assertTrue(all(r['status']=='completed' for r in result.requests))
            log=[json.loads(x) for x in (root/'complete/events.jsonl').read_text().splitlines()]
            hits=[e for e in log if e['event']=='prefix_cache_lookup']
            self.assertTrue(any(e['prefill_hit_tokens']>0 for e in hits))
            resources=json.loads((root/'complete/final_resources.json').read_text())
            self.assertEqual(sum(r['reserved_bytes'] for r in resources.values()),0)
            self.assertEqual(sum(r['occupied_bytes'] for r in resources.values()),result.summary['prefix_cache']['resident_bytes'])
            summary=json.loads((root/'complete/observation/kv_scheduler_summary.json').read_text())
            self.assertEqual(summary['tracked_mlu_requests']+summary['decode_owners_remaining'],0)
            cutoff=replace(cfg,runtime=replace(cfg.runtime,time_limit_seconds=.012),evaluation=replace(cfg.evaluation,window_end_seconds=.012))
            short=run_baseline(cutoff,root/'cutoff')
            self.assertTrue(any(r['status']=='incomplete' for r in short.requests))
            state=json.loads((root/'cutoff/final_resources.json').read_text())
            self.assertGreater(sum(r['occupied_bytes']+r['reserved_bytes'] for r in state.values()),0)
