"""Timeout-bounded RPC to the vendored flowSim fluid event loop.

Same contract as :class:`joint_scheduling.backend.client.Ns3Backend`: one clock,
one completion callback per logical transfer, telemetry that never advances
time. The differences are deliberate and recorded in ``metadata``:

* one logical transfer is one fluid flow (no 64 MiB message split: that limit
  belongs to the ns-3 reliable receiver window, not to KV chunking);
* completion means "all bytes serialised at the destination", not an ACK;
* packet queues, ECN/PFC and congestion control are not modelled;
* strict network priority is rejected here instead of being silently ignored.
"""

import json
import math
from pathlib import Path

from .build import BackendError
from .flow_build import LOCK, require_flowsim
from .rpc import LineRpcBackend


class FlowSimBackend(LineRpcBackend):
    backend_name = "simai_flowsim_fluid"
    log_name = "flowsim.log"

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
        if type(network_priority_enabled) is not bool:
            raise BackendError('network_priority_enabled must be boolean')
        if network_priority_enabled:
            raise BackendError(
                'the flowSim fluid model does not model strict priority queues; set '
                'runtime.network_priority_enabled=false or use network_backend=ns3_alibabacloud')
        if native_schedule is not None:
            raise BackendError('the flowSim backend cannot replay frozen ns-3 native events')
        self.state = require_flowsim()
        self.topology = topology
        self.native_schedule = None
        self.protocol_version = LOCK["protocol_version"]
        self.submission_batch_size = submission_batch_size
        self.timeout = rpc_timeout_seconds
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        topo_file = export_backend_topology(topology, self.work_dir / "network.topo")
        route_table = self.work_dir / "flowsim_routes.json"
        self._init_state()
        self.log_path = self.work_dir / self.log_name
        self._log = self.log_path.open("wb")
        self._launch([str(self.state["executable"]), str(topo_file), str(seed), "0",
                      topology.intra_node_model, str(topology.gpus_per_node), str(route_table)])
        info = json.loads(self._command("INFO")[5:])
        self.metadata = {"backend": info["backend"], "protocol_version": LOCK["protocol_version"],
                         "flowsim_repository": LOCK["flowsim_repository"],
                         "flowsim_revision": LOCK["flowsim_revision"],
                         "flowsim_path": LOCK["flowsim_path"],
                         "source_sha256": LOCK["source_sha256"],
                         "patch": LOCK["patch"], "patch_sha256": LOCK["patch_sha256"],
                         "upstream_license": LOCK["upstream_license"],
                         "build_fingerprint": self.state["fingerprint"],
                         "endpoint_model": topology.nic_model,
                         "nic_bandwidth_gbps_per_node": topology.nic_bandwidth_gbps,
                         "nic_bandwidth_gbps_per_endpoint": topology.endpoint_bandwidth_gbps,
                         "local_model": topology.intra_node_model,
                         "local_bandwidth_scope": "per_gpu_tx_rx_port" if topology.intra_node_model == 'gpu_ports' else "shared_node_fifo",
                         "model": LOCK["model"], "completion_semantics": LOCK["completion_semantics"],
                         "link_model": info["link_model"], "latency_model": info["latency_model"],
                         "queue_model": info["queue_model"], "congestion_control": info["congestion_control"],
                         "packet_model": info["packet_model"], "pfc": info["pfc"], "ecmp": info["ecmp"],
                         "priority_model": info["priority_model"],
                         "congestion_control_requested": congestion_control,
                         "packet_bytes_requested": packet_bytes,
                         "switch_buffer_bytes_requested": switch_buffer_bytes,
                         "seed": seed, "route_table": route_table.name,
                         "network_priority_enabled": False}
        self._write_metadata()

    def _transfer_parts(self, transfer, done):
        source, destination = transfer.source_gpu_id, transfer.destination_gpu_id
        local = source // self.topology.gpus_per_node == destination // self.topology.gpus_per_node
        if local and self.topology.intra_node_model == 'gpu_ports':
            command = "LOCAL_PORTS {{id}} {} {} {} {} {} {} {}".format(
                source // self.topology.gpus_per_node, source, destination, transfer.size_bytes,
                round(self.topology.intra_node_bandwidth_gbps * 1e9),
                round(self.topology.intra_node_latency_us * 1000), transfer.kind)
        elif local:
            host = source // self.topology.gpus_per_node
            command = "LOCAL {{id}} {} {} {} {} {}".format(
                host, transfer.size_bytes, round(self.topology.intra_node_bandwidth_gbps * 1e9),
                round(self.topology.intra_node_latency_us * 1000), transfer.kind)
        else:
            command = "SEND {{id}} {} {} {} {} {}".format(
                self.topology.network_endpoint(source), self.topology.network_endpoint(destination),
                transfer.size_bytes, transfer.kind, transfer.priority)
        return [self._register(command, done)]

    def send_hosts(self, source, destination, size_bytes, callback, kind="kv", priority=2):
        """Send between network endpoint IDs, mirroring the ns-3 smoke helper."""
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
        if source == destination:
            command = "LOCAL {{id}} {} {} {} {} {}".format(source, size_bytes,
                round(self.topology.intra_node_bandwidth_gbps * 1e9),
                round(self.topology.intra_node_latency_us * 1000), kind)
        else:
            command = "SEND {{id}} {} {} {} {} {}".format(source, destination, size_bytes, kind, priority)
        return [self._register(command, callback)]

    def submit_frozen_event(self, command, callback):
        raise BackendError('the flowSim backend does not replay frozen ns-3 native events')
