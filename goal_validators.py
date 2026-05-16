"""Acceptance-gate validators — STUB for workstream C.

Workstream A owns this module. This is a minimal stub so workstream C tests
can import it. The orchestrator (the real implementation merger) will
replace this file verbatim with workstream A's version at merge time.

Public API (real):
    validate_criterion(criterion: dict) -> None
        Schema-check a done_condition. Raise ValueError on malformed input.

    run_validator(criterion: dict) -> tuple[bool, str]
        Execute the criterion against the filesystem / shell / HTTP.
        Returns (passed, remediation). passed=True → remediation == "".
"""
from __future__ import annotations


def validate_criterion(criterion: dict) -> None:
    """Stub: accepts anything. Workstream A replaces with the real schema check."""
    return None


def run_validator(criterion: dict) -> tuple[bool, str]:
    """Stub: every criterion passes. Workstream A replaces with real execution."""
    return True, ""
