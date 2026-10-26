"""Atomic physical-object capacity accounting with reference and read pins."""

from dataclasses import dataclass, field


@dataclass
class _Object:
    total: int
    occupied: int = 0
    refs: set = field(default_factory=set)
    pins: int = 0


class CapacityLedger:
    def __init__(self, capacities):
        if not capacities or any(type(value) is not int or value < 0 for value in capacities.values()):
            raise ValueError("capacities must be nonnegative integer byte counts")
        self.capacities = dict(capacities)
        self.objects = {}
        self.plans = {}

    def try_commit(self, plan_id, objects):
        merged = {}
        for item in objects:
            if item.memory_domain_id not in self.capacities or type(item.total_size_bytes) is not int or item.total_size_bytes < 0:
                raise ValueError("unknown domain or invalid reservation size")
            if not item.object_id or not item.request_ids:
                raise ValueError("objects require an ID and at least one reference")
            key = (item.memory_domain_id, item.object_id)
            size, refs = merged.get(key, (0, set()))
            merged[key] = (max(size, item.total_size_bytes), refs | set(item.request_ids))
        signature = tuple(sorted((key, size, tuple(sorted(refs))) for key, (size, refs) in merged.items()))
        if plan_id in self.plans:
            if self.plans[plan_id] != signature:
                raise ValueError("plan ID was reused for a different reservation")
            return True
        proposed = {domain: 0 for domain in self.capacities}
        for (domain, _), obj in self.objects.items():
            proposed[domain] += obj.total
        for key, (size, _) in merged.items():
            old = self.objects.get(key)
            proposed[key[0]] += max(0, size - (old.total if old else 0))
        if any(proposed[domain] > limit for domain, limit in self.capacities.items()):
            return False
        for key, (size, refs) in merged.items():
            obj = self.objects.setdefault(key, _Object(size))
            obj.total = max(obj.total, size)
            obj.refs.update(refs)
        self.plans[plan_id] = signature
        return True

    def materialize(self, memory_domain_id, object_id, size_bytes):
        obj = self.objects[(memory_domain_id, object_id)]
        if type(size_bytes) is not int or size_bytes < 0 or obj.occupied + size_bytes > obj.total:
            raise ValueError("materialization exceeds reserved bytes")
        obj.occupied += size_bytes

    def pin(self, memory_domain_id, object_id):
        self.objects[(memory_domain_id, object_id)].pins += 1

    def unpin(self, memory_domain_id, object_id):
        key = (memory_domain_id, object_id)
        obj = self.objects[key]
        if obj.pins == 0:
            raise ValueError("unbalanced read pin release")
        obj.pins -= 1
        self._collect(key)

    def _collect(self, key):
        obj = self.objects.get(key)
        if obj is not None and not obj.refs and obj.pins == 0:
            del self.objects[key]

    def release_reference(self, memory_domain_id, object_id, request_id):
        key = (memory_domain_id, object_id)
        if key in self.objects:
            self.objects[key].refs.discard(request_id)
            self._collect(key)

    def release_request(self, request_id):
        for key in list(self.objects):
            self.objects[key].refs.discard(request_id)
            self._collect(key)

    def snapshot(self):
        result = {domain: {"capacity_bytes": capacity, "occupied_bytes": 0, "reserved_bytes": 0}
                  for domain, capacity in self.capacities.items()}
        for (domain, _), obj in self.objects.items():
            result[domain]["occupied_bytes"] += obj.occupied
            result[domain]["reserved_bytes"] += obj.total - obj.occupied
        for domain, row in result.items():
            row["available_bytes"] = row["capacity_bytes"] - row["occupied_bytes"] - row["reserved_bytes"]
        return result
