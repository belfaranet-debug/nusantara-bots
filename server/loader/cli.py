"""CLI: list loaded skills / characters with their validation status.

Usage (run from ~/workspace/grokbot-own)::

    python3 -m loader.cli skills [--shelf DIR] [--json]
    python3 -m loader.cli characters [--dir DIR] [--json]
    python3 -m loader.cli check [--shelf DIR] [--dir DIR] [--json]

Defaults: skills = ../grokbot/skill-pack (read-only shelf),
characters = ./characters (the runtime character directory).
Exit code 0 = everything valid, 1 = at least one item invalid
(or a directory argument is missing), 2 = bad CLI usage.
No network, no model calls, no writes anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .characters import CHARACTERS_DIR
from .skills import SKILL_SHELF_DIR
from . import load_characters, load_skills


def _row(item, kind: str) -> dict:
    row = {"kind": kind, "status": item.status, "path": str(item.path),
           "errors": item.errors}
    if kind == "skill":
        row.update({"name": item.name, "version": item.version,
                    "version_source": item.version_source,
                    "description_chars": len(item.description)})
    else:
        row.update({"name": item.name, "role": item.role,
                    "skills": item.skills, "tools": item.tools,
                    "memory_scope": item.memory_scope})
    return row


def _print(rows, as_json: bool) -> int:
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            mark = "PASS" if r["status"] == "valid" else "FAIL"
            extra = ""
            if r["kind"] == "skill":
                extra = " version=%s (%s) desc=%d chars" % (
                    r["version"] or "-", r["version_source"] or "-",
                    r["description_chars"])
            else:
                extra = " role=%r skills=%s tools=%s memory_scope=%s" % (
                    r["role"], r["skills"], r["tools"], r["memory_scope"] or "-")
            print("%s %s %s%s" % (mark, r["name"], r["path"], extra))
            for e in r["errors"]:
                print("     - %s: %s" % (e["code"], e["message"]))
        valid = sum(1 for r in rows if r["status"] == "valid")
        print("%d/%d valid" % (valid, len(rows)))
    return 0 if all(r["status"] == "valid" for r in rows) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="loader.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("skills"); p.add_argument("--shelf", default=str(SKILL_SHELF_DIR)); p.add_argument("--json", action="store_true")
    p = sub.add_parser("characters"); p.add_argument("--dir", default=str(CHARACTERS_DIR)); p.add_argument("--json", action="store_true")
    p = sub.add_parser("check"); p.add_argument("--shelf", default=str(SKILL_SHELF_DIR)); p.add_argument("--dir", default=str(CHARACTERS_DIR)); p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = []
    if args.cmd in ("skills", "check"):
        shelf = Path(args.shelf)
        if not shelf.is_dir():
            print("shelf directory not found: %s" % shelf, file=sys.stderr)
            return 1
        rows += [_row(s, "skill") for s in load_skills(shelf)]
    if args.cmd in ("characters", "check"):
        rows += [_row(c, "character") for c in load_characters(Path(args.dir))]
    if not rows and args.cmd != "check":
        print("(no items found)")
    return _print(rows, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
