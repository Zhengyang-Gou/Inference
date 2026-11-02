"""Existing algorithms composed through the explicit runtime port.

These adapters retain the original algorithms; they own their private state.
Cross-component calls use RuntimeComponent instead of a shared inheritance tree.
"""
from .component import RuntimeComponent
from ..expert_parallel import ExpertParallelMixin
from ...binding.runtime import BindingRuntime

class ExpertRuntime(ExpertParallelMixin, RuntimeComponent):
    owned_fields = frozenset(['ep_calls', 'ep_counter', 'ep_enabled', 'ep_groups', 'ep_lane_active', 'ep_lanes', 'ep_stats'])

class BindingController(BindingRuntime, RuntimeComponent):
    owned_fields = frozenset(['binding_active', 'binding_commit_sequence', 'binding_cursor', 'binding_known', 'binding_model', 'binding_phase', 'binding_rounds', 'binding_snapshot_ns', 'binding_snapshot_wall', 'binding_wall', 'binding_window', 'fast_predictor', 'joint_enabled', 'joint_policy', 'prediction_call_count', 'prediction_history', 'prediction_records', 'prediction_visible', 'predictor'])

class InstanceState(RuntimeComponent):
    owned_fields = frozenset(['active_by_instance', 'busy', 'decodes', 'instances', 'prefills', 'queues'])
