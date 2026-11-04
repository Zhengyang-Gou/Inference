"""Experiment matrices with independent planning and configurable execution."""
from .planning import plan_suite


def run_suite(spec_path, output_dir, make_plots=False, fail_fast=False, progress=None, *, jobs=1):
    """Load the execution backend only when a suite is actually run."""
    from .runner import run_suite as execute
    return execute(spec_path, output_dir, make_plots, fail_fast, progress, jobs=jobs)


__all__ = ["plan_suite", "run_suite"]
