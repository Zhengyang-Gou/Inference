"""Per-request prefix/suffix objects and byte-exact layer/rank layout."""

import math
from dataclasses import replace

from .cache import prefix_chain
from ..kv_scheduler import split_layer_kv
from ..reservations import PhysicalObjectReservation


class PrefixPlan:
    def __init__(self, cache, row, prefill, decode, profile, now_seconds):
        self.cache, self.row, self.prefill, self.decode = cache, row, prefill, decode
        self.profile = profile
        self.blocks = row.get("prefix_blocks", [])
        self.keys = prefix_chain(self.blocks, cache.namespace)
        self.hits = {
            "p": cache.lookup(prefill.instance_id, self.blocks, now_seconds)
                 if cache.config.prefill_reuse_enabled else (),
            "d": cache.lookup(decode.instance_id, self.blocks, now_seconds)
                 if cache.config.decode_reuse_enabled and row["output_tokens"] > 1 else ()}
        counts = [b["tokens"] for b in self.blocks]
        suffix = row["input_tokens"] - sum(counts)
        if suffix:
            counts.append(suffix)
        self.counts = counts
        self.layer_bytes = [s["kv_bytes_per_token"] for s in row["prefill_steps"]]
        self.objects, self.by_role = [], {"p": [], "d": []}
        for role, placement in (("p", prefill), ("d", decode)):
            if role == "d" and row["output_tokens"] == 1:
                continue
            for index, tokens in enumerate(counts):
                if index < len(self.hits[role]):
                    objects = tuple(replace(o, request_ids=(row["request_id"],))
                                    for o in self.hits[role][index].objects)
                else:
                    identity = "{}:{}:segment:{}".format(role, row["request_id"], index)
                    objects = tuple(PhysicalObjectReservation(identity, "gpu-{}".format(gpu),
                        sum(self.shards(tokens * size)[rank] for size in self.layer_bytes), (row["request_id"],))
                        for rank, gpu in enumerate(placement.gpu_ids))
                self.by_role[role].append(objects)
                self.objects.extend(objects)
        self.growth_id = "d:{}:growth".format(row["request_id"])
        if row["output_tokens"] > 1:
            growth = math.ceil(row["output_tokens"] * profile.kv_bytes_per_token / len(decode.gpu_ids))
            self.objects.extend(PhysicalObjectReservation(self.growth_id, "gpu-{}".format(gpu), growth,
                                                         (row["request_id"],)) for gpu in decode.gpu_ids)

    def shards(self, size):
        base, extra = divmod(size, len(self.prefill.gpu_ids))
        return tuple(base + int(rank < extra) for rank in range(len(self.prefill.gpu_ids)))

    def hit_tokens(self, role):
        return sum(self.counts[:len(self.hits[role])])

    def commit(self):
        row = self.row
        for role in ("p", "d"):
            if role == "d" and row["output_tokens"] == 1:
                continue
            self.cache.used(self.hits[role], self.hit_tokens(role))
        self.cache.stats["reused_prefill_tokens"] += self.hit_tokens("p")
        self.cache.stats["reused_decode_tokens"] += self.hit_tokens("d")
        self.cache.stats["saved_kv_transfer_bytes"] += self.hit_tokens("d") * self.profile.kv_bytes_per_token
        row["kv_rank_bytes"] = [sum(objects[rank].total_size_bytes for objects in self.by_role["p"])
                                for rank in range(len(self.prefill.gpu_ids))]
        row["kv_received_by_gpu"] = {gpu: sum(objects[rank].total_size_bytes
            for objects in self.by_role["d"][:len(self.hits["d"])]) for rank, gpu in enumerate(self.decode.gpu_ids)}
        row["kv_received_bytes"] = sum(row["kv_received_by_gpu"].values())
        row["kv_unsent_bytes"] = (row["input_tokens"] * self.profile.kv_bytes_per_token - row["kv_received_bytes"]
                                  if row["output_tokens"] > 1 else 0)

    def ranges(self, start_token=0, end_token=None):
        """Intersect a query interval with independently sharded prefix objects."""
        end_token = self.row["input_tokens"] if end_token is None else end_token
        if not 0 <= start_token <= end_token <= self.row["input_tokens"]:
            raise ValueError("invalid prefix token interval")
        cursor = 0
        for segment, tokens in enumerate(self.counts):
            left, right = max(start_token, cursor), min(end_token, cursor+tokens)
            if left < right:
                yield segment, left-cursor, right-left
            cursor += tokens

    def materialize_layer(self, index, start_token=0, end_token=None):
        for segment, offset, tokens in self.ranges(start_token, end_token):
            if segment < len(self.hits["p"]):
                continue
            before = self.shards(offset*self.layer_bytes[index])
            after = self.shards((offset+tokens)*self.layer_bytes[index])
            for obj, left, right in zip(self.by_role["p"][segment], before, after):
                self.cache.ledger.materialize(obj.memory_domain_id, obj.object_id, right-left)

    def transfer_blocks(self, index, max_block_bytes, now_seconds, cached, start_token=0, end_token=None):
        if self.row["output_tokens"] == 1:
            return ()
        result = []
        for segment, offset, tokens in self.ranges(start_token, end_token):
            if segment < len(self.hits["d"]) or (segment < len(self.hits["p"])) != cached:
                continue
            blocks = split_layer_kv(self.row["request_id"]+":segment:{}".format(segment), index,
                self.prefill.gpu_ids, self.decode.gpu_ids, tokens * self.layer_bytes[index],
                max_block_bytes, now_seconds, start_bytes=offset*self.layer_bytes[index])
            for block in blocks:
                rank = self.prefill.gpu_ids.index(block.source_gpu_id)
                result.append(replace(block, request_id=self.row["request_id"],
                    source_object_id=self.by_role["p"][segment][rank].object_id,
                    destination_object_id=self.by_role["d"][segment][rank].object_id))
        return tuple(result)

    def whole_transfers(self):
        for segment, objects in enumerate(self.by_role["p"]):
            if segment < len(self.hits["d"]):
                continue
            for rank, obj in enumerate(objects):
                if obj.total_size_bytes:
                    yield (self.prefill.gpu_ids[rank], self.decode.gpu_ids[rank], obj.total_size_bytes,
                           obj.object_id, self.by_role["d"][segment][rank].object_id)

    def release_source(self):
        for objects in self.by_role["p"]:
            for obj in objects:
                self.cache.ledger.release_reference(obj.memory_domain_id, obj.object_id, self.row["request_id"])

    def publish(self, role, now_seconds):
        placement = self.prefill if role == "p" else self.decode
        for index, key in enumerate(self.keys):
            if index >= len(self.hits[role]):
                self.cache.publish(placement.instance_id, key, self.by_role[role][index], now_seconds)
