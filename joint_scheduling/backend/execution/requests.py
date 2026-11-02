"""Request input is immutable; execution progress has named owners.

The mapping API is the migration boundary for existing binding/EP/batching
algorithms. New code can inspect the named progress sections directly.
"""
import copy
from collections.abc import MutableMapping
from types import MappingProxyType


class RequestRecord(MutableMapping):
    def __init__(self, specification, **progress):
        self.specification = MappingProxyType(copy.deepcopy(dict(specification)))
        self.lifecycle, self.prefill, self.decode, self.kv, self.binding, self.memory, self.execution = ({} for _ in range(7))
        self._keys = list(self.specification)
        self.update(progress)

    def _section(self, key):
        if key in ('segment_index', 'compute_segment_end'):
            return self.execution
        if key.startswith(('prefill_', 'cached_prefix')) or key == 'ready_layers':
            return self.prefill
        if key.startswith(('kv_', 'pending_kv', 'source_released')):
            return self.kv
        if key.startswith('decode_') or key in ('generated', 'token_times', 'first_token_at_seconds'):
            return self.decode
        if key.startswith(('binding_', 'bound_')) or key in ('p', 'd'):
            return self.binding
        if key in ('prefix_plan', 'pre_reserved'):
            return self.memory
        return self.lifecycle

    def __getitem__(self, key):
        if key in self.specification:
            return self.specification[key]
        return self._section(key)[key]

    def __setitem__(self, key, value):
        if key in self.specification:
            raise TypeError('request input is immutable: '+key)
        section = self._section(key)
        if key not in section:
            self._keys.append(key)
        section[key] = value

    def __delitem__(self, key):
        if key in self.specification:
            raise TypeError('request input is immutable: '+key)
        del self._section(key)[key]
        self._keys.remove(key)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def __deepcopy__(self, memo):
        # Predictors receive an independent legacy snapshot, never live state.
        snapshot = {}
        memo[id(self)] = snapshot
        for key in self:
            snapshot[key] = copy.deepcopy(self[key], memo)
        return snapshot
