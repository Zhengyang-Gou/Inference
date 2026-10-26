"""Real packet-network and deterministic end-to-end regression checks."""

import json
import uuid
from dataclasses import replace
from pathlib import Path

from .build import PACKAGE, BackendError
from .client import Ns3Backend
from .runner import run_baseline
from ..config import load_config


def smoke_test(output_dir=None):
    output = Path(output_dir).resolve() if output_dir else PACKAGE / "artifacts" / "backend-smoke" / uuid.uuid4().hex[:12]
    output.mkdir(parents=True, exist_ok=False)
    config = load_config(PACKAGE / "configs" / "baseline_smoke.json")
    results = {}
    for name, bandwidth, sources in (("single_10g", 10.0, [0]), ("single_20g", 20.0, [0]),
                                      ("incast_10g", 10.0, [0, 1])):
        topo = replace(config.topology, nic_bandwidth_gbps=bandwidth, uplink_bandwidth_gbps=bandwidth * 2)
        completions = []
        with Ns3Backend(topo, output / name, congestion_control="dcqcn") as backend:
            for src in sources:
                backend.send_hosts(src, 2, 2 * 1024 * 1024,
                                   lambda src=src: completions.append({"source": src, "seconds": backend.now_seconds()}))
            backend.run_until(0.1)
        if len(completions) != len(sources):
            raise BackendError("smoke probe did not finish: {}".format(name))
        results[name] = completions
    slow = results["single_10g"][0]["seconds"]
    fast = results["single_20g"][0]["seconds"]
    incast = max(row["seconds"] for row in results["incast_10g"])
    if not fast < slow * 0.75:
        raise BackendError("higher NIC bandwidth did not reduce packet-transfer time")
    if not incast > slow * 1.4:
        raise BackendError("shared receiver NIC contention was not visible")
    first = run_baseline(config, output / "baseline-a")
    second = run_baseline(config, output / "baseline-b")
    if (output / "baseline-a" / "events.jsonl").read_bytes() != (output / "baseline-b" / "events.jsonl").read_bytes():
        raise BackendError("identical inputs/seed did not produce a deterministic trace")
    if first.summary["completion_window"]["completed_count"] != 12:
        raise BackendError("baseline requests did not all complete")
    resources = json.loads((output / "baseline-a" / "final_resources.json").read_text())
    if any(row["occupied_bytes"] or row["reserved_bytes"] for row in resources.values()):
        raise BackendError("completed baseline leaked KV reservations/occupancy")
    report = {"passed": True, "output_dir": str(output), "synthetic_compute_profile": True,
              "network_is_actual_ns3_packet_simulation": True, "deterministic_replay": True,
              "network_probes": results, "baseline_completed_requests": len(second.requests),
              "baseline_completion_window": first.summary["completion_window"]}
    (output / "smoke_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
