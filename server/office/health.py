"""Health assessment for the own build (office piece).

Adapted in SHAPE from hermes-virtual-office's health.ts (MIT) — see
office/SOURCE.md. Three levels, same as upstream, to avoid alarm fatigue:

    ok    — every check passes.
    warn  — an OPTIONAL check fails (a service we can live without).
    bad   — a CORE check fails (memory, chat, or characters broken).

`reasons` is always filled when the level is not ok, and a failed read
is reported as a failure, never as empty data.

Differences from upstream, all deliberate:

* No cost thresholds. This build is BYO-API: inference cost sits with
  the owner of the key in the browser, not with this server, so there
  is nothing honest to meter here.
* No error-log thresholds. The server writes no error log (its access
  log records method + path only), so there is no error count to judge.
* No "one waiting approval = warn" rule. Our drafts do not stop any
  agent (they are just drafts waiting for a human), so a waiting draft
  is reported as information, not as a warning. The approval queue
  itself is a Phase 2 item.
"""

from __future__ import annotations

import time

HEALTH_LEVELS = ("ok", "warn", "bad")

#: A check without an explicit severity fails closed: it is core.
DEFAULT_SEVERITY = "core"


def assess_health(checks: dict, started_at=None, version: str = "next-level-phase-1",
                  now=None) -> dict:
    """Assess health from a dict of named checks.

    checks: {name: {"ok": bool, "severity": "core"|"optional",
                    "detail": str}}.
    started_at: epoch seconds the server started (for uptime), or None.
    Returns {"level", "reasons", "checks", "uptime_seconds", "version"}.
    Never raises on bad input shapes (a malformed check counts as failed).
    """
    if now is None:
        now = time.time()
    normalised = {}
    reasons = []
    level = "ok"
    for name, raw in (checks or {}).items():
        if isinstance(raw, dict):
            ok = bool(raw.get("ok"))
            severity = raw.get("severity") or DEFAULT_SEVERITY
            detail = raw.get("detail") or ""
        else:
            ok = False
            severity = DEFAULT_SEVERITY
            detail = ""
        if severity not in ("core", "optional"):
            severity = DEFAULT_SEVERITY
        if not detail:
            detail = "%s: %s" % (name, "ok" if ok else "failed")
        normalised[name] = {"ok": ok, "severity": severity, "detail": detail}
        if not ok:
            reasons.append(detail)
            if severity == "core":
                level = "bad"
            elif level != "bad":
                level = "warn"
    uptime = None
    if started_at is not None:
        try:
            uptime = max(0, int(now - float(started_at)))
        except (TypeError, ValueError):
            uptime = None
    return {
        "level": level,
        "reasons": reasons,
        "checks": normalised,
        "uptime_seconds": uptime,
        "version": version,
    }
