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

    @staticmethod
    def _signature(merged):
        return tuple(sorted((key, size, tuple(sorted(refs))) for key, (size, refs) in merged.items()))

    def _merge(self, objects):
        merged = {}
        for item in objects:
            if item.memory_domain_id not in self.capacities or type(item.total_size_bytes) is not int or item.total_size_bytes < 0:
                raise ValueError("unknown domain or invalid reservation size")
            if not item.object_id or not item.request_ids:
                raise ValueError("objects require an ID and at least one reference")
            key = (item.memory_domain_id, item.object_id)
            size, refs = merged.get(key, (0, set()))
            merged[key] = (max(size, item.total_size_bytes), refs | set(item.request_ids))
        return merged

    def try_commit(self, plan_id, objects):
        return self.try_commit_batch(((plan_id, objects),))

    def try_commit_batch(self, plans):
        """Preflight every plan before mutation; failed batches leave no refs.

        No callbacks run during publication on the single simulation clock.
        Existing physical objects, occupancy and read pins remain intact.
        """
        pending, combined = {}, {}
        for plan_id, objects in plans:
            merged = self._merge(objects)
            signature = self._signature(merged)
            old = pending.get(plan_id, self.plans.get(plan_id))
            if old is not None:
                if old != signature:
                    raise ValueError("plan ID was reused for a different reservation")
                continue
            pending[plan_id] = signature
            for key, (size, refs) in merged.items():
                previous, references = combined.get(key, (0, set()))
                combined[key] = (max(previous, size), references | refs)
        proposed = {domain: 0 for domain in self.capacities}
        for (domain, _), obj in self.objects.items():
            proposed[domain] += obj.total
        for key, (size, _) in combined.items():
            old = self.objects.get(key)
            proposed[key[0]] += max(0, size - (old.total if old else 0))
        if any(proposed[domain] > limit for domain, limit in self.capacities.items()):
            return False
        for key, (size, refs) in combined.items():
            obj = self.objects.setdefault(key, _Object(size))
            obj.total = max(obj.total, size)
            obj.refs.update(refs)
        self.plans.update(pending)
        return True

    def clone(self):
        """Detached snapshot without copying historical plan signatures."""
        result = CapacityLedger(self.capacities)
        result.objects = {key: _Object(o.total, o.occupied, set(o.refs), o.pins) for key, o in self.objects.items()}
        return result

    def materialize(self, memory_domain_id, object_id, size_bytes):
        obj = self.objects[(memory_domain_id, object_id)]
        if type(size_bytes) is not int or size_bytes < 0 or obj.occupied + size_bytes > obj.total:
            raise ValueError("materialization exceeds reserved bytes")
        obj.occupied += size_bytes

    def can_commit(self, objects):
        additions = {}
        for item in objects:
            key = (item.memory_domain_id, item.object_id)
            additions[key] = max(additions.get(key, 0), item.total_size_bytes)
        available = {domain: row["available_bytes"] for domain, row in self.snapshot().items()}
        for (domain, identity), size in additions.items():
            old = self.objects.get((domain, identity))
            available[domain] -= max(0, size - (old.total if old else 0))
        return all(value >= 0 for value in available.values())

    def materialized(self, domain, identity):
        obj = self.objects.get((domain, identity))
        return obj.occupied if obj is not None else None

    def evictable(self, domain, identity, cache_reference):
        obj = self.objects.get((domain, identity))
        return obj is not None and obj.refs == {cache_reference} and obj.pins == 0

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
