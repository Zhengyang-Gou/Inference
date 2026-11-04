"""Read-only EP event auditing through existing runtime hooks."""
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .ep_reference import audit_layout, audit_routing_constraint, payload_totals, stage_flows, token_reference


class EPFlowAudit:
    """Verify routing, each submitted flow, ACK dependencies and per-call ordering.

    State is retained only for current compute steps and active EP calls. No
    extra sampling or events are introduced. Cut-off calls stay incomplete.
    """
    def __init__(self, config, profile):
        self.config, self.profile = config, profile
        self.placements = {p.instance_id: p for p in config.topology.placements}
        self.groups = {source: group for group in config.expert_parallel.groups for source in group}
        self.steps, self.calls, self.transfers = {}, {}, {}
        self.started = self.completed = self.checked_flows = 0
        self.last_ns = 0
        self.totals = {phase: dict(dispatch=Counter(), combine=Counter(), calls=0, assignments=0)
                       for phase in ('prefill', 'decode')}

    def _require(self, condition, message):
        if not condition:
            raise ValueError('EP traffic audit: ' + message)

    def event(self, event, time_ns, data):
        self._require(time_ns >= self.last_ns, 'event time moved backwards')
        self.last_ns = time_ns
        if event == 'compute_step_started':
            source, phase = data['instance_id'], data['phase']
            self._require(source not in self.steps, 'overlapping source compute steps')
            if phase == 'decode':
                self._require('context_tokens' in data, 'missing Decode token cursors')
                spans = [(1, cursor) for cursor in data['context_tokens']]
            elif 'scheduled_tokens' in data:
                spans = [(n, data['cached_tokens'][rid]) for rid, n in data['scheduled_tokens'].items()]
            else:
                spans = [(data['tokens'], data.get('cached_tokens', 0))]
            reference = token_reference(self.profile.expert, self.groups[source], self.placements,
                source, self.config.topology.gpus_per_node, spans)
            layers = (list(range(self.profile.expert.raw['decode_layers'])) if phase == 'decode' else
                      [data['layer_id']] if data.get('layer_id') in self.profile.expert.raw['prefill_layer_ids'] else [])
            self.steps[source] = dict(reference=reference, layers=layers, next_layer=0, active_call=None)
        elif event == 'compute_step_completed':
            step = self.steps.pop(data['instance_id'])
            self._require(step['active_call'] is None and step['next_layer'] == len(step['layers']),
                          'compute step completed before all declared MoE layers')
        elif event == 'ep_call_started':
            cid, source, phase = data['ep_call_id'], data['source_instance_id'], data['phase']
            self._require(cid not in self.calls and source in self.steps, 'unknown or duplicate EP call')
            step = self.steps[source]
            self._require(step['active_call'] is None and step['next_layer'] < len(step['layers']), 'overlapping or excess MoE calls')
            self._require(data['layer_id'] == step['layers'][step['next_layer']], 'MoE layer order mismatch')
            self._require(self.placements[source].role == phase, 'EP phase differs from source role')
            step['next_layer'] += 1
            step['active_call'] = cid
            reference = step['reference']
            self._require(data['expert_assignments'] == reference['assignments'], 'token cursor/assignment mismatch')
            group = self.groups[source]
            per_rank = self.profile.expert.raw['num_experts'] // len(group)
            experts = {target: sum(reference['assignments'][i*per_rank:(i+1)*per_rank])
                       for i, target in enumerate(group)}
            self.calls[cid] = dict(reference=reference, phase=phase, source=source, stages={},
                experts={target: n for target, n in experts.items() if n}, expert_started=set(), expert_ended=set())
            self.started += 1
            totals = payload_totals(reference, self.profile.expert.raw, self.config.topology.gpus_per_node)
            self.totals[phase]['calls'] += 1
            self.totals[phase]['assignments'] += sum(reference['assignments'])
            for stage in totals:
                self.totals[phase][stage].update(totals[stage])
        elif data.get('collective_type') in ('ep_dispatch', 'ep_combine'):
            call = self.calls[data['ep_call_id']]
            stage = data['collective_type'][3:]
            if event == 'collective_started':
                self._require(stage not in call['stages'], 'duplicate EP communication stage')
                if stage == 'combine':
                    self._require(call['expert_ended'] == set(call['experts']), 'combine before all experts complete')
                expected = stage_flows(call['reference'], self.profile.expert.raw[stage+'_element_bytes'], stage=='combine')
                self._require(data['size_bytes'] == sum(s*n for (_, _, s), n in expected.items()), 'stage byte total mismatch')
                self._require(data['flow_count'] == sum(expected.values()), 'stage flow count mismatch')
                call['stages'][stage] = dict(expected=expected, pending=set(), started_ns=time_ns, completed=False)
            elif event == 'transfer_started':
                row = call['stages'][stage]
                key = (data['source_gpu_id'], data['destination_gpu_id'], data['size_bytes'])
                self._require(row['expected'][key] > 0, 'unexpected/duplicate flow payload or endpoints')
                self._require(time_ns == row['started_ns'], 'flow submitted at a different simulated time')
                self._require(data['transfer_id'] not in self.transfers, 'duplicate active transfer ID')
                row['expected'][key] -= 1
                row['pending'].add(data['transfer_id'])
                self.transfers[data['transfer_id']] = row
                self.checked_flows += 1
            elif event == 'transfer_completed':
                row = call['stages'][stage]
                self._require(self.transfers.pop(data['transfer_id'], None) is row, 'unknown flow completion')
                self._require(data['source_reads_complete'] and data['destination_ready'], 'completion without sender ACK readiness')
                row['pending'].remove(data['transfer_id'])
            elif event == 'collective_completed':
                row = call['stages'][stage]
                self._require(not row['completed'] and not +row['expected'] and not row['pending'],
                              'stage completed before every expected flow ACK')
                row['completed'] = True
        elif event == 'compute_segment_started' and data.get('task_kind') == 'expert':
            call = self.calls[data['ep_call_id']]
            target = data['instance_id']
            self._require(call['stages']['dispatch']['completed'], 'expert compute before dispatch ACK')
            self._require(target in call['experts'] and target not in call['expert_started'], 'unexpected expert destination')
            self._require(data['assignments'] == call['experts'][target], 'expert work differs from routed assignments')
            call['expert_started'].add(target)
        elif event == 'ep_expert_completed':
            call = self.calls[data['ep_call_id']]
            target = data['instance_id']
            self._require(target in call['expert_started'] and target not in call['expert_ended'], 'unexpected expert completion')
            self._require(data['assignments'] == call['experts'][target], 'expert completion assignment mismatch')
            call['expert_ended'].add(target)
        elif event == 'ep_call_completed':
            call = self.calls[data['ep_call_id']]
            self._require(call['stages'].get('combine', {}).get('completed'), 'EP call completed before combine ACK')
            self.steps[call['source']]['active_call'] = None
            del self.calls[data['ep_call_id']]
            self.completed += 1

    def finish(self, summary):
        self._require(summary['started_calls'] == self.started, 'summary started call count mismatch')
        self._require(summary['completed_calls'] == self.completed, 'summary completed call count mismatch')
        self._require(summary['routed_assignments'] == sum(p['assignments'] for p in self.totals.values()),
                      'summary assignment mismatch')
        self._require(set(summary['active_calls_at_end']) == set(self.calls), 'summary active call IDs mismatch')
        for call in self.calls.values():
            for row in call['stages'].values():
                self._require(not +row['expected'], 'cut-off stage omitted a declared flow submission')
        for stage in ('dispatch', 'combine'):
            # The summary includes a stage only once it has actually started;
            # a cut-off may leave a call between dispatch and combine.
            excluded = [c for c in self.calls.values() if stage not in c['stages']]
            totals = payload_totals_sum(self.totals, stage)
            for call in excluded:
                row = payload_totals(call['reference'], self.profile.expert.raw, self.config.topology.gpus_per_node)[stage]
                totals.subtract(row)
            self._require(summary[stage+'_bytes'] == totals['direct_bytes'], 'summary '+stage+' payload mismatch')
            self._require(summary['inter_node_'+stage+'_bytes'] == totals['direct_inter_node_bytes'],
                          'summary '+stage+' remote payload mismatch')
        return dict(status='passed',started_calls=self.started,completed_calls=self.completed,
            incomplete_calls=len(self.calls),checked_logical_flows=self.checked_flows,
            active_logical_flows=len(self.transfers),active_compute_steps=len(self.steps),
            phase_started_call_payloads=self.totals,
            scope='current direct-assignment model; incomplete calls are not counted as completed')


