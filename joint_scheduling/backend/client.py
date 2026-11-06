"""Timeout-bounded RPC to the actual ns-3 event loop, with completion callbacks."""

import math
from pathlib import Path

from .build import BackendError, LOCK, require_backend
from .rpc import LineRpcBackend


class Ns3Backend(LineRpcBackend):
    backend_name = "simai_ns3_rdma"
    log_name = "backend.log"

    def __init__(self, topology, work_dir, seed=7, congestion_control="dcqcn", packet_bytes=1000,
                 switch_buffer_bytes=16 * 1024 * 1024, rpc_timeout_seconds=60, network_priority_enabled=False,
                 *, submission_batch_size=128, native_schedule=None):
        from ..topology import export_backend_topology
        if type(seed) is not int or not 0 <= seed < 2147483647:
            raise BackendError("seed must be an integer in [0, 2147483647)")
        if congestion_control not in ("dcqcn", "none"):
            raise BackendError("supported congestion controls: dcqcn, none")
        if type(packet_bytes) is not int or not 64 <= packet_bytes <= 9000:
            raise BackendError("packet_bytes must be an integer in [64, 9000]")
        if type(switch_buffer_bytes) is not int or not 1048576 <= switch_buffer_bytes < 2**32:
            raise BackendError("switch buffer must be in [1 MiB, 4 GiB)")
        if type(rpc_timeout_seconds) not in (int, float) or not math.isfinite(rpc_timeout_seconds) or rpc_timeout_seconds <= 0:
            raise BackendError("RPC timeout must be positive and finite")
        if type(submission_batch_size) is not int or not 1 <= submission_batch_size <= 256:
            raise BackendError('submission_batch_size must be an integer in [1, 256]')
        self.state = require_backend()
        if type(network_priority_enabled) is not bool:
            raise BackendError('network_priority_enabled must be boolean')
        self.network_priority_enabled = network_priority_enabled
        self.topology = topology
        self.native_schedule = native_schedule
        self.protocol_version = LOCK["protocol_version"]
        self.submission_batch_size = submission_batch_size
        self.timeout = rpc_timeout_seconds
        self._init_state()
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        topo_file = export_backend_topology(topology, self.work_dir / "network.topo")
        self.log_path = self.work_dir / self.log_name
        self._log = self.log_path.open("wb")
        self._launch([str(self.state["executable"]), str(topo_file), str(seed + 1),
                      "1" if congestion_control == "dcqcn" else "0",
                      str(packet_bytes), str(switch_buffer_bytes), str(int(network_priority_enabled))])
        self.metadata = {"backend": "simai_ns3_rdma", "protocol_version": LOCK["protocol_version"],
                         "ns3_revision": self.state["revision"], "build_fingerprint": self.state["fingerprint"],
                         "completion_semantics": LOCK["completion_semantics"],
                         "endpoint_model": topology.nic_model, "congestion_control": congestion_control,
                         "nic_bandwidth_gbps_per_node": topology.nic_bandwidth_gbps,
                         "nic_bandwidth_gbps_per_endpoint": topology.endpoint_bandwidth_gbps,
                         "local_model": topology.intra_node_model,
                         "local_bandwidth_scope": "per_gpu_tx_rx_port" if topology.intra_node_model == 'gpu_ports' else "shared_node_fifo",
                         "packet_bytes": packet_bytes, "switch_buffer_bytes": switch_buffer_bytes,
                         "seed": seed, "ns3_seed": seed + 1,
                         "network_priority_enabled": network_priority_enabled,
                         "priority_mapping": {'0': 'urgent_kv', '1': 'collective', '2': 'normal_kv', '3': 'loose_kv'}}
        self._write_metadata()

    def _transfer_parts(self, transfer, done):
        # A receiver sequence number is 32-bit in the pinned model. Large logical
        # transfers use multiple reliable messages; this is not layer-wise KV.
        remaining = [math.ceil(transfer.size_bytes / (64 * 1024 * 1024))]
        def part_done():
            remaining[0] -= 1
            if remaining[0] == 0:
                done()
        source, destination = transfer.source_gpu_id, transfer.destination_gpu_id
        local = source // self.topology.gpus_per_node == destination // self.topology.gpus_per_node
        left = transfer.size_bytes
        identifiers = []
        while left:
            amount = min(left, 64 * 1024 * 1024)
            if local and self.topology.intra_node_model == 'gpu_ports':
                command = "LOCAL_PORTS {{id}} {} {} {} {} {} {} {}".format(
                    source // self.topology.gpus_per_node, source, destination, amount,
                    round(self.topology.intra_node_bandwidth_gbps * 1e9),
                    round(self.topology.intra_node_latency_us * 1000), transfer.kind)
            elif local:
                host = source // self.topology.gpus_per_node
                command = "LOCAL {{id}} {} {} {} {} {}".format(
                    host, amount, round(self.topology.intra_node_bandwidth_gbps * 1e9),
                    round(self.topology.intra_node_latency_us * 1000), transfer.kind)
            else:
                command = "SEND {{id}} {} {} {} {} {}".format(
                    self.topology.network_endpoint(source), self.topology.network_endpoint(destination),
                    amount, transfer.kind, transfer.priority)
            identifiers.append(self._register(command, part_done))
            left -= amount
        return identifiers

    def send_hosts(self, source, destination, size_bytes, callback, kind="kv", priority=2):
        """Send between RDMA endpoint IDs (server IDs in legacy shared mode)."""
        if kind not in ("kv", "collective"):
            raise BackendError("unknown transfer kind")
        if type(priority) is not int or not 0 <= priority <= 3:
            raise BackendError('data priority must be an integer in [0, 3]')
        if any(type(value) is not int for value in (source, destination, size_bytes)):
            raise BackendError("endpoint IDs and byte count must be integers")
        if not 0 <= source < self.topology.network_endpoint_count or not 0 <= destination < self.topology.network_endpoint_count:
            raise BackendError("endpoint ID outside topology")
        if size_bytes < 1:
            raise BackendError("transfer size must be positive")
        remaining = [math.ceil(size_bytes / (64 * 1024 * 1024))]
        def done():
            remaining[0] -= 1
            if remaining[0] == 0:
                callback()
        left = size_bytes
        identifiers = []
        while left:
            amount = min(left, 64 * 1024 * 1024)
            if source == destination:
                command = "LOCAL {{id}} {} {} {} {} {}".format(source, amount,
                    round(self.topology.intra_node_bandwidth_gbps * 1e9),
                    round(self.topology.intra_node_latency_us * 1000), kind)
            else:
                command = "SEND {{id}} {} {} {} {} {}".format(source, destination, amount, kind, priority)
            identifiers.append(self._register(command, done))
            left -= amount
        return identifiers

    def submit_frozen_event(self, command, callback):
        """Replay one captured native registration, without compute callbacks."""
        words = command.split()
        if ('\n' in command or '\r' in command or len(words) < 3 or words[0] not in
                ('SEND', 'TIMER', 'LOCAL', 'LOCAL_PORTS') or words[1] != '{id}'):
            raise BackendError('invalid frozen native event')
        return self._register(command, callback)
