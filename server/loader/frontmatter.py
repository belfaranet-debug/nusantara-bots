"""Tiny YAML-ish frontmatter parser (Python standard library only).

Scope, deliberately small and documented so later phases do not have
to guess what is supported:

* The document must start with a ``---`` line and end the frontmatter
  with a second ``---`` line. Everything after the second line is
  the body.
* Top-level ``key: value`` scalars (single line, optional surrounding
  single or double quotes).
* One level of nesting: a key with an empty value followed by
  indented ``subkey: value`` lines becomes a dict
  (this is how the real shelf writes ``metadata:`` / ``version:``).
* Lists, in either form::

      skills: [team-memory, bot-live-presence]
      skills:
        - team-memory
        - bot-live-presence

* Blank lines and full-line ``#`` comments are ignored inside the
  frontmatter.

Anything outside that scope (multi-line folded scalars, deeper
nesting, anchors, multi-document files) is reported as an error
instead of being silently mis-parsed. The parser never raises on
bad input: it returns ``(data, body, errors)`` where ``errors`` is a
list of ``{"code": ..., "message": ...}`` dicts.
"""

from __future__ import annotations

import re

_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_inline_list(value: str, errors: list, key: str):
    """Parse ``[a, b, "c d"]``; on failure append an error, return None."""
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        errors.append({
            "code": "malformed_list",
            "message": "list for %r is not closed with ']': %r" % (key, value),
        })
        return None
    inner = value[1:-1].strip()
    if not inner:
        return []
    items = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            errors.append({
                "code": "malformed_list",
                "message": "empty item in list for %r: %r" % (key, value),
            })
            return None
        items.append(_unquote(part))
    return items


def parse_frontmatter(text: str):
    """Parse ``text`` into ``(data, body, errors)``.

    ``data`` is the frontmatter dict (possibly partial on error),
    ``body`` is the text after the closing ``---`` ("" on error),
    ``errors`` is a list of ``{"code", "message"}`` dicts.
    """
    errors: list = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "", [{
            "code": "missing_frontmatter",
            "message": "document does not start with a '---' frontmatter line",
        }]
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        return {}, "", [{
            "code": "unclosed_frontmatter",
            "message": "no closing '---' frontmatter line found",
        }]
    body = "\n".join(lines[closing + 1:]).strip()
    data: dict = {}
    pending_dict = None   # key awaiting indented subkeys
    pending_list = None   # key awaiting indented "- item" lines
    fm_lines = lines[1:closing]
    for pos, raw in enumerate(fm_lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent > 0:
            if pending_list is not None and stripped.startswith("- "):
                data[pending_list].append(_unquote(stripped[2:]))
                continue
            if pending_dict is not None:
                m = _KEY_RE.match(stripped)
                if m:
                    subkey, subval = m.group(1), m.group(2).strip()
                    if subval == "":
                        errors.append({
                            "code": "malformed_frontmatter",
                            "message": "nested key %r under %r has no value "
                                       "(only one nesting level is supported)"
                                       % (subkey, pending_dict),
                        })
                    elif subval.startswith("["):
                        parsed = _parse_inline_list(subval, errors, subkey)
                        data[pending_dict][subkey] = parsed if parsed is not None else subval
                    else:
                        data[pending_dict][subkey] = _unquote(subval)
                    continue
            errors.append({
                "code": "malformed_frontmatter",
                "message": "unexpected indented line in frontmatter: %r" % raw,
            })
            continue
        # Top-level line.
        pending_dict = None
        pending_list = None
        if stripped.startswith("- "):
            errors.append({
                "code": "malformed_frontmatter",
                "message": "bare list item at top level: %r" % raw,
            })
            continue
        m = _KEY_RE.match(stripped)
        if not m:
            errors.append({
                "code": "malformed_frontmatter",
                "message": "line is not 'key: value': %r" % raw,
            })
            continue
        key, value = m.group(1), m.group(2).strip()
        if key in data:
            errors.append({
                "code": "duplicate_key",
                "message": "frontmatter key %r appears twice" % key,
            })
            continue
        if value == "":
            # Look ahead: the next non-blank line decides dict vs list.
            # We set both pendings and resolve on the first indented line:
            # a "- " line makes it a list, a "k: v" line makes it a dict.
            # To keep that decision simple, peek at the following lines.
            data[key] = None  # placeholder, replaced below
            pending_key = key
            # Peek forward from this line's position in the block.
            nxt = None
            for following in fm_lines[pos + 1:]:
                if following.strip():
                    nxt = following
                    break
            if nxt is not None and nxt.strip().startswith("- "):
                data[key] = []
                pending_list = key
            else:
                data[key] = {}
                pending_dict = key
            del pending_key
        elif value.startswith("["):
            parsed = _parse_inline_list(value, errors, key)
            data[key] = parsed if parsed is not None else value
        else:
            data[key] = _unquote(value)
    # A key left as None/placeholder means it had no indented content.
    for key, value in list(data.items()):
        if value is None:
            data[key] = []
    return data, body, errors
