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
    nic_model: str = field(default="shared_node", metadata={"choices": ("shared_node", "per_gpu")})
    uplink_bandwidth_gbps: float = field(default=400.0, metadata={"gt": 0})
    link_latency_us: float = field(default=1.0, metadata={"min": 0})
    intra_node_bandwidth_gbps: float = field(default=1600.0, metadata={"gt": 0})
    intra_node_model: str = field(default="shared_fifo", metadata={"choices": ("shared_fifo", "gpu_ports")})
    intra_node_latency_us: float = field(default=0.5, metadata={"min": 0})
    routing: str = field(default="ecmp", metadata={"choices": ("ecmp",)})
    placements: Tuple[InstancePlacement, ...] = field(default=(), metadata={"internal": True})

    @property
    def num_nodes(self):
        return self.num_leaves * self.nodes_per_leaf

    @property
    def num_gpus(self):
        return self.num_nodes * self.gpus_per_node

    @property
    def network_endpoint_count(self):
        return self.num_gpus if self.nic_model == 'per_gpu' else self.num_nodes

    @property
    def endpoint_bandwidth_gbps(self):
        # nic_bandwidth_gbps always denotes the total per physical node.
        return self.nic_bandwidth_gbps / (self.gpus_per_node if self.nic_model == 'per_gpu' else 1)

    def network_endpoint(self, gpu):
        return gpu if self.nic_model == 'per_gpu' else gpu // self.gpus_per_node


def local_resources(config):
    """Known local capacities, also usable before a port has carried traffic."""
    if getattr(config, 'intra_node_model', 'shared_fifo') == 'gpu_ports':
        entries = ((f'local:gpu-{gpu}:{direction}', gpu // config.gpus_per_node)
                   for gpu in range(config.num_gpus) for direction in ('tx', 'rx'))
    else:
        entries = ((f'local:{host}', host) for host in range(config.num_nodes))
    return {key: dict(resource_id=key, node_id=host, peer_id=host,
        capacity_bps=round(config.intra_node_bandwidth_gbps * 1e9), tx_bytes=0,
        kv_bytes=0, collective_bytes=0, other_bytes=0, queue_bytes=0, pfc_pause_count=0)
        for key, host in entries}


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

    The configured NIC model is either one shared endpoint per server or one
    endpoint per GPU. Local fabric is modeled separately by the adapter.
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
        if config.nic_model == 'per_gpu':
            nodes[-1].pop('nic_id')
            nodes[-1]['nic_ids'] = [f'gpu-nic-{gpu}' for gpu in gpu_ids]
            for gpu in gpus[-len(gpu_ids):]:
                gpu['nic_id'] = f"gpu-nic-{gpu['gpu_id']}"
        endpoints = ["gpu-{}".format(gpu) for gpu in gpu_ids] if config.nic_model == 'per_gpu' else ["node-{}".format(node_id)]
        for endpoint in endpoints:
            links.append({"src": endpoint, "dst": "leaf-{}".format(leaf_id),
                          "bandwidth_bytes_per_second": config.endpoint_bandwidth_gbps * 1e9 / 8,
                          "latency_seconds": config.link_latency_us * 1e-6, "duplex": "full"})
    for leaf_id in range(config.num_leaves):
        for spine_id in range(config.num_spines):
            links.append({"src": "leaf-{}".format(leaf_id), "dst": "spine-{}".format(spine_id),
                          "bandwidth_bytes_per_second": config.uplink_bandwidth_gbps * 1e9 / 8,
                          "latency_seconds": config.link_latency_us * 1e-6, "duplex": "full"})
    return {"format": "joint-scheduling-topology-inspection-v1", "backend_ready": False,
            "routing": config.routing, "switches": switches, "nodes": nodes, "gpus": gpus,
            "links": links, "placements": [asdict(p) for p in config.placements],
            "network_endpoint_count": config.network_endpoint_count,
            "assumptions": ["one_leaf_per_rack",
                            "one_full_duplex_nic_per_gpu" if config.nic_model == 'per_gpu' else "one_shared_full_duplex_nic_per_server",
                            "local_gpu_tx_rx_ports" if config.intra_node_model == 'gpu_ports' else "local_shared_fifo",
                            "gpu_internal_fabric_not_exported"]}


def export_backend_topology(config, path):
    """Write SimAI's topology with server or GPU NIC endpoints as configured.

    Endpoint IDs precede switch IDs. Local GPU traffic bypasses the RDMA graph
    and uses the configured FIFO or GPU-port fabric abstraction.
    """
    from pathlib import Path
    downlinks = config.nodes_per_leaf * (config.gpus_per_node if config.nic_model == 'per_gpu' else 1)
    if config.network_endpoint_count > 60000 or config.num_spines + downlinks >= 1024 or config.num_leaves >= 1024:
        raise ConfigError("topology exceeds the pinned ns-3 endpoint/port limits")
    graph = build_manifest(config)
    switch_ids = {}
    for index, switch in enumerate(graph["switches"], config.network_endpoint_count):
        switch_ids[switch["id"]] = index
    def endpoint(name):
        return int(name.split("-")[1]) if name.startswith(("node-", "gpu-")) else switch_ids[name]
    lines = ["{} 1 0 {} {} NONE".format(config.network_endpoint_count + len(switch_ids), len(switch_ids), len(graph["links"])),
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
