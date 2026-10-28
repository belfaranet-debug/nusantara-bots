"""Approval queue — the human review gate (Phase 2)."""

from approvals.queue import (
    APPROVAL_ACTIONS,
    APPROVAL_STATUSES,
    DEFAULT_TTL_SECONDS,
    HEALTH_WARN_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    ApprovalQueue,
)

__all__ = [
    "APPROVAL_ACTIONS",
    "APPROVAL_STATUSES",
    "DEFAULT_TTL_SECONDS",
    "HEALTH_WARN_AFTER_SECONDS",
    "STALE_AFTER_SECONDS",
    "ApprovalQueue",
]
