"""Small CLI for the Grok Bot own-build memory service.

Fully offline: stdlib only, local SQLite only, no provider, no
network. This is the floor guarantee in runnable form -- every
command below works with zero providers available.

Run from ``~/workspace/grokbot-own/``::

    python3 -m memory.cli remember --scope team --author tester \\
        --text "TEST: weekly theme is Sejarah Islam Nusantara"
    python3 -m memory.cli recall --scope team --keywords "theme"
    python3 -m memory.cli list --scope team
    python3 -m memory.cli forget --id 1

Or as a script (same directory)::

    python3 memory/cli.py list --scope team

``--db PATH`` overrides the default ``data/memory.db`` on any
command; ``GROKBOT_MEMORY_DB`` does the same. Only test rows belong
in the default database unless a later phase adopts it as live.
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    # Allow `python3 memory/cli.py ...` as well as `-m memory.cli`.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.store import MemoryStore  # noqa: E402


def _add_db(parser):
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: data/memory.db, or $GROKBOT_MEMORY_DB)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="memory.cli", description="Offline memory service (stdlib only).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("remember", help="Save a fact.")
    p.add_argument("--scope", required=True, help="'team' or 'private:<character>'")
    p.add_argument("--author", required=True, help="character name that wrote it")
    p.add_argument("--text", required=True)
    p.add_argument("--supersedes-id", type=int, default=None,
                   help="id of an older same-scope memory this replaces")
    _add_db(p)

    p = sub.add_parser("recall", help="Keyword + recency recall in one scope.")
    p.add_argument("--scope", required=True)
    p.add_argument("--keywords", default="", help="space-separated keywords")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--include-superseded", action="store_true")
    _add_db(p)

    p = sub.add_parser("list", help="List all memories in one scope, newest first.")
    p.add_argument("--scope", required=True)
    _add_db(p)

    p = sub.add_parser("forget", help="Delete one memory by id.")
    p.add_argument("--id", type=int, required=True, dest="memory_id")
    _add_db(p)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    with MemoryStore(args.db) as store:
        if args.command == "remember":
            new_id = store.remember(
                args.scope, args.text, args.author,
                supersedes_id=args.supersedes_id)
            print(json.dumps({"id": new_id, "scope": args.scope}, indent=2))
        elif args.command == "recall":
            rows = store.recall(
                args.scope, args.keywords, limit=args.limit,
                include_superseded=args.include_superseded)
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        elif args.command == "list":
            print(json.dumps(store.list(args.scope), indent=2, ensure_ascii=False))
        elif args.command == "forget":
            print(json.dumps({"id": args.memory_id,
                              "deleted": store.forget(args.memory_id)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
