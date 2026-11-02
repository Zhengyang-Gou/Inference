"""Compose the event coordinator without runtime algorithm multiple inheritance."""
import hashlib
from collections import deque

from ..runtime import KV_TRANSFER_MODES, CONTROLLED_KV_MODES
from ...reservations.ledger import CapacityLedger
from ...prefix_cache import PrefixCache
from ...kv_scheduler.adaptive import AdaptiveKVPolicy, NetworkResources
from ...kv_scheduler.mlu import MLUKVPolicy
from ...observation.network import NetworkObserver, StaticNetworkObserver


from .component import RuntimeHooks, install_components
from .requests import RequestRecord


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

    def __init__(self, config, backend, recorder, profile, requests, observer, hooks=None):
        self._install_components()
        self.hooks = hooks or RuntimeHooks()
        self.config, self.backend, self.recorder, self.profile = config, backend, recorder, profile
        self.observer = observer
        self.kv_mode = config.runtime.kv_transfer_mode
        self.mlu_enabled = config.kv_scheduler.policy == 'slo_mlu'
        self.slo_budget_cache = {}
        if self.kv_mode not in KV_TRANSFER_MODES:
            raise ValueError("unknown KV transfer mode")
        if self.kv_mode.startswith("layer_ready_") and not profile.layered:
            raise ValueError("layer-ready modes require an explicit schema 2 layer profile")
        self.active_by_instance, self.collective_demands = {}, {}
        self.collective_counter = 0
        self.active_decode_batches = {}
        self.init_prefill_batching()
        self.prefill_chunk_started_count = 0
        self.prefill_chunk_completed_count = 0
        self.prefill_chunk_query_tokens = 0
        self.decode_batch_counter = 0
        self.decode_batch_completed = 0
        self.decode_batch_histogram = {}
        self.network_monitor, self.scheduler = None, None
        self.dispatch_generation, self.dispatch_at = 0, None
        self.compute_active = set()
        self.request_counts = {"arrived_count": 0, "completed_count": 0, "failed_count": 0, "incomplete_count": 0}
        self.instances = {placement.instance_id: placement for placement in config.topology.placements}
        self.init_expert_parallel()
        self.prefills = [p.instance_id for p in config.topology.placements if p.role == "prefill"]
        self.decodes = [p.instance_id for p in config.topology.placements if p.role == "decode"]
        self.queues = {key: deque() for key in self.instances}
        self.busy = {key: False for key in self.instances}
        capacity = int(config.topology.gpu_memory_gib * 2**30 * (1 - config.reservations.capacity_safety_fraction)) - profile.weights_bytes_per_gpu
        if capacity < 0:
            raise ValueError("model weights exceed usable GPU capacity")
        self.ledger = CapacityLedger({"gpu-{}".format(gpu): capacity for gpu in range(config.topology.num_gpus)})
        self.prefix_cache = (PrefixCache(config.prefix_cache, self.ledger,
            hashlib.sha256(profile.path.read_bytes()).hexdigest(), self.emit) if config.prefix_cache.enabled else None)
        self.requests = {}
        self.transfer_counter = 0
        for index, item in enumerate(requests):
            row = RequestRecord(item, p=self.prefills[index % len(self.prefills)], d=self.decodes[index % len(self.decodes)],
                       generated=0, state="not_arrived")
            p, d = self.instances[row["p"]], self.instances[row["d"]]
            if len(p.gpu_ids) != len(d.gpu_ids):
                raise ValueError("this baseline requires equal P/D TP sizes; KV resharding is not implemented")
            row["prefill_steps"] = profile.prefill_steps(row["input_tokens"], len(p.gpu_ids))
            row["prefill_chunks"] = (profile.prefill_chunks(row["input_tokens"], len(p.gpu_ids), config.runtime.prefill_chunk_tokens)
                                     if config.runtime.prefill_chunk_tokens and not self.prefill_batching else ())
            row["prefill_chunk_index"] = 0
            row["prefill_computed_tokens"] = 0
            # Shard each layer independently. Summed per-layer rounding can differ
            # from rounding the whole request once, so reserve the actual layout.
            row["kv_rank_bytes"] = [0] * len(p.gpu_ids)
            for step in row["prefill_steps"]:
                base, extra = divmod(row["input_tokens"] * step["kv_bytes_per_token"], len(p.gpu_ids))
                for rank in range(len(p.gpu_ids)):
                    row["kv_rank_bytes"][rank] += base + int(rank < extra)
            row.update(prefill_finished=False, kv_received_bytes=0, pending_kv_blocks=[], decode_enqueued=False,
                       kv_unsent_bytes=row["input_tokens"] * profile.kv_bytes_per_token if row["output_tokens"] > 1 else 0,
                       source_released=False, kv_received_by_gpu={gpu: 0 for gpu in d.gpu_ids}, kv_active_transfers={},
                       token_times=[], ready_layers=0)
            if row["output_tokens"] > 1:
                profile.lookup("decode", row["input_tokens"], len(d.gpu_ids))
                profile.lookup("decode", row["input_tokens"] + row["output_tokens"] - 2, len(d.gpu_ids))
            self.requests[row["request_id"]] = row
            if config.binding.window_enabled:
                row["p"], row["d"] = None, None
        if config.feedback.network_observation_enabled or self.kv_mode in CONTROLLED_KV_MODES:
            self.network_monitor = (NetworkObserver(backend, config.topology, config.feedback, observer.output,
                                                      record_samples=config.recording.detailed, sample_sink=self.hooks.network_sample)
                                    if config.feedback.network_observation_enabled else StaticNetworkObserver(backend, config.topology))
            if self.kv_mode in CONTROLLED_KV_MODES:
                resources = NetworkResources(config.topology, self.network_monitor.initial["resources"])
                policy_class = MLUKVPolicy if self.mlu_enabled else AdaptiveKVPolicy
                self.scheduler = policy_class(config.kv_scheduler, resources,
                    "slo" if self.kv_mode == "layer_ready_slo" and config.kv_scheduler.slo_priority_enabled else "fifo",
                    decision_delay_seconds=config.feedback.scheduling_overhead_us/1e6,
                    **(dict(observation_delay_seconds=config.feedback.observation_delay_us/1e6) if self.mlu_enabled else {}))
            if config.feedback.network_observation_enabled:
                self.schedule_network_sample()
        self.init_binding()
