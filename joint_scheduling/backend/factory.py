"""One backend-selection entry point shared by the runner and experiments.

The runner and every execution component depend only on the network contract in
:mod:`joint_scheduling.feedback`; this module is the single place that maps the
configuration pair ``(feedback.time_authority, feedback.network_backend)`` to a
concrete adapter. No Prefill/Decode/EP/KV/MLU component branches on the backend.
"""

import json

from .build import BackendError, require_backend
from .client import Ns3Backend
from .flow_build import require_flowsim
from .flow_client import FlowSimBackend

NS3 = ("ns3", "ns3_alibabacloud")
FLOWSIM = ("flowsim", "flowsim")
BACKENDS = {NS3: Ns3Backend, FLOWSIM: FlowSimBackend}


def selection(config):
    """Return the validated (time_authority, network_backend) pair."""
    pair = (config.feedback.time_authority, config.feedback.network_backend)
    try:
        BACKENDS[pair]
    except KeyError:
        raise BackendError(
            "unsupported feedback pair time_authority={!r} network_backend={!r}; supported pairs are "
            "{}".format(pair[0], pair[1], ", ".join("{} + {}".format(*name) for name in sorted(BACKENDS))))
    return pair


def create_backend(config, work_dir, *, hooks=None):
    """Create the configured backend adapter for one experiment run."""
    pair = selection(config)
    native_schedule = None
    if hooks is not None:
        native_schedule = getattr(hooks, "native_schedule", None)
    if pair == FLOWSIM and native_schedule is not None:
        raise BackendError("frozen native replay requires feedback.network_backend=ns3_alibabacloud")
    return BACKENDS[pair](
        config.topology, work_dir, config.runtime.seed, config.runtime.congestion_control,
        config.runtime.packet_payload_bytes, config.runtime.switch_buffer_bytes,
        config.runtime.rpc_timeout_seconds,
        network_priority_enabled=config.runtime.network_priority_enabled,
        submission_batch_size=config.runtime.rpc_submission_batch_size,
        native_schedule=native_schedule)


def backend_identity(config):
    """Resolved build identity, read before the run without starting the backend."""
    pair = selection(config)
    if pair == FLOWSIM:
        return json.loads(require_flowsim()["manifest"].read_text())
    return json.loads(require_backend()["manifest"].read_text())


def backend_ready(config):
    """Whether the configured backend can be started with the current build."""
    try:
        if selection(config) == FLOWSIM:
            require_flowsim()
        else:
            require_backend()
        return True
    except BackendError:
        return False
