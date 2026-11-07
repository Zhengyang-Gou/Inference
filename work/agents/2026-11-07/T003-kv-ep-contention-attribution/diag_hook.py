"""Static reproduction of the first failing hook call; no ns-3 backend. """
import io, json, traceback, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from joint_scheduling.config import load_config
import work.agents  # noqa
import importlib.util
spec = importlib.util.spec_from_file_location(
    "live_baseline", "work/agents/2026-11-07/T003-kv-ep-contention-attribution/live_baseline.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg = load_config(str(m.DIAG / "config.json"))
probe = m.T3Probe(cfg, io.StringIO(), __import__("csv").writer(io.StringIO()))
r03 = json.load(open("outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/results/boundary-events.json", encoding="utf-8"))
row = next(r for r in r03["hooks"]["captured_rows"] if r["event"] == "transfer_started")
data = {k: row[k] for k in ("request_id", "transfer_id", "transfer_kind", "block_id", "layer_id",
                            "offset_bytes", "ready_at_seconds", "size_bytes", "source_gpu_id",
                            "destination_gpu_id", "priority") if k in row}
data.setdefault("transfer_kind", "kv")
print("probe data:", json.dumps(data, ensure_ascii=False))
for label, fn in [("probe.event", lambda: probe.event("transfer_started", 472621, data)),
                  ("probe.capture", lambda: probe.capture("transfer_started", 472621, data))]:
    try:
        fn(); print("OK", label)
    except Exception:
        print("FAILED", label); traceback.print_exc()
