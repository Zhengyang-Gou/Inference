"""Timeout-bounded RPC to the actual ns-3 event loop, with completion callbacks."""

import json
import math
import os
import selectors
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .build import BackendError, LOCK, require_backend
from ..feedback import TransferProgress


class Ns3Backend:
    def __init__(self, topology, work_dir, seed=7, congestion_control="dcqcn", packet_bytes=1000,
                 switch_buffer_bytes=16 * 1024 * 1024, rpc_timeout_seconds=60, network_priority_enabled=False,
                 *, submission_batch_size=128):
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
        self.now_ns = 0
        self._next_id = 1
        self._callbacks = {}
        self._logical_transfers = set()
        self._part_owners = {}
        self._buffer = b""
        self.submission_batch_size = submission_batch_size
        self._batch_depth = 0
        self._pending_submissions = []
        self._submission_commands = self._submission_batches = self._barrier_commands = 0
        self.timeout = rpc_timeout_seconds
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        topo_file = export_backend_topology(topology, self.work_dir / "network.topo")
        self.log_path = self.work_dir / "backend.log"
        self._log = self.log_path.open("wb")
        self.process = subprocess.Popen([str(self.state["executable"]), str(topo_file), str(seed + 1),
                                        "1" if congestion_control == "dcqcn" else "0",
                                        str(packet_bytes), str(switch_buffer_bytes), str(int(network_priority_enabled))],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            if self._read() != "READY {}".format(LOCK["protocol_version"]):
                raise BackendError("backend protocol handshake failed")
        except Exception:
            self.close()
            raise
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
                         "priority_mapping": {'0':'urgent_kv', '1':'collective', '2':'normal_kv', '3':'loose_kv'}}
        (self.work_dir / "backend_metadata.json").write_text(json.dumps(self.metadata, indent=2) + "\n")

    def _read(self):
        deadline = time.monotonic() + self.timeout
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("JSB ERROR "):
                    raise BackendError(text + "; see " + str(self.log_path))
                if text.startswith("JSB "):
                    return text[4:]
                self._log.write(line + b"\n"); self._log.flush()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(max(0, remaining)):
                raise BackendError("backend RPC timed out; see {}".format(self.log_path))
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise BackendError("backend exited unexpectedly (code {}); see {}".format(self.process.poll(), self.log_path))
            self._buffer += chunk
            if len(self._buffer) > 1024 * 1024:
                raise BackendError("backend response exceeded the protocol size limit")

    def _write_commands(self, text):
        if self.process.poll() is not None:
            raise BackendError("backend process is not running")
        try:
            data = memoryview((text + "\n").encode())
            while data:
                written = self.process.stdin.write(data)
                if not written:
                    raise OSError('backend pipe accepted no data')
                data = data[written:]
        except (BrokenPipeError, OSError) as error:
            raise BackendError("backend pipe closed; see {}".format(self.log_path)) from error

    def _command(self, text):
        # A query or time advance observes every preceding submission, including
        # those queued by an enclosing callback's batch scope.
        self._flush_submissions()
        self._barrier_commands += 1
        self._write_commands(text)
        return self._read()

    def _register(self, command, callback):
        identifier = self._next_id
        self._next_id += 1
        self._callbacks[identifier] = callback
        self._pending_submissions.append((identifier, command.format(id=identifier)))
        self._submission_commands += 1
        if not self._batch_depth or len(self._pending_submissions) >= self.submission_batch_size:
            self._flush_submissions()
        return identifier

    def _flush_submissions(self):
        if not self._pending_submissions:
            return
        pending, self._pending_submissions = self._pending_submissions, []
        try:
            # The bounded batch keeps OK replies below the pipe's capacity.
            # ns-3 processes the original commands in order without advancing
            # simulated time. Individual flow IDs and ACK semantics stay intact.
            self._write_commands('\n'.join(command for _, command in pending))
            self._submission_batches += 1
            for _ in pending:
                if self._read() != 'OK':
                    raise BackendError('backend rejected command')
        except Exception:
            for identifier, _ in pending:
                self._callbacks.pop(identifier, None)
            raise

    @contextmanager
    def submission_batch(self):
        """Coalesce ordered submissions; queries and advances always flush."""
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if not self._batch_depth:
                self._flush_submissions()

    def now_seconds(self):
        return self.now_ns / 1e9

    def _time_ns(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds):
            raise BackendError("time must be finite")
        result = round(seconds * 1e9)
        if not 0 <= result < 2**63:
            raise BackendError("time is outside the ns-3 nanosecond clock range")
        return result

    def call_at(self, time_seconds, callback):
        target = self._time_ns(time_seconds)
        if target < self.now_ns:
            raise BackendError("cannot schedule an event in the past")
        return self._register("TIMER {id} " + str(target - self.now_ns), callback)

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
        # A receiver sequence number is 32-bit in the pinned model. Large logical
        # transfers use multiple reliable messages; this is not layer-wise KV.
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

    def _send_local_gpus(self, source, destination, size_bytes, callback, kind):
        remaining = [math.ceil(size_bytes / (64 * 1024 * 1024))]
        def done():
            remaining[0] -= 1
            if not remaining[0]:
                callback()
        identifiers = []
        left = size_bytes
        while left:
            amount = min(left, 64 * 1024 * 1024)
            command = "LOCAL_PORTS {{id}} {} {} {} {} {} {} {}".format(
                source // self.topology.gpus_per_node, source, destination, amount,
                round(self.topology.intra_node_bandwidth_gbps * 1e9),
                round(self.topology.intra_node_latency_us * 1000), kind)
            identifiers.append(self._register(command, done))
            left -= amount
        return identifiers

    def submit(self, transfer, on_progress):
        if transfer.kind not in ("kv", "collective"):
            raise BackendError("unknown transfer kind")
        if type(transfer.priority) is not int or not 0 <= transfer.priority <= 3:
            raise BackendError('data priority must be an integer in [0, 3]')
        if not isinstance(transfer.transfer_id, str) or not transfer.transfer_id or transfer.transfer_id in self._logical_transfers:
            raise BackendError("logical transfer IDs must be non-empty and unique while active")
        for gpu in (transfer.source_gpu_id, transfer.destination_gpu_id):
            if type(gpu) is not int or not 0 <= gpu < self.topology.num_gpus:
                raise BackendError("GPU ID outside topology")
        if type(transfer.size_bytes) is not int or transfer.size_bytes < 1:
            raise BackendError("transfer size must be a positive integer")
        self._logical_transfers.add(transfer.transfer_id)
        def done():
            self._logical_transfers.remove(transfer.transfer_id)
            for identifier in parts:
                self._part_owners.pop(identifier, None)
            on_progress(TransferProgress(transfer.transfer_id, self.now_seconds(), True, True))
        try:
            source, destination = transfer.source_gpu_id, transfer.destination_gpu_id
            local = source // self.topology.gpus_per_node == destination // self.topology.gpus_per_node
            if local and self.topology.intra_node_model == 'gpu_ports':
                parts = self._send_local_gpus(source, destination, transfer.size_bytes, done, transfer.kind)
            elif local:
                host = source // self.topology.gpus_per_node
                parts = self.send_hosts(host, host, transfer.size_bytes, done, transfer.kind, transfer.priority)
            else:
                parts = self.send_hosts(self.topology.network_endpoint(source), self.topology.network_endpoint(destination),
                                       transfer.size_bytes, done, transfer.kind, transfer.priority)
            for identifier in parts:
                self._part_owners[identifier] = transfer.transfer_id
            return parts
        except Exception:
            self._logical_transfers.discard(transfer.transfer_id)
            raise

    def network_snapshot(self):
        """Read current packet counters/queues/ACKs without advancing ns-3 time."""
        response = self._command("STATS")
        if not response.startswith("STATS "):
            raise BackendError("invalid network telemetry response")
        snapshot = json.loads(response[6:])
        if snapshot["time_ns"] != self.now_ns:
            raise BackendError("network telemetry clock differs from ns-3 time")
        for flow in snapshot["flows"]:
            flow["logical_id"] = self._part_owners.get(flow["id"])
        return snapshot

    def advance(self, deadline_seconds):
        deadline = self._time_ns(deadline_seconds)
        if deadline < self.now_ns:
            raise BackendError("cannot advance backwards")
        response = self._command("ADVANCE {}".format(deadline)).split()
        if len(response) < 3 or response[0] != "TIME":
            raise BackendError("invalid backend time response")
        now, count = int(response[1]), int(response[2])
        ids = [int(value) for value in response[3:]]
        if now < self.now_ns or now > deadline or len(ids) != count:
            raise BackendError("backend violated clock/completion protocol")
        self.now_ns = now
        with self.submission_batch():
            for identifier in ids:
                callback = self._callbacks.pop(identifier, None)
                if callback is None:
                    raise BackendError("unknown completion ID from backend")
                callback()
        return count

    def run_until(self, deadline_seconds):
        target = self._time_ns(deadline_seconds)
        while True:
            completions = self.advance(deadline_seconds)
            if self.now_ns == target and completions == 0:
                break

    def close(self):
        if hasattr(self, "process"):
            if self.process.poll() is None:
                try:
                    self._flush_submissions()
                    self.process.stdin.write(b"CLOSE\n")
                    self.process.wait(timeout=5)
                except (BackendError, OSError, subprocess.TimeoutExpired):
                    self.process.kill(); self.process.wait(timeout=5)
            self.process.stdin.close(); self.process.stdout.close()
        if hasattr(self, "selector"):
            self.selector.close()
        if hasattr(self, "_log"):
            self._log.close()
        if hasattr(self, 'process'):
            (self.work_dir / 'backend_rpc_summary.json').write_text(json.dumps(dict(
                submission_batch_size=self.submission_batch_size,
                submission_commands=self._submission_commands,
                submission_batches=self._submission_batches,
                barrier_commands=self._barrier_commands,
                synchronous_exchanges=self._submission_batches+self._barrier_commands,
                time_ns=self.now_ns), indent=2)+'\n')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
