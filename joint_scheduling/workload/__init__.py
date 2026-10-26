"""Reproducible synthetic request generation for the actual baseline runner."""

from .generator import generate_workload, write_workload

__all__ = ["generate_workload", "write_workload"]
