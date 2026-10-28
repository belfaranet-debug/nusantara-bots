"""Merge the computer registry with live device-bridge status.

The registry (``computers/registry.py``) records the owner's machines;
the device bridge (``devices/bridge.py``) reports what is actually
attached right now: the phone on ADB, the :100 display, the scrcpy
mirror. This module joins the two views into one per-machine record
for ``GET /api/computers`` — a pure function over plain data, so it
is unit-testable without a bridge, a phone, or a display.

Honesty rules (same as the rest of the deck):

* The attached devices belong to the OWNER, not to one machine: the
  bridge is shared, so every machine record carries the same live
  device view. The record says so in ``devices.shared``.
* When the bridge is unavailable, ``devices`` is ``None`` and
  ``devices_reason`` says why — never an empty object pretending
  the devices were checked.
* "Last shot" ages come from the last shot the server actually
  recorded (a vision turn's shot metadata), never from a fresh
  capture: this endpoint must not take screenshots as a side
  effect. No vision turn yet means ``last_shot`` is ``None``.
* ``primary`` is the first machine with status ``active`` in
  registration order. No active machine means no primary.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_ts(value):
    """Epoch seconds for an ISO timestamp string, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _shot_entry(shot, now):
    """One last-shot entry: {ts, age_seconds} or None.

    ``shot`` is {"ts": <iso>, "ok": bool, "bytes": int} as recorded
    by a vision turn. A failed shot (ok False) is not a last shot.
    """
    if not isinstance(shot, dict) or not shot.get("ok"):
        return None
    ts = _parse_ts(shot.get("ts"))
    if ts is None:
        return None
    age = max(0.0, now - ts) if now is not None else None
    return {"ts": shot.get("ts"),
            "age_seconds": round(age, 1) if age is not None else None,
            "bytes": shot.get("bytes", 0)}


def _devices_view(bridge_available, bridge_status, bridge_error,
                  last_shots, now):
    """The shared live-device view, or (None, reason) when the
    bridge cannot be read."""
    if not bridge_available:
        reason = bridge_error or "device bridge unavailable"
        return None, reason
    if not isinstance(bridge_status, dict):
        return None, (bridge_error
                      or "device bridge available but status unreadable")
    adb_devices = bridge_status.get("adb_devices")
    if not isinstance(adb_devices, list):
        adb_devices = []
    attached = [d for d in adb_devices
                if isinstance(d, dict) and d.get("state") == "device"]
    phone = attached[0] if attached else None
    shots = last_shots if isinstance(last_shots, dict) else {}
    view = {
        "available": True,
        "shared": True,
        "phone": {
            "connected": phone is not None,
            "serial": phone.get("serial") if phone else None,
            "state": phone.get("state") if phone else None,
        },
        "display": {
            "running": bool(bridge_status.get("display_100")),
        },
        "mirror_running": bool(bridge_status.get("mirror_running")),
        "last_shot": {
            "phone": _shot_entry(shots.get("phone"), now),
            "display": _shot_entry(shots.get("display"), now),
        },
    }
    return view, None


def merge_machine_devices(machines, bridge_available,
                          bridge_status=None, bridge_error=None,
                          last_shots=None, now=None):
    """Annotate every registry record with its live device view.

    Returns a NEW list of copies; the input records are untouched.
    Each record gains:

    * ``primary`` (bool) — first ``active`` machine in order.
    * ``devices`` (dict | None) — the shared live view (see above).
    * ``devices_reason`` (str | None) — why ``devices`` is None,
      when it is; otherwise None.
    """
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    devices, reason = _devices_view(bridge_available, bridge_status,
                                    bridge_error, last_shots, now)
    primary_id = None
    for m in machines:
        if isinstance(m, dict) and m.get("status") == "active":
            primary_id = m.get("id")
            break
    merged = []
    for m in machines:
        record = dict(m)
        record["primary"] = (record.get("id") == primary_id
                             and primary_id is not None)
        record["devices"] = devices
        record["devices_reason"] = reason
        merged.append(record)
    return merged


def primary_machine(merged_machines):
    """The primary record of an already-merged list, or None."""
    for m in merged_machines or []:
        if isinstance(m, dict) and m.get("primary"):
            return m
    return None
