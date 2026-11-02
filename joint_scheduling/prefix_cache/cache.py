"""Ready prefix entries, local references, and non-disruptive LRU eviction.

Keys describe an ordered chain of opaque workload block IDs, scoped to a
compute-profile namespace. No future requests or completion times are read.
"""

import hashlib
import json
from dataclasses import dataclass

from ..reservations import PhysicalObjectReservation


def prefix_chain(blocks, namespace):
    parent = namespace
    result = []
    for block in blocks:
        parent = hashlib.sha256(json.dumps([parent, block["id"], block["tokens"]],
                                          ensure_ascii=False).encode()).hexdigest()
        result.append(parent)
    return tuple(result)


@dataclass
class Entry:
    instance_id: str
    key: str
    objects: tuple
    cache_reference: str
    ready_at_seconds: float
    last_used: int

    @property
    def size_bytes(self):
        return sum(obj.total_size_bytes for obj in self.objects)


class PrefixCache:
    def __init__(self, config, ledger, namespace, emit=None):
        self.config, self.ledger, self.namespace = config, ledger, namespace
        self.emit = emit or (lambda *args, **kwargs: None)
        self.entries, self.sequence = {}, 0
        self.stats = dict(lookups=0, hits=0, hit_tokens=0, insertions=0, evictions=0,
                          skipped_insertions=0, reused_prefill_tokens=0,
                          reused_decode_tokens=0, saved_kv_transfer_bytes=0)

    def lookup(self, instance_id, blocks, now_seconds):
        """Pure query for predictors; usage/LRU changes happen only on commit."""
        result = []
        for key in prefix_chain(blocks, self.namespace):
            entry = self.entries.get((instance_id, key))
            if entry is None or entry.ready_at_seconds > now_seconds:
                break
            result.append(entry)
        return tuple(result)

    def used(self, entries, tokens):
        self.stats["lookups"] += 1
        self.stats["hits"] += bool(entries)
        self.stats["hit_tokens"] += tokens
        for entry in entries:
            self.sequence += 1
            entry.last_used = self.sequence

    def _idle(self, entry):
        return all(self.ledger.evictable(obj.memory_domain_id, obj.object_id, entry.cache_reference)
                   for obj in entry.objects)

    def _evict(self, entry):
        for obj in entry.objects:
            self.ledger.release_reference(obj.memory_domain_id, obj.object_id, entry.cache_reference)
        del self.entries[entry.instance_id, entry.key]
        self.stats["evictions"] += 1
        self.emit("prefix_cache_evicted", instance_id=entry.instance_id,
                  prefix_key=entry.key, size_bytes=entry.size_bytes)

    def evict_for(self, objects):
        """Reclaim idle cache only, protecting every object in the trial plan."""
        protected = {(o.memory_domain_id, o.object_id) for o in objects}
        candidates = sorted(self.entries.values(), key=lambda e: (e.last_used, e.instance_id, e.key))
        for entry in candidates:
            if self.ledger.can_commit(objects):
                return True
            if (self.config.eviction_enabled and self._idle(entry)
                    and not any((o.memory_domain_id, o.object_id) in protected for o in entry.objects)):
                self._evict(entry)
        return self.ledger.can_commit(objects)

    def publish(self, instance_id, key, objects, now_seconds):
        """Publish only fully materialized, usable KV, never an in-progress hit."""
        if (instance_id, key) in self.entries:
            return False
        if not all(self.ledger.materialized(o.memory_domain_id, o.object_id) == o.total_size_bytes
                   for o in objects):
            raise ValueError("cannot cache a prefix before every layer/rank is ready")
        size = sum(o.total_size_bytes for o in objects)
        if size > self.config.max_bytes_per_instance:
            self.stats["skipped_insertions"] += 1
            return False
        def used_bytes():
            return sum(e.size_bytes for e in self.entries.values() if e.instance_id == instance_id)
        for entry in sorted(list(self.entries.values()), key=lambda e: (e.last_used, e.key)):
            if used_bytes() + size <= self.config.max_bytes_per_instance:
                break
            if entry.instance_id == instance_id and self.config.eviction_enabled and self._idle(entry):
                self._evict(entry)
        if used_bytes() + size > self.config.max_bytes_per_instance:
            self.stats["skipped_insertions"] += 1
            return False
        self.sequence += 1
        reference = "\x00prefix-cache:{}:{}:{}".format(instance_id, key, self.sequence)
        cached = tuple(PhysicalObjectReservation(o.object_id, o.memory_domain_id,
                                                o.total_size_bytes, (reference,)) for o in objects)
        if not self.ledger.try_commit(reference, cached):
            raise RuntimeError("adding a cache reference unexpectedly required capacity")
        self.entries[instance_id, key] = Entry(instance_id, key, cached, reference, now_seconds, self.sequence)
        self.stats["insertions"] += 1
        self.emit("prefix_cache_inserted", instance_id=instance_id, prefix_key=key, size_bytes=size)
        return True

    def snapshot(self):
        entries = [dict(instance_id=e.instance_id, prefix_key=e.key, size_bytes=e.size_bytes,
                        ready_at_seconds=e.ready_at_seconds, idle=self._idle(e),
                        objects=[dict(memory_domain_id=o.memory_domain_id, object_id=o.object_id,
                                      size_bytes=o.total_size_bytes) for o in e.objects])
                   for e in sorted(self.entries.values(), key=lambda e: (e.instance_id, e.key))]
        return dict(self.stats, resident_bytes=sum(e["size_bytes"] for e in entries), entries=entries)
