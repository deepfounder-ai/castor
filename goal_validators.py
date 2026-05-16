"""Goal subtask validators — STUB (workstream A owns the real implementation).

Per spec `docs/specs/2026-05-16-acceptance-gate.md` §2: each subtask carries a
`done_condition` of shape ``{"kind": <enum>, "spec": <kind-specific payload>}``
that must pass before the orchestrator can mark the subtask completed.

This file is a workstream-B placeholder so the import chain links cleanly when
workstream B lands first. Workstream A will replace it with the real validator
implementations. The orchestrator (workstream C) will resolve any merge
conflict by taking workstream A's full version verbatim.

Public contract (do NOT change without re-syncing with workstreams A and C):

    validate_criterion(criterion: dict) -> tuple[bool, str]
        Static-shape check at plan-set time. Returns (ok, error_message).
        Empty error_message on success.

    run_validator(criterion: dict) -> tuple[bool, str]
        Runtime check at completion time. Returns (passed, remediation).
        Empty remediation on success. Non-empty remediation is a human-readable
        sentence the orchestrator can show the user / use to retry.
"""
from __future__ import annotations

# Recognised criterion kinds. Workstream A will expand this with real
# per-kind validators (file_exists, http_returns, regex_in_output,
# llm_check, etc.). The stub accepts anything so plan-set doesn't reject
# valid criteria authored against the real schema.
_KNOWN_KINDS = {
    "file_exists",
    "http_returns",
    "regex_in_output",
    "llm_check",
    "always_pass",  # explicit no-op for backward-compat callers
}


def validate_criterion(criterion: dict) -> tuple[bool, str]:
    """Static shape check — does ``criterion`` look like a valid done_condition?

    Returns ``(ok, error_message)``. The stub only enforces the outer envelope
    (dict with ``kind`` + ``spec``). Workstream A will add per-kind shape rules.
    """
    if not isinstance(criterion, dict):
        return False, "done_condition must be a JSON object"
    kind = criterion.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return False, "done_condition.kind is required (string)"
    if "spec" not in criterion:
        return False, "done_condition.spec is required"
    # Stub: unknown kinds pass shape check so workstream A can add new kinds
    # without coordinating a workstream-B re-release.
    return True, ""


def run_validator(criterion: dict | None) -> tuple[bool, str]:
    """Runtime check — does the criterion currently pass?

    Stub returns ``(True, "")`` so workstream B's tests can pin the integration
    contract without dragging in workstream A's runtime dependencies (network,
    LLM, etc.). Workstream A's real implementation dispatches by ``criterion.kind``.
    """
    if criterion is None:
        # No criterion ≡ implicit always-pass (back-compat for callers that
        # somehow stored a subtask without one).
        return True, ""
    if not isinstance(criterion, dict):
        return False, "done_condition is malformed (expected object)"
    return True, ""
