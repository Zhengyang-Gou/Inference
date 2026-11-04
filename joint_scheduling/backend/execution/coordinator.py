"""Wire runtime components in dependency order; ns-3 remains the sole clock."""
from ..settings import KV_TRANSFER_MODES
from .component import RuntimeHooks, install_components


class ExecutionCoordinator:
    def _install_components(self):
        install_components(self)

    def __getattr__(self, name):
        owners = object.__getattribute__(self, '_field_owners')
        if name in owners:
            return getattr(owners[name], name)
        methods = object.__getattribute__(self, '_method_owners')
        if name in methods:
            return getattr(methods[name], name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        owners = self.__dict__.get('_field_owners', {})
        if name in owners:
            object.__setattr__(owners[name], name, value)
        else:
            object.__setattr__(self, name, value)

    def __init__(self, config, backend, recorder, profile, requests, observer, hooks=None, bindings=None):
        self._install_components()
        self.hooks = hooks or RuntimeHooks()
        self.config, self.backend, self.recorder, self.profile = config, backend, recorder, profile
        self.observer = observer
        self.kv_mode = config.runtime.kv_transfer_mode
        self.mlu_enabled = config.kv_scheduler.policy == 'slo_mlu'
        if self.kv_mode not in KV_TRANSFER_MODES:
            raise ValueError("unknown KV transfer mode")
        if self.kv_mode.startswith("layer_ready_") and not profile.layered:
            raise ValueError("layer-ready modes require an explicit schema 2 layer profile")

        self.components['budgets'].init_budget_state()
        self.components['instances'].init_instance_state()
        self.components['network'].init_network_state()
        self.components['prefill'].init_prefill_state()
        self.components['decode'].init_decode_state()
        self.components['compute'].init_compute_state()
        self.components['expert'].init_expert_parallel()
        self.components['memory'].init_memory_state()
        self.components['lifecycle'].init_request_state(requests, bindings)
        self.components['kv'].init_kv_state()
        self.components['binding'].init_binding()
