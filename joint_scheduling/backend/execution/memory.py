"""MemoryRuntime: explicit runtime component; scheduling order preserved."""
import hashlib
import math
from ...reservations import PhysicalObjectReservation
from ...prefix_cache.layout import PrefixPlan
from ...reservations.ledger import CapacityLedger
from ...prefix_cache import PrefixCache
from .component import RuntimeComponent


class MemoryRuntime(RuntimeComponent):
    owned_fields = frozenset(['ledger', 'prefix_cache'])

    def init_memory_state(self):
        usable = int(self.config.topology.gpu_memory_gib * 2**30 * (1 - self.config.reservations.capacity_safety_fraction))
        weights = {gpu: self.profile.weights_for_phase(p.role) for p in self.config.topology.placements for gpu in p.gpu_ids}
        capacities = {"gpu-{}".format(gpu): usable-weights.get(gpu, self.profile.weights_bytes_per_gpu)
                      for gpu in range(self.config.topology.num_gpus)}
        if min(capacities.values()) < 0:
            raise ValueError("model weights exceed usable GPU capacity")
        self.ledger = CapacityLedger(capacities)
        self.prefix_cache = (PrefixCache(self.config.prefix_cache, self.ledger,
            hashlib.sha256(self.profile.path.read_bytes()).hexdigest(), self.emit) if self.config.prefix_cache.enabled else None)

    def _objects(self, row):
        if "binding_objects" in row:
            return row["binding_objects"]
        if "prefix_plan" in row:
            return row["prefix_plan"].objects
        rid = row["request_id"]
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        growth = math.ceil(row["output_tokens"] * self.profile.kv_bytes_per_token / len(d.gpu_ids))
        objects = [PhysicalObjectReservation("p:" + rid, "gpu-{}".format(gpu), size, (rid,))
                   for gpu, size in zip(p.gpu_ids, row["kv_rank_bytes"])]
        if row["output_tokens"] > 1:
            objects += [PhysicalObjectReservation("d:" + rid, "gpu-{}".format(gpu), size + growth, (rid,))
                        for gpu, size in zip(d.gpu_ids, row["kv_rank_bytes"])]
        return objects

    def prepare_prefill(self, row):
        """Admit once; both single-chunk and packed execution use this path."""
        rid = row["request_id"]
        if self.prefix_cache is not None and not row.get("pre_reserved"):
            # Query at actual execution admission, never at workload loading.
            row["prefix_plan"] = PrefixPlan(self.prefix_cache, row, self.instances[row["p"]],
                self.instances[row["d"]], self.profile, self.backend.now_seconds())
        objects = self._objects(row)
        if self.prefix_cache is not None:
            standalone = {}
            for obj in objects:
                standalone[obj.memory_domain_id] = standalone.get(obj.memory_domain_id, 0) + obj.total_size_bytes
            if any(size > self.ledger.capacities[domain] for domain, size in standalone.items()):
                # Per-segment rank rounding may need more bytes than coalescing
                # the entire request. Report infeasibility instead of waiting.
                self.fail_request(row, "prefix layout exceeds GPU KV capacity")
                row.pop("prefix_plan", None)
                self.capture("request_failed")
                return False
        if self.prefix_cache is not None and not row.get("pre_reserved"):
            self.prefix_cache.evict_for(objects)
        if not row.get("pre_reserved") and not self.ledger.try_commit(rid, objects):
            row.pop("prefix_plan", None)
            return False
        if self.prefix_cache is not None:
            plan = row["prefix_plan"]
            plan.commit()
            cached_context = min(plan.hit_tokens("p"), row["input_tokens"]-1)
            # A full KV hit still recomputes the last query to produce logits.
            if self.config.runtime.prefill_chunk_tokens:
                row["prefill_computed_tokens"] = cached_context
                if not self.prefill_batching:
                    row["prefill_chunks"] = self.profile.prefill_chunks(row["input_tokens"],
                        len(self.instances[row["p"]].gpu_ids), self.config.runtime.prefill_chunk_tokens,
                        cached_tokens=cached_context)
            else:
                row["prefill_steps"] = self.profile.prefill_steps(row["input_tokens"],
                    len(self.instances[row["p"]].gpu_ids), cached_context)
            self.emit("prefix_cache_lookup", request_id=rid, prefill_instance_id=row["p"], decode_instance_id=row["d"],
                      prefill_hit_tokens=plan.hit_tokens("p"), decode_hit_tokens=plan.hit_tokens("d"),
                      compute_cached_tokens=cached_context, saved_kv_transfer_bytes=plan.hit_tokens("d")*self.profile.kv_bytes_per_token)
        return True

    def release_source_if_submitted(self, row):
        if row["prefill_finished"] and row["kv_unsent_bytes"] == 0 and not row["source_released"]:
            if "prefix_plan" in row:
                row["prefix_plan"].release_source()
            else:
                for gpu in self.instances[row["p"]].gpu_ids:
                    self.ledger.release_reference("gpu-{}".format(gpu), "p:" + row["request_id"], row["request_id"])
            row["source_released"] = True
