"""Legacy snapshot client for the Virtual Office deck.

Subprocess transport to the PC-side reader script ONLY, the same
pattern as device_client: this module imports no HTTP client and
makes no outbound request, so the BYO-API invariant holds.

The reader returns raw sections (bots, threads, message activity,
recent runs, counts, model prefs). This client assembles them
into the deck shape: each bot with its latest thread (message
count + last activity) and its latest run (status + trigger +
timestamp). No message text, instruction, secret, or credential
is ever read — the reader only selects metadata, and this client
only passes that metadata through.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_READER_CANDIDATES = (
    os.environ.get("GROKBOT_LEGACY_READER"),
    str(_REPO_ROOT / "legacy" / "read_legacy.py"),
    "/data/grokbot-own/legacy/read_legacy.py",
)


class LegacyError(Exception):
    """Legacy reader unavailable, or a legacy read failed."""


class LegacyClient:
    """Subprocess transport to the read-only legacy reader."""

    def __init__(self, reader_py=None):
        self.reader_py = reader_py
        if not self.reader_py:
            for cand in _DEFAULT_READER_CANDIDATES:
                if cand and os.path.isfile(cand):
                    self.reader_py = cand
                    break

    def available(self):
        return bool(self.reader_py and os.path.isfile(self.reader_py))

    def _run(self):
        if not self.available():
            raise LegacyError("legacy reader not found")
        proc = subprocess.run(
            [sys.executable, self.reader_py],
            capture_output=True, text=True, timeout=90)
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            raise LegacyError("legacy reader returned non-JSON output")
        if not data.get("ok"):
            raise LegacyError(data.get("error", "legacy read failed"))
        return data

    def snapshot(self):
        """Assembled live office state (metadata only)."""
        raw = self._run()
        threads = raw.get("threads") or []
        activity = {row.get("threadId"): row
                    for row in (raw.get("activity") or [])
                    if row.get("threadId")}
        threads_by_bot = {}
        for thread in threads:
            bot_id = thread.get("botId")
            if not bot_id:
                continue
            prev = threads_by_bot.get(bot_id)
            if prev is None or (thread.get("createdAt") or "") >= \
                    (prev.get("createdAt") or ""):
                threads_by_bot[bot_id] = thread
        latest_run = {}
        for run in (raw.get("runs") or []):
            bot_id = run.get("botId")
            if not bot_id:
                continue
            prev = latest_run.get(bot_id)
            if prev is None or (run.get("updatedAt") or "") >= \
                    (prev.get("updatedAt") or ""):
                latest_run[bot_id] = run
        bots = []
        for bot in (raw.get("bots") or []):
            thread = threads_by_bot.get(bot.get("id"))
            thread_info = None
            if thread is not None:
                act = activity.get(thread["id"]) or {}
                thread_info = {
                    "id": thread["id"],
                    "messages": act.get("n", thread.get("nextMessageSeq")),
                    "last_activity": act.get("last"),
                }
            run = latest_run.get(bot.get("id"))
            run_info = None
            if run is not None:
                run_info = {"status": run.get("status"),
                            "trigger": run.get("trigger"),
                            "updated": run.get("updatedAt"),
                            "error": run.get("error") or ""}
            bots.append({
                "id": bot.get("id"),
                "name": bot.get("name"),
                "title": bot.get("title"),
                "created": bot.get("createdAt"),
                "updated": bot.get("updatedAt"),
                "thread": thread_info,
                "latest_run": run_info,
            })
        return {
            "ok": True,
            "fetched_at": raw.get("fetched_at"),
            "bots": bots,
            "runs": raw.get("runs") or [],
            "counts": raw.get("counts") or {},
            "model_prefs": raw.get("model_prefs") or [],
        }
