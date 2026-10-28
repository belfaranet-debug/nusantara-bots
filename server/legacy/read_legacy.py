"""Read-only legacy bridge for the Virtual Office deck.

Authorized by the owner (2026-10-24) for READ-ONLY discovery of
live office state: bot names and titles, thread message counts,
run statuses, and timestamps. Nothing else is read, and nothing
is ever written back.

Hard rules enforced by this script itself:
* Only fixed SELECT statements exist in this file, and every
  database session runs with the session forced read-only, so
  even a bug could not write.
* The database container is resolved at runtime: running
  containers whose name mentions the database engine are probed
  for their connection settings (read from the container's own
  environment, never from this repo) and for a table named
  ``bots``. No container name, database name, or login is stored
  in this repo.
* Never selected, printed, or returned: message text, message
  blocks, bot instructions, secrets, credentials, session tokens,
  or any table holding them. The deck shows metadata only:
  names, titles, counts, statuses, timestamps.
* Output is one JSON object on stdout. Diagnostics go to stderr.
* Standard library only. No network code.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

READ_ONLY_OPT = "-c default_transaction_read_only=on"

# Fixed statements only. Row-shaped JSON per line keeps parsing
# robust against spaces in names and titles.
SECTIONS = [
    ("bots",
     'SELECT id, name, title, "createdAt", "updatedAt" FROM bots '
     'ORDER BY "createdAt"'),
    ("threads",
     'SELECT id, "botId", "nextMessageSeq", "createdAt" FROM threads '
     'ORDER BY "createdAt" DESC'),
    ("activity",
     'SELECT "threadId", count(*) AS n, max("createdAt") AS last '
     'FROM messages GROUP BY "threadId"'),
    ("runs",
     'SELECT id, "botId", "threadId", status, trigger, "updatedAt", '
     "left(coalesce(error, ''), 120) AS error FROM runs "
     'ORDER BY "updatedAt" DESC LIMIT 20'),
    ("counts",
     "SELECT 'bots' AS kind, count(*) AS n FROM bots "
     "UNION ALL SELECT 'threads', count(*) FROM threads "
     "UNION ALL SELECT 'runs', count(*) FROM runs "
     "UNION ALL SELECT 'messages', count(*) FROM messages"),
    ("model_prefs",
     'SELECT "spaceId", "modelId", "isDefault" '
     'FROM space_model_preferences'),
]


class ReaderError(Exception):
    """The legacy database could not be found or read."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(argv, timeout=60):
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)
    return proc


def _docker_names():
    proc = _run(["docker", "ps", "--format", "{{.Names}}"], timeout=30)
    if proc.returncode != 0:
        raise ReaderError("could not list containers: %s"
                          % (proc.stderr or "").strip()[:200])
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()]


def _container_var(container, var):
    proc = _run(["docker", "exec", container, "printenv", var],
                timeout=30)
    value = (proc.stdout or "").strip()
    return value or None


def _has_bots_table(container, user, db):
    proc = _run(["docker", "exec", container, "psql", "-U", user,
                 "-d", db, "-At", "-c", "SELECT to_regclass('bots');"],
                timeout=30)
    return proc.returncode == 0 and (proc.stdout or "").strip() == "bots"


def resolve():
    """Find the legacy database container at runtime.

    Returns (container, user, db). No name is stored anywhere:
    every run re-discovers it from the live container list.
    """
    for name in _docker_names():
        if "postgres" not in name:
            continue
        user = _container_var(name, "POSTGRES_USER")
        db = _container_var(name, "POSTGRES_DB")
        if not user or not db:
            continue
        if _has_bots_table(name, user, db):
            return name, user, db
    raise ReaderError("no legacy database container found")


def _psql(container, user, db, script):
    proc = _run(["docker", "exec", "-e",
                 "PGOPTIONS=" + READ_ONLY_OPT, container,
                 "psql", "-U", user, "-d", db, "-At",
                 "-v", "ON_ERROR_STOP=1", "-c", script], timeout=120)
    if proc.returncode != 0:
        raise ReaderError("database read failed: %s"
                          % (proc.stderr or "").strip()[:300])
    return proc.stdout or ""


def build_script():
    parts = []
    for section, query in SECTIONS:
        parts.append("SELECT 'SECTION:%s';" % section)
        parts.append("SELECT row_to_json(t) FROM (%s) t;" % query)
    return "\n".join(parts)


def parse(output):
    sections = {name: [] for name, _ in SECTIONS}
    current = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("SECTION:"):
            current = line.split(":", 1)[1]
            continue
        if current is None:
            continue
        try:
            sections[current].append(json.loads(line))
        except ValueError:
            continue
    counts = {}
    for row in sections["counts"]:
        try:
            counts[row["kind"]] = int(row["n"])
        except (KeyError, TypeError, ValueError):
            continue
    sections["counts"] = counts
    return sections


def main():
    try:
        container, user, db = resolve()
        output = _psql(container, user, db, build_script())
        payload = {"ok": True, "fetched_at": _utcnow(), **parse(output)}
    except ReaderError as err:
        print(json.dumps({"ok": False, "error": str(err)}))
        return 1
    except (subprocess.TimeoutExpired, OSError) as err:
        print(json.dumps({"ok": False, "error": "reader failed: %s" % err}))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
