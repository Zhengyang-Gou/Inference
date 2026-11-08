#!/bin/sh
# T003-r02 static recheck driver. Documentation / analysis / acceptance-logic only.
# Starts no simulation: no live baseline, no network replay, no long experiment.
set -u
cd /home/s223/m0nesy/Inference || exit 99
export CONDA_EXE=/home/s223/miniconda3/bin/conda
R2=work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02
OUT=outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02
LOG=$OUT/logs
RES=$OUT/results
mkdir -p "$LOG" "$RES"

{
  echo "host=$(hostname)"
  echo "utc_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "local_start=$(date -Iseconds)"
  echo "repo=$(pwd)"
  echo "branch=$(git rev-parse --abbrev-ref HEAD)"
  echo "head=$(git rev-parse HEAD)"
  echo "conda_exe=$CONDA_EXE"
  echo "python=$(./scripts/simai-python -c 'import sys;print(sys.version.split()[0])')"
  echo "git_status_lines=$(git status --short --untracked-files=all | wc -l)"
  echo "--- script hashes ---"
  sha256sum $R2/archive_audit.py $R2/interference_recompute.py $R2/acceptance_revision.py $R2/t3_tree_hash.py
} > "$LOG/r02-00-environment.log" 2>&1
ENV_RC=$?

run() {
  name=$1; shift
  "$@" > "$LOG/$name.log" 2>&1
  rc=$?
  echo "EXIT_CODE=$rc" >> "$LOG/$name.log"
  echo "$name $rc" >> "$LOG/r02-05-exit-codes.txt"
  return 0
}

: > "$LOG/r02-05-exit-codes.txt"

run r02-01-archive-audit ./scripts/simai-python $R2/archive_audit.py
run r02-02-static-recompute ./scripts/simai-python $R2/interference_recompute.py
run r02-03-acceptance-revision ./scripts/simai-python $R2/acceptance_revision.py
run r02-04-tree-hash-after ./scripts/simai-python $R2/t3_tree_hash.py $RES/t003-original-tree-after.json
run r02-06-command-record ./scripts/simai-python $R2/record_static_review.py

{
  echo "utc_end=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "local_end=$(date -Iseconds)"
  echo "env_log_rc=$ENV_RC"
  echo "--- git status after ---"
  git status --short --untracked-files=all
} >> "$LOG/r02-00-environment.log" 2>&1

cat "$LOG/r02-05-exit-codes.txt"
