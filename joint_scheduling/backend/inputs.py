"""Compatibility imports for the input helpers now owned by their domains."""
from ..validation import number, read_json
from ..workload.inputs import load_workload, validate_requests

__all__ = ["number", "read_json", "load_workload", "validate_requests"]
