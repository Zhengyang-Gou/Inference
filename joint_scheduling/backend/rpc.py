"""Timeout-bounded line-protocol client shared by the native network backends.

Both the ns-3 adapter and the flowSim adapter own simulated time in a child
process. This module implements the protocol mechanics once: ordered submission,
bounded batching, completion callbacks, deadline advances and telemetry reads.
Subclasses only translate one logical transfer into that backend's commands.
"""

import json
import math
import os
import selectors
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .build import BackendError
from ..feedback import TransferProgress


class LineRpcBackend:
    """Common transport for a native event loop spoken over stdin/stdout."""

    backend_name = "unknown"
    log_name = "backend.log"

    def _launch(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self._log, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        if self._read() != "READY {}".format(self.protocol_version):
            raise BackendError("backend protocol handshake failed")

    def _write_metadata(self):
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
        if self.native_schedule is not None:
            self.native_schedule(dict(time_ns=self.now_ns, native_id=identifier, command=command,
                                      after_ids=list(self._epoch_completions)))
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
            raise BackendError("time is outside the nanosecond clock range")
        return result

    def call_at(self, time_seconds, callback):
        target = self._time_ns(time_seconds)
        if target < self.now_ns:
            raise BackendError("cannot schedule an event in the past")
        return self._register("TIMER {id} " + str(target - self.now_ns), callback)

    def _transfer_parts(self, transfer, done):
        raise NotImplementedError

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
            parts = self._transfer_parts(transfer, done)
            for identifier in parts:
                self._part_owners[identifier] = transfer.transfer_id
            return parts
        except Exception:
            self._logical_transfers.discard(transfer.transfer_id)
            raise

    def network_snapshot(self):
        """Read current telemetry without advancing simulated time."""
        response = self._command("STATS")
        if not response.startswith("STATS "):
            raise BackendError("invalid network telemetry response")
        snapshot = json.loads(response[6:])
        if snapshot["time_ns"] != self.now_ns:
            raise BackendError("network telemetry clock differs from simulated time")
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
        if now != self.now_ns:
            self._epoch_completions = []
        self.now_ns = now
        self._epoch_completions.extend(ids)
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

    def _init_state(self):
        self.now_ns = 0
        self._next_id = 1
        self._epoch_completions = []
        self._callbacks = {}
        self._logical_transfers = set()
        self._part_owners = {}
        self._buffer = b""
        self._batch_depth = 0
        self._pending_submissions = []
        self._submission_commands = self._submission_batches = self._barrier_commands = 0
