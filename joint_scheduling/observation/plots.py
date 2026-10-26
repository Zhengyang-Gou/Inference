"""Offline state timelines; plotting never changes execution."""

import csv
from pathlib import Path


def plot_observation(directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory = Path(directory)
    with (directory / "state.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    times = [float(row["time_seconds"]) for row in rows]
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    plots = [(("prefill_queue_length", "Prefill"), ("decode_queue_length", "Decode")),
             (("compute_busy_instances", "Compute busy"), ("execution_busy_instances", "Step busy")),
             (("kv_inflight_bytes", "KV"), ("collective_inflight_bytes", "Collective")),
             (("occupied_bytes", "Occupied"), ("reserved_bytes", "Reserved"))]
    for axis, pairs, label in zip(axes, plots, ("Queued requests", "Instances", "In-flight MiB", "Memory MiB")):
        divisor = 2**20 if "MiB" in label else 1
        for key, name in pairs:
            axis.step(times, [float(row[key]) / divisor for row in rows], where="post", label=name)
        axis.set_ylabel(label)
        axis.legend(loc="upper right")
        axis.grid(alpha=0.2)
    axes[-1].set_xlabel("Simulation time (seconds)")
    fig.suptitle("Simulator state | in-flight whole messages, not remaining wire bytes")
    fig.tight_layout()
    path = directory / "timeline.png"
    try:
        with path.open("xb") as handle:
            fig.savefig(handle, format="png", dpi=160)
    finally:
        plt.close(fig)
    return path
