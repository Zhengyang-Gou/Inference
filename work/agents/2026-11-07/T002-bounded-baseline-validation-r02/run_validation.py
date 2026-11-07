"""T002-r02: one bounded ns-3 end-to-end run.

Run once per invocation (--run run-a / run-b) so a failed first run is never
followed by an automatic retry. Hooks are observational only: EPFlowAudit plus
an event counter receive copies of already-emitted events.
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02"
INPUTS = TASK / "inputs"
RESULTS = TASK / "results"

from joint_scheduling.config import load_config
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.execution import RuntimeHooks
from joint_scheduling.backend.runner import run_baseline
from joint_scheduling.experiments.ep_audit import EPFlowAudit


class EventLog:
    """Count already-emitted events; never mutate runtime state."""

    def __init__(self):
        self.counts = Counter()
        self.first_ns = None
        self.last_ns = None

    def event(self, name, time_ns, data):
        self.counts[name] += 1
        if self.first_ns is None:
            self.first_ns = time_ns
        self.last_ns = time_ns

    def document(self):
        return {"event_counts": dict(sorted(self.counts.items())),
                "event_total": sum(self.counts.values()),
                "first_event_ns": self.first_ns, "last_event_ns": self.last_ns}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, choices=("run-a", "run-b"))
    args = parser.parse_args()
    output = RESULTS / args.run
    if output.exists():
        print("refusing to overwrite existing run directory: {}".format(output), file=sys.stderr)
        return 2

    config = load_config(str(INPUTS / "config.json"))
    profile = ComputeProfile(config.profile_path())
    binding_plan = json.loads((INPUTS / "binding_plan.json").read_text(encoding="utf-8"))

    ep = EPFlowAudit(config, profile)
    log = EventLog()

    def dispatch(name, time_ns, data):
        ep.event(name, time_ns, data)
        log.event(name, time_ns, data)

    hooks = RuntimeHooks(event=dispatch)
    began = time.monotonic()
    evidence = {"run": args.run}
    try:
        result = run_baseline(config, output, hooks=hooks, binding_plan=binding_plan)
    except Exception as error:
        evidence.update(status="run_failed", wall_clock_seconds=time.monotonic() - began,
                        error_type=type(error).__name__, error=str(error),
                        hooks_events=log.document())
        (RESULTS / (args.run + "-hooks.json")).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print("RUN FAILED: {}: {}".format(type(error).__name__, error), file=sys.stderr)
        return 1
    evidence["wall_clock_seconds"] = time.monotonic() - began

    summary_path = output / "expert_parallel_summary.json"
    try:
        evidence["ep_flow_audit"] = ep.finish(
            json.loads(summary_path.read_text(encoding="utf-8")))
    except Exception as error:
        evidence["ep_flow_audit"] = {"status": "failed", "error_type": type(error).__name__,
                                     "error": str(error)}
    evidence["hooks_events"] = log.document()
    evidence["result_summary_keys"] = sorted(result.summary.keys())
    evidence["status"] = "completed"
    (RESULTS / (args.run + "-hooks.json")).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"run": args.run, "status": "completed",
                      "wall_clock_seconds": round(evidence["wall_clock_seconds"], 3),
                      "ep_audit": evidence["ep_flow_audit"].get("status"),
                      "hook_events": log.document()["event_total"]}, ensure_ascii=False))
    if evidence["ep_flow_audit"].get("status") != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
