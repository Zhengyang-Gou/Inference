#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_python="$task_root/scripts/simai-python"
cd "$task_root"
"$task_python" "$task_root/scripts/check_repository.py"
"$task_root/joint_scheduling/jsim" backend doctor
"$task_python" -m unittest discover -s joint_scheduling/evaluation/tests -v
"$task_python" -m unittest discover -s joint_scheduling/backend/tests -v
"$task_root/joint_scheduling/jsim" backend smoke
