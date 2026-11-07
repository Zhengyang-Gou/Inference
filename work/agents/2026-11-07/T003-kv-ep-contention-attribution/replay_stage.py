"""T003 phases 2-4: ONE frozen native-network replay per invocation.

Uses the official joint_scheduling.experiments.contention.replay unchanged.
The calendar is restored from files (flows.csv / native_schedule.jsonl), so
nothing depends on the live Python process still being alive.
"""
import argparse
import csv
import hashlib
import json
import signal
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
TASK = ROOT / "outputs/agents/2026-11-07/T003-kv-ep-contention-attribution"
DIAG = TASK / "results/diagnostic"
RESULTS = TASK / "results"
HARD_WALL_SECONDS = 15 * 60


class HardStop(Exception):
    pass


def _alarm(_signum, _frame):
    raise HardStop("replay exceeded 15 minute hard limit")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
                          encoding="utf-8")


class ReplayProbe:
    """Minimal view of contention.Probe reconstructed from the frozen files."""

    def __init__(self, diag):
        with (diag / "flows.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))[1:]
        self.flows = []
        for r in rows:
            self.flows.append([int(r[0]), int(r[1]), int(r[2]), int(r[3]),
                               int(r[4]), r[5], int(r[6]), int(r[7]), r[8]])
        self.stages = json.loads((diag / "stages.json").read_text(encoding="utf-8"))
        self.native_schedule = [json.loads(line) for line in
                                (diag / "native_schedule.jsonl").read_text(encoding="utf-8").splitlines()
                                if line.strip()]
        self.calls = {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("both", "collective", "kv"))
    args = parser.parse_args()
    keep = args.stage
    out = DIAG / ("replay-" + keep)
    if out.exists():
        print("REFUSE: replay directory already exists: %s" % out, file=sys.stderr)
        return 2
    evidence_path = RESULTS / ("replay-stage-" + keep + ".json")
    if evidence_path.exists():
        print("REFUSE: stage evidence already exists", file=sys.stderr)
        return 2

    from joint_scheduling.config import load_config
    from joint_scheduling.experiments import contention

    config = load_config(str(DIAG / "config.json"))
    probe = ReplayProbe(DIAG)
    evidence = dict(phase="replay-" + keep, started_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    source_files={name: sha(DIAG / name) for name in
                                  ("flows.csv", "stages.json", "native_schedule.jsonl", "config.json")},
                    native_registration_count=len(probe.native_schedule),
                    flow_count=len(probe.flows))
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(HARD_WALL_SECONDS)
    began = time.monotonic()
    try:
        out.mkdir()
        end, report = contention.replay(out, config, probe, keep)
    except Exception as error:
        wall = time.monotonic() - began
        signal.alarm(0)
        evidence.update(status="failed", wall_clock_seconds=wall,
                        error_type=type(error).__name__, error=str(error))
        dump(evidence_path, evidence)
        print("REPLAY FAILED %s: %s: %s" % (keep, type(error).__name__, error), file=sys.stderr)
        return 1
    wall = time.monotonic() - began
    signal.alarm(0)

    completions = list(csv.DictReader((out / "completions.csv").open(encoding="utf-8")))
    rpc = json.loads((out / "network/backend_rpc_summary.json").read_text(encoding="utf-8"))
    expected_sends = sum(1 for row in probe.native_schedule if row["command"].startswith("SEND "))
    kept_sends = sum(1 for row in probe.native_schedule
                     if row["flow_index"] is not None and
                     (keep == "both" or probe.flows[row["flow_index"]][5] == keep))
    evidence.update(
        status="completed", wall_clock_seconds=wall,
        selected_flows=report["selected_flows"], completed_flows=report["completed_flows"],
        port_cooldown_checks=report["port_cooldown_checks"],
        completion_rows=len(completions),
        completion_indices=[int(r["flow_index"]) for r in completions],
        replay_summary=report,
        rpc_summary=rpc,
        native_submission_identity=dict(
            total_registrations=len(probe.native_schedule),
            send_registrations=expected_sends,
            expected_submitted_registrations=len(probe.native_schedule) - (expected_sends - kept_sends),
            actual_submission_commands=rpc["submission_commands"],
            matches=rpc["submission_commands"] ==
                    len(probe.native_schedule) - (expected_sends - kept_sends)),
        output_files={p.relative_to(out).as_posix(): sha(p) for p in sorted(out.rglob("*")) if p.is_file()},
    )
    dump(evidence_path, evidence)
    print(json.dumps(dict(stage=keep, status="completed", wall_clock_seconds=round(wall, 3),
                          selected=report["selected_flows"], completed=report["completed_flows"],
                          port_cooldown_checks=report["port_cooldown_checks"],
                          native_submission_matches=evidence["native_submission_identity"]["matches"]),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
