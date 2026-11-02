"""Explicit state ownership and a narrow compatibility port for algorithms."""
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class RuntimeHooks:
    # Observational only: no runtime/clock handle is exposed to either callback.
    event: Optional[Callable] = None  # (name, time_ns, copied_details)
    network_sample: Optional[Callable] = None  # (copied_snapshot)


class RuntimeComponent:
    owned_fields = frozenset()

    def __init__(self, runtime):
        object.__setattr__(self, '_runtime', runtime)

    def __getattr__(self, name):
        if name in self.owned_fields:
            raise AttributeError('component state not initialized: '+name)
        return getattr(self._runtime, name)

    def __setattr__(self, name, value):
        if name in self.owned_fields:
            object.__setattr__(self, name, value)
        elif name in self._runtime._field_owners:
            setattr(self._runtime, name, value)
        else:
            raise AttributeError('undeclared component state: '+name)


def install_components(runtime):
    from .adapters import BindingController, ExpertRuntime, InstanceState
    from .lifecycle import RequestLifecycle
    from .prefill import PrefillRuntime
    from .decode import DecodeRuntime
    from .kv import KVRuntime
    from .compute import ComputeExecutor
    from .network import NetworkExecutor
    from .memory import MemoryRuntime
    from .budgets import BudgetEstimator
    from .telemetry import RuntimeTelemetry
    import inspect

    components = dict(lifecycle=RequestLifecycle, instances=InstanceState,
        prefill=PrefillRuntime, decode=DecodeRuntime, kv=KVRuntime,
        compute=ComputeExecutor, network=NetworkExecutor, memory=MemoryRuntime,
        budgets=BudgetEstimator, telemetry=RuntimeTelemetry,
        binding=BindingController, expert=ExpertRuntime)
    fields, methods, installed = {}, {}, {}
    for label, cls in components.items():
        component = cls(runtime)
        installed[label] = component
        for name in cls.owned_fields:
            if name in fields:
                raise ValueError('duplicate runtime state owner: '+name)
            fields[name] = component
        for name, function in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith('__'):
                continue
            if name in methods:
                raise ValueError('duplicate runtime method owner: '+name)
            methods[name] = component
    object.__setattr__(runtime, 'components', installed)
    object.__setattr__(runtime, '_field_owners', fields)
    object.__setattr__(runtime, '_method_owners', methods)