def payload_totals_sum(phases, stage):
    total = Counter()
    for row in phases.values():
        total.update(row[stage])
    return total


def run_ep_audit(config, output, simulate=False, *, expert_groups=None, max_groups_per_token=None):
    from ..backend.compute_profile import ComputeProfile
    profile = ComputeProfile(config.profile_path())
    report = dict(schema_version=1,layout=audit_layout(config, profile),routing_constraint=None,simulation=None)
    if expert_groups is not None or max_groups_per_token is not None:
        report['routing_constraint'] = audit_routing_constraint(profile.expert, expert_groups, max_groups_per_token)
    if simulate and config.prefix_cache.enabled:
        raise ValueError('EP live audit currently requires prefix_cache.enabled=false to observe exact token cursors')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output/'audit_config.json').write_text(json.dumps(asdict(config),default=str,indent=2)+'\n')
    (output/'compute_profile.json').write_text(json.dumps(profile.raw,indent=2)+'\n')
    if simulate:
        from ..backend.execution import RuntimeHooks
        from ..backend.runner import run_baseline
        probe = EPFlowAudit(config, profile)
        run_baseline(config, output/'run', hooks=RuntimeHooks(event=probe.event))
        summary = json.loads((output/'run/expert_parallel_summary.json').read_text())
        report['simulation'] = probe.finish(summary)
    (output/'audit_summary.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    return report
