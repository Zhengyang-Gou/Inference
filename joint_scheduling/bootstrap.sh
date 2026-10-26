#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_conda=${CONDA_EXE:-conda}
if ! command -v "$task_conda" >/dev/null 2>&1; then
    printf '%s\n' 'Conda is unavailable. Install or initialize Conda first.' >&2
    exit 2
fi
cd "$task_root"
if "$task_conda" run --name simai python --version >/dev/null 2>&1; then
    task_action=install
else
    task_action=create
fi
"$task_conda" "$task_action" --yes --name simai --override-channels --channel conda-forge python=3.12 pip
"$task_root/scripts/simai-python" -m pip install -r "$task_root/joint_scheduling/backend/requirements.txt"
printf '%s\n' 'Conda environment simai is ready. Next: ./inference backend build'
