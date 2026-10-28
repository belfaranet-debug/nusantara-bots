"""Character loading and validation.

A character is one markdown file with YAML-ish frontmatter plus a
prompt body. This is what turns a chat prompt into an employee: the
file does not just say how the character talks, it declares what the
character can do (``skills``), what it may touch (``tools``), and
what it remembers (``memory_scope``). The runtime (a later phase)
binds exactly those declarations — a character cannot silently gain
a skill, a tool, or a memory scope that its file does not declare.

Frontmatter fields (all required):

* ``name`` — display name, e.g. ``YT Strategist``. Unlike skills,
  this is NOT required to equal the file name: the file name is a
  slug (``yt-strategist.md``) and the name is human-facing.
* ``role`` — one line, what the character owns, e.g.
  ``Channel strategy, positioning, and content calendar``.
* ``skills`` — list of skill names (inline ``[a, b]`` or block
  ``- a`` form). May be empty (``skills: []``) but must be a list.
* ``tools`` — list of tool names the character may use, same list
  forms. May be empty but must be a list.
* ``memory_scope`` — exactly one of ``private``, ``team`` or
  ``private+team`` (see ../MEMORY.md: private = the character's own
  notes only; team = the shared team memory; private+team = both).

The body after the frontmatter is the persona prompt: instructions,
working rules, and output format. An empty body is invalid.

Validation rules (all must pass for status ``"valid"``):

* Frontmatter parses (else the parser's error code).
* ``name`` / ``role`` present and non-empty.
* ``skills`` / ``tools`` present and parsed as lists
  (a scalar or an unclosed ``[`` gives ``malformed_list`` /
  ``not_a_list``).
* ``memory_scope`` present and one of the three allowed values
  (else ``missing_memory_scope`` / ``invalid_memory_scope``).
* Prompt body non-empty (else ``empty_body``).

Note on the legacy crew format: the crew-youtube-team pack's
``characters/*.md`` files use a ``# Name`` / ``Role: ...`` / ``---``
header, NOT frontmatter. They therefore fail with
``missing_frontmatter`` under this format — reported as-is. Those
files are not converted in place; a later crew-installer phase may
offer an explicit, owner-approved import that writes NEW files in
this format into the runtime character directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import parse_frontmatter

ALLOWED_MEMORY_SCOPES = ("private", "team", "private+team")

# Runtime character directory (see ../characters/README.md and
# loader/README.md). Later phases (app shell, crew installer) MUST
# use exactly this path.
CHARACTERS_DIR = Path(__file__).resolve().parents[1] / "characters"


@dataclass
class Character:
    """One loaded character file, valid or not (never dropped)."""

    name: str
    role: str
    skills: list
    tools: list
    memory_scope: str
    prompt: str
    path: Path
    status: str  # "valid" | "invalid"
    errors: list = field(default_factory=list)
    soul: str = ""  # standing instructions (SOUL), from <stem>.soul.md

    @property
    def ok(self) -> bool:
        return self.status == "valid"

    def error_codes(self) -> list:
        return [e["code"] for e in self.errors]


def _list_field(data: dict, key: str, errors: list):
    """Return the list value for ``key`` or None (appending an error)."""
    if key not in data:
        errors.append({"code": "missing_%s" % key,
                       "message": "frontmatter has no %r" % key})
        return None
    value = data[key]
    if not isinstance(value, list):
        errors.append({"code": "not_a_list",
                       "message": "frontmatter %r must be a list, got %r" % (key, value)})
        return None
    return value


def _load_soul(path: Path) -> str:
    """Read the sibling ``<stem>.soul.md`` file, if present.

    The soul is the character's standing instructions
    (personality, tone, working rules) for the app's layered
    system-prompt builder. It lives beside the character file
    so it travels with it, but it is NOT a character itself
    (load_characters skips ``*.soul.md``). Returns "" when
    there is no soul file.
    """
    soul_path = path.with_name(path.stem + ".soul.md")
    try:
        return soul_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def load_character(path: Path) -> Character:
    """Load and validate one character file. Never raises on bad input."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Character(
            name=path.stem, role="", skills=[], tools=[], memory_scope="",
            prompt="", path=path, status="invalid",
            errors=[{"code": "unreadable_character", "message": str(exc)}],
        )
    data, body, errors = parse_frontmatter(text)
    if any(e["code"] in ("missing_frontmatter", "unclosed_frontmatter")
           for e in errors):
        # No frontmatter to validate against: report the root cause
        # only, not a cascade of missing-field errors.
        return Character(
            name=path.stem, role="", skills=[], tools=[], memory_scope="",
            prompt="", path=path, status="invalid", errors=errors)
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    role = data.get("role") if isinstance(data.get("role"), str) else ""
    skills = _list_field(data, "skills", errors)
    tools = _list_field(data, "tools", errors)
    scope = data.get("memory_scope") if isinstance(data.get("memory_scope"), str) else ""
    if not name:
        errors.append({"code": "missing_name",
                       "message": "frontmatter has no 'name'"})
    if not role:
        errors.append({"code": "missing_role",
                       "message": "frontmatter has no 'role'"})
    if not scope:
        errors.append({"code": "missing_memory_scope",
                       "message": "frontmatter has no 'memory_scope'"})
    elif scope not in ALLOWED_MEMORY_SCOPES:
        errors.append({"code": "invalid_memory_scope",
                       "message": "memory_scope %r not in %s" % (scope, list(ALLOWED_MEMORY_SCOPES))})
    if not body:
        errors.append({"code": "empty_body",
                       "message": "no prompt body after the frontmatter"})
    return Character(
        name=name or path.stem, role=role or "",
        skills=skills if skills is not None else [],
        tools=tools if tools is not None else [],
        memory_scope=scope, prompt=body, path=path,
        status="valid" if not errors else "invalid", errors=errors,
        soul=_load_soul(path),
    )


def load_characters(characters_dir: Path) -> list:
    """Load every ``*.md`` file in ``characters_dir``, sorted by name.

    ``README.md`` in the directory is documentation, not a character,
    and is skipped. A missing directory loads as an empty list.
    """
    characters_dir = Path(characters_dir)
    if not characters_dir.is_dir():
        return []
    return [load_character(p) for p in sorted(
        (p for p in characters_dir.glob("*.md")
         if p.name != "README.md" and not p.name.endswith(".soul.md")),
        key=lambda p: p.name)]
