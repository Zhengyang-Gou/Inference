"""Physical inventory, logical instance placement and topology inspection."""

from dataclasses import asdict, dataclass, field, replace
from typing import Tuple

from ..validation import ConfigError, integer, read_settings, require_mapping


@dataclass(frozen=True)
class InstancePlacement:
    instance_id: str
    role: str
    gpu_ids: Tuple[int, ...]


@dataclass(frozen=True)
class TopologyConfig:
    num_leaves: int = field(default=2, metadata={"min": 1})
    num_spines: int = field(default=2, metadata={"min": 1})
    nodes_per_leaf: int = field(default=4, metadata={"min": 1})
    gpus_per_node: int = field(default=2, metadata={"min": 1})
    gpu_memory_gib: float = field(default=80.0, metadata={"gt": 0})
    nic_bandwidth_gbps: float = field(default=200.0, metadata={"gt": 0})
    uplink_bandwidth_gbps: float = field(default=400.0, metadata={"gt": 0})
    link_latency_us: float = field(default=1.0, metadata={"min": 0})
    intra_node_bandwidth_gbps: float = field(default=1600.0, metadata={"gt": 0})
    intra_node_latency_us: float = field(default=0.5, metadata={"min": 0})
    routing: str = field(default="ecmp", metadata={"choices": ("ecmp",)})
    placements: Tuple[InstancePlacement, ...] = field(default=(), metadata={"internal": True})

    @property
    def num_nodes(self):
        return self.num_leaves * self.nodes_per_leaf

    @property
    def num_gpus(self):
        return self.num_nodes * self.gpus_per_node


def parse_config(raw):
    raw = require_mapping(raw, "topology")
    placements = raw.pop("placements", None)
    config = read_settings(TopologyConfig, raw, "topology")
    if not isinstance(placements, list) or not placements:
        raise ConfigError("topology.placements must be a non-empty list")
    result, ids, allocated_gpus = [], set(), set()
    for index, entry in enumerate(placements):
        path = "topology.placements[{}]".format(index)
        entry = require_mapping(entry, path)
        if set(entry) != {"instance_id", "role", "gpu_ids"}:
            raise ConfigError("{} requires only instance_id, role and gpu_ids".format(path))
        instance_id, role, gpu_ids = entry["instance_id"], entry["role"], entry["gpu_ids"]
        if not isinstance(instance_id, str) or not instance_id.strip() or instance_id in ids:
            raise ConfigError("{}.instance_id must be a non-empty unique string".format(path))
        if role not in ("prefill", "decode"):
            raise ConfigError("{}.role must be prefill or decode".format(path))
        if not isinstance(gpu_ids, list) or not gpu_ids:
            raise ConfigError("{}.gpu_ids must be a non-empty list".format(path))
        for gpu_id in gpu_ids:
            integer(gpu_id, path + ".gpu_ids")
            if gpu_id >= config.num_gpus:
                raise ConfigError("{}: GPU {} is outside the configured topology".format(path, gpu_id))
            if gpu_id in allocated_gpus:
                raise ConfigError("GPU {} is assigned more than once".format(gpu_id))
            allocated_gpus.add(gpu_id)
        ids.add(instance_id)
        result.append(InstancePlacement(instance_id, role, tuple(gpu_ids)))
    if {instance.role for instance in result} != {"prefill", "decode"}:
        raise ConfigError("topology.placements must include both prefill and decode instances")
    return replace(config, placements=tuple(result))


def build_manifest(config):
    """Export an inspection graph, NOT a runnable SimAI/ns-3 topology file.

    This initial model has one shared full-duplex NIC per server and one leaf
    per rack. GPU-internal communication still needs a backend-specific model.
    """
    nodes, gpus, links = [], [], []
    switches = [{"id": "leaf-{}".format(i), "kind": "leaf"} for i in range(config.num_leaves)]
    switches += [{"id": "spine-{}".format(i), "kind": "spine"} for i in range(config.num_spines)]
    for node_id in range(config.num_nodes):
        leaf_id = node_id // config.nodes_per_leaf
        gpu_ids = list(range(node_id * config.gpus_per_node, (node_id + 1) * config.gpus_per_node))
        nodes.append({"id": "node-{}".format(node_id), "node_id": node_id,
                      "leaf_id": leaf_id, "rack_id": leaf_id,
                      "gpu_ids": gpu_ids, "nic_id": "nic-{}".format(node_id)})
        for gpu_id in gpu_ids:
            gpus.append({"id": "gpu-{}".format(gpu_id), "gpu_id": gpu_id, "node_id": node_id,
                         "memory_domain_id": "gpu-{}".format(gpu_id),
                         "physical_capacity_bytes": int(config.gpu_memory_gib * 2**30)})
        links.append({"src": "node-{}".format(node_id), "dst": "leaf-{}".format(leaf_id),
                      "bandwidth_bytes_per_second": config.nic_bandwidth_gbps * 1e9 / 8,
                      "latency_seconds": config.link_latency_us * 1e-6, "duplex": "full"})
    for leaf_id in range(config.num_leaves):
        for spine_id in range(config.num_spines):
            links.append({"src": "leaf-{}".format(leaf_id), "dst": "spine-{}".format(spine_id),
                          "bandwidth_bytes_per_second": config.uplink_bandwidth_gbps * 1e9 / 8,
                          "latency_seconds": config.link_latency_us * 1e-6, "duplex": "full"})
    return {"format": "joint-scheduling-topology-inspection-v1", "backend_ready": False,
            "routing": config.routing, "switches": switches, "nodes": nodes, "gpus": gpus,
            "links": links, "placements": [asdict(p) for p in config.placements],
            "assumptions": ["one_leaf_per_rack", "one_shared_full_duplex_nic_per_server",
                            "gpu_internal_fabric_not_exported"]}


def export_backend_topology(config, path):
    """Write SimAI's six-field topology syntax, aggregating GPUs at server NICs.

    Backend endpoints are physical servers (GPUsPerServer=1 at the network
    layer). The adapter separately maps GPU IDs to those server endpoints.
    Intra-node GPU traffic uses the explicitly configured aggregate local link.
    """
    from pathlib import Path
    if config.num_nodes > 60000 or config.num_spines + config.nodes_per_leaf >= 1024 or config.num_leaves >= 1024:
        raise ConfigError("topology exceeds the pinned ns-3 endpoint/port limits")
    graph = build_manifest(config)
    switch_ids = {}
    for index, switch in enumerate(graph["switches"], config.num_nodes):
        switch_ids[switch["id"]] = index
    def endpoint(name):
        return int(name.split("-")[1]) if name.startswith("node-") else switch_ids[name]
    lines = ["{} 1 0 {} {} NONE".format(config.num_nodes + len(switch_ids), len(switch_ids), len(graph["links"])),
             " ".join(str(value) for value in switch_ids.values())]
    for link in graph["links"]:
        rate = round(link["bandwidth_bytes_per_second"] * 8)
        latency = round(link["latency_seconds"] * 1e9)
        if rate < 1:
            raise ConfigError("backend link bandwidth must be at least 1 bit/second")
        lines.append("{} {} {}bps {}ns 0".format(endpoint(link["src"]), endpoint(link["dst"]), rate, latency))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path
