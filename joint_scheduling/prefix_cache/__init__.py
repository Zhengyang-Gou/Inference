"""Metadata-only prefix reuse on the simulator's physical memory ledger."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrefixCacheConfig:
    enabled: bool = False
    prefill_reuse_enabled: bool = True
    decode_reuse_enabled: bool = True
    max_bytes_per_instance: int = field(default=67108864, metadata={"min": 1})
    eviction_enabled: bool = True


from .cache import PrefixCache, prefix_chain

__all__ = ["PrefixCacheConfig", "PrefixCache", "prefix_chain"]
