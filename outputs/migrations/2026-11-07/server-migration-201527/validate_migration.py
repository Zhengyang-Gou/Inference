"""Server-only migration validation; no full regression or research experiment."""
import csv, hashlib, json, os, platform, socket, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path("/home/s223/m0nesy/Inference")
OUT=Path("/home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527")
EXPECTED_COMMIT="e9b447df7f118661b9b0e4ab2ce13483e7dd9b2a"
os.chdir(ROOT)
env=os.environ.copy()
env["CONDA_EXE"]="/home/s223/miniconda3/bin/conda"
env["PYTHONPATH"]=str(ROOT)
commands=[]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p): return json.loads(p.read_text())
def run(name,args,timeout=180):
    print("START "+name,flush=True)
    began=time.monotonic()
    log=OUT/"logs"/(name+".log")
    with log.open("w") as f:
        try:
            result=subprocess.run(args,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
            rc=result.returncode
        except subprocess.TimeoutExpired:
            rc=124
    commands.append(dict(name=name,argv=args,cwd=str(ROOT),returncode=rc,wall_seconds=time.monotonic()-began,log=str(log)))
    print("END "+name+" rc="+str(rc),flush=True)
    if rc: raise RuntimeError(name+" failed; see "+str(log))
    return log
summary=dict(kind="server_migration_validation",host=socket.gethostname(),repo=str(ROOT),python=sys.version,
             expected_commit=EXPECTED_COMMIT,platform=platform.platform(),started_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),commands=commands,
             old_WSL_experiments_used_as_validation=False,full_regression_run=False,research_experiment_run=False)
try:
    actual=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    if actual!=EXPECTED_COMMIT: raise RuntimeError("HEAD mismatch")
    summary["actual_commit"]=actual
    frozen=ROOT/"artifacts/inputs/T001-baseline-freeze"
    transfer=load(frozen/"TRANSFER.json")
    files=[dict(name=f["name"],expected_sha256=f["sha256"],actual_sha256=sha(frozen/f["name"])) for f in transfer["files"]]
    if not all(f["expected_sha256"]==f["actual_sha256"] for f in files):raise RuntimeError("input hash mismatch")
    summary["frozen_input_checks"]=files
    run("01-python",["./scripts/simai-python","-V"])
    run("02-dependencies",[sys.executable,"-m","pip","check"])
    log=run("03-backend-doctor",["./inference","backend","doctor"])
    doctor=load(log)
    if not doctor["backend_ready"]:raise RuntimeError("backend not ready")
    summary["backend_doctor"]=doctor
    run("04-smoke-config",["./inference","--config","joint_scheduling/configs/baseline_smoke.json","validate"])
    run("05-frozen-config",["./inference","--config",str(frozen/"config.json"),"validate"])
    run("06-targeted-tests",["./scripts/simai-python","-m","unittest",
        "joint_scheduling.backend.tests.test_module_boundaries",
        "joint_scheduling.backend.tests.test_rpc_batching",
        "joint_scheduling.backend.tests.test_vendored_source"],timeout=240)
    run_dir=OUT/"results/smoke"
    run("07-smoke",["./inference","--config","joint_scheduling/configs/baseline_smoke.json","run-baseline","--output",str(run_dir)],timeout=180)
    results=load(run_dir/"result/summary.json")
    cohort=results["arrival_cohort"]
    with (run_dir/"result/requests.csv").open(newline="") as f:requests=list(csv.DictReader(f))
    resources=load(run_dir/"final_resources.json")
    checks=dict(request_count_is_12=len(requests)==12,all_completed=all(r["status"]=="completed" for r in requests),
        failed_zero=cohort["failed_count"]==0,incomplete_zero=cohort["incomplete_count"]==0,
        resources_all_released=bool(resources) and all(v["occupied_bytes"]==v["reserved_bytes"]==0 for v in resources.values()))
    summary["smoke"]=dict(checks=checks,arrival_cohort=cohort,completion_window=results["completion_window"],
        requests=len(requests),slo_met=sum(r["slo_met"]=="True" for r in requests),run=str(run_dir),evidence="server CPU simulation functional smoke; not GPU inference performance")
    if not all(checks.values()):raise RuntimeError("smoke semantic checks failed")
    final=subprocess.check_output(["git","status","--porcelain=v1","--untracked-files=all"],text=True)
    (OUT/"logs/final-status.txt").write_text(final)
    if final:raise RuntimeError("unexpected uncommitted changes after checks")
    summary["status"]="passed"
except Exception as e:
    summary["status"]="failed"
    summary["error"]=str(e)
finally:
    summary["finished_at"]=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    (OUT/"validation-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    (OUT/"commands.json").write_text(json.dumps(commands,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(dict(status=summary["status"],error=summary.get("error"),host=summary["host"],commit=summary.get("actual_commit"),smoke=summary.get("smoke")),ensure_ascii=False,indent=2),flush=True)
raise SystemExit(0 if summary["status"]=="passed" else 1)
