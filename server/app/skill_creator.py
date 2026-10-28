"""Skill creator (0.10.10): model-authored skills for the shelf.

A skill is one folder on the skill shelf containing ``SKILL.md``.
The model writes a skill by calling the ``create_skill`` tool; the
server validates it (loader rules + Hermes-style lint) and only
then writes it to the shelf. A skill that fails validation is
never written. Skills never carry credentials (BYO-API by design:
a skill declares what it needs, never a key).

Validation (hard — a violation rejects the skill, nothing is
written):

* ``name`` is present, lowercase-hyphenated (``^[a-z0-9][a-z0-9-]*$``),
  1-40 chars, and does not already exist on the shelf.
* ``description`` is present, one line, no double quotes (the
  frontmatter writer renders it unquoted), at most 1024 chars.
* ``description`` carries no marketing word (Hermes linter list):
  powerful, comprehensive, seamless, advanced, cutting-edge,
  state-of-the-art, revolutionary, robust.
* ``version`` is ``1.0.0`` for a new skill (the learn loop's
  CREATE_VERSION; an update is a patch bump, not a create).
* The body after the frontmatter is non-empty, and the whole
  SKILL.md text fits in 60_000 UTF-8 bytes (the server's
  ``MAX_BODY`` is 64 KiB — a larger skill could not arrive).
* No credential assignment line (``api_key`` / ``secret`` /
  ``password`` / ``token`` / ``access_key`` with a value) anywhere
  in the text — skills never carry a key.
* The written file must load ``valid`` under ``loader.skills``
  (the same loader POST /api/characters validates against);
  otherwise it is removed and the call fails.

Advisory lint (warnings — reported, never blocking, Hermes
``skill_linter.py`` pattern):

* The body mentions raw shell utilities (grep, rg, cat, head,
  tail, sed, awk, find, ls) as bare commands — the skill should
  describe the outcome and let the model use its own file/shell
  tool, not steer it to a raw shell call.
* The body exceeds the ~24_000-char soft budget (Hermes:
  SKILL.md rides in context for every later call, so body size
  is paid per turn).

Assigning a skill to a bot rewrites only the ``skills:`` list in
that bot's character file (frontmatter); the prompt body and the
other frontmatter fields (role, tools, memory_scope) are preserved
verbatim. The skill must be a valid folder on the shelf — the
same check POST /api/characters applies to an explicit skill
list. (The two focus-default skills, team-memory and
bot-live-presence, are NOT shelf folders, so they cannot be
assigned this way; that matches the existing create-bot rule.)
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from loader.frontmatter import parse_frontmatter
from loader.skills import MAX_DESCRIPTION_CHARS, load_skill, load_skills
from loader.characters import load_character

CREATE_VERSION = "1.0.0"

# New-skill text must fit the server's 64 KiB MAX_BODY with JSON
# overhead to spare.
MAX_SKILL_TEXT_BYTES = 60_000
# Hermes soft budget: a SKILL.md body this long rides in context
# for every later call of the session.
BODY_SOFT_BUDGET_CHARS = 24_000
MAX_SKILL_NAME_CHARS = 40
MAX_TAGS = 10
MAX_TAG_CHARS = 30

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Hermes skill_linter.py: marketing words banned from the
# description (the routing prompt must say what the skill does,
# not sell it). Whole-word, case-insensitive.
MARKETING_WORDS = (
    "powerful", "comprehensive", "seamless", "advanced",
    "cutting-edge", "state-of-the-art", "revolutionary", "robust",
)

# Hermes skill_linter.py: shell utilities already wrapped as native
# tools; naming them in prose steers the model to a raw shell call.
# In this app the file/shell tool is vm_exec, so the finding is
# advisory: describe the outcome, do not script raw shell utils.
_SHELL_UTILS = (
    "grep", "rg", "cat", "head", "tail", "sed", "awk", "find", "ls",
)

# A line that assigns a credential value — a skill must never
# carry one (BYO-API by design). Line-anchored so prose that
# merely mentions the word (e.g. "never ask for a password", or a
# definition line like "token: the session token") does not trip
# it. ``token`` is left out of the line set on purpose: it is a
# common prose word in definitions. ``api_key`` / ``access_key``
# are almost never prose, so they are also caught mid-sentence.
_CREDENTIAL_LINE_RE = re.compile(
    r"(?im)^[ \t]*(api[_-]?key|secret|password|access[_-]?key)"
    r"[ \t]*[:=][ \t]*\S+"
)
_CREDENTIAL_ANYWHERE_RE = re.compile(
    r"(?i)(?<![a-z0-9_])(api[_-]?key|access[_-]?key)[ \t]*[:=][ \t]*\S+"
)


def _marketing_hits(description: str) -> list:
    """Marketing words present in ``description`` (whole-word)."""
    lowered = description.lower()
    return [w for w in MARKETING_WORDS
            if re.search(r"\b%s\b" % re.escape(w), lowered)]


def _shell_util_hits(body: str) -> list:
    """Raw shell utilities mentioned as bare words in ``body``."""
    lowered = body.lower()
    return [u for u in _SHELL_UTILS
            if re.search(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(u),
                         lowered)]


def lint_skill_text(name, description, version, body):
    """Validate a proposed skill. Returns ``(errors, warnings)``.

    ``errors`` is non-empty when the skill must be rejected (nothing
    is written). ``warnings`` is advisory only (Hermes linter
    pattern) and never blocks. Never raises on bad input.
    """
    errors: list = []
    warnings: list = []
    name = name if isinstance(name, str) else ""
    description = description if isinstance(description, str) else ""
    version = version if isinstance(version, str) else ""
    body = body if isinstance(body, str) else ""

    if not name:
        errors.append("name is required (lowercase-hyphenated, e.g. 'meeting-notes')")
    elif len(name) > MAX_SKILL_NAME_CHARS:
        errors.append("name is %d chars, limit is %d"
                      % (len(name), MAX_SKILL_NAME_CHARS))
    elif not _SKILL_NAME_RE.match(name):
        errors.append(
            "name %r must be lowercase-hyphenated ([a-z0-9-], must start "
            "with a letter or digit)" % name)

    if not description.strip():
        errors.append("description is required (one sentence: what the skill does)")
    else:
        if len(description) > MAX_DESCRIPTION_CHARS:
            errors.append("description is %d chars, limit is %d"
                          % (len(description), MAX_DESCRIPTION_CHARS))
        if "\n" in description or "\r" in description:
            errors.append("description must be a single line (no line breaks)")
        if '"' in description:
            errors.append(
                "description must not contain double quotes "
                "(rephrase without them)")
        hits = _marketing_hits(description)
        if hits:
            errors.append(
                "description contains marketing word(s): %s — rewrite "
                "plainly, one concrete sentence about what the skill does"
                % ", ".join(hits))

    if version and version != CREATE_VERSION:
        errors.append(
            "new skills start at version %r (got %r)"
            % (CREATE_VERSION, version))

    if not body.strip():
        errors.append("body is required (the skill instructions after the frontmatter)")
    else:
        if len(body) > BODY_SOFT_BUDGET_CHARS:
            warnings.append(
                "body is %d chars, over the ~%d-char soft budget — "
                "consider shortening it; the whole file rides in context "
                "for every later call"
                % (len(body), BODY_SOFT_BUDGET_CHARS))
        if _shell_util_hits(body):
            warnings.append(
                "body mentions raw shell utilit(ies): %s — describe the "
                "outcome instead of scripting raw shell calls"
                % ", ".join(_shell_util_hits(body)))

    text = build_skill_md(name or "skill",
                          description or "x",
                          body or "x",
                          version=version or CREATE_VERSION)
    if len(text.encode("utf-8")) > MAX_SKILL_TEXT_BYTES:
        errors.append(
            "skill text is %d bytes, limit is %d (it must fit the "
            "server's request body)"
            % (len(text.encode("utf-8")), MAX_SKILL_TEXT_BYTES))
    if _CREDENTIAL_LINE_RE.search(text) or _CREDENTIAL_ANYWHERE_RE.search(text):
        errors.append(
            "skill text assigns a credential (api_key / secret / "
            "password / access_key) — skills never carry a key; declare "
            "what the skill needs, never the value")
    return errors, warnings


def build_skill_md(name, description, body, version=CREATE_VERSION,
                   tags=None, category=None):
    """Render the canonical SKILL.md text for a skill."""
    lines = ["---",
             "name: %s" % name,
             "description: %s" % description.strip(),
             "version: %s" % (version or CREATE_VERSION)]
    if category:
        lines.append("category: %s" % str(category).strip())
    if tags:
        lines.append("tags: [%s]" % ", ".join(str(t).strip() for t in tags))
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


def skill_to_json(skill):
    """Public JSON shape for a loaded skill (mirrors _character_json)."""
    return {
        "name": skill.name,
        "description": skill.description,
        "version": skill.version,
        "status": skill.status,
        "errors": [e.get("code", "") for e in (skill.errors or [])],
    }


def list_shelf_skills(shelf_dir):
    """Every skill folder on the shelf, as public JSON, sorted by name."""
    return [skill_to_json(s) for s in load_skills(Path(shelf_dir))]


def create_skill_file(shelf_dir, name, description, body,
                      version=None, tags=None, category=None):
    """Validate + write a new skill to the shelf.

    Returns ``(skill, warnings)``. Raises ``ValueError`` (with the
    violation list) when validation fails — nothing is written —
    and ``FileExistsError`` when the skill already exists. The
    written file is re-loaded through ``loader.skills``; if it
    does not load valid it is removed and ``ValueError`` is raised.
    """
    version = version or CREATE_VERSION
    errors, warnings = lint_skill_text(name, description, version, body)
    if errors:
        raise ValueError("; ".join(errors))
    shelf_dir = Path(shelf_dir)
    folder = shelf_dir / name
    if folder.exists():
        raise FileExistsError("skill %r already exists on the shelf" % name)
    text = build_skill_md(name, description, body, version=version,
                          tags=tags, category=category)
    folder.mkdir(parents=True, exist_ok=False)
    skill_path = folder / "SKILL.md"
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(folder),
                                        prefix=".new-skill-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, skill_path)
        tmp_name = None
    except OSError:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise ValueError("could not write skill file")
    skill = load_skill(folder)
    if not skill.ok:
        try:
            shutil.rmtree(folder)
        except OSError:
            pass
        raise ValueError(
            "created skill failed validation: %s"
            % ", ".join(skill.error_codes()))
    return skill, warnings


def _rewrite_character_skills(path, new_skills):
    """Rewrite only the ``skills:`` list of a character file.

    The prompt body and every other frontmatter field (role, tools,
    memory_scope) are preserved verbatim. Returns the file text.
    """
    text = path.read_text(encoding="utf-8")
    data, body, errors = parse_frontmatter(text)
    if errors:
        raise ValueError(
            "character file has invalid frontmatter: %s"
            % "; ".join(e.get("message", e.get("code", "")) for e in errors))
    name = data.get("name", "")
    role = data.get("role", "")
    tools = data.get("tools", [])
    memory_scope = data.get("memory_scope", "")
    if not isinstance(name, str) or '"' in name or "\n" in name:
        raise ValueError("character name cannot be re-rendered safely")
    if not isinstance(role, str) or '"' in role or "\n" in role:
        raise ValueError("character role cannot be re-rendered safely")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError("character tools are not a list")
    if not isinstance(memory_scope, str) or not memory_scope:
        raise ValueError("character memory_scope is missing")
    lines = ["---",
             'name: "%s"' % name,
             'role: "%s"' % role,
             "skills:"]
    for skill in new_skills:
        lines.append("  - %s" % skill)
    if tools:
        lines.append("tools:")
        for tool in tools:
            lines.append("  - %s" % tool)
    else:
        lines.append("tools: []")
    lines.append("memory_scope: %s" % memory_scope)
    lines += ["---", "", body.rstrip(), ""]
    return "\n".join(lines)


def assign_skill_to_character(characters_dir, shelf_dir, char_id, skill,
                              remove=False):
    """Assign (or remove) a skill on a bot's character file.

    Returns ``(character, changed, note)``. ``changed`` is False
    when the skill was already (un)assigned — idempotent, not an
    error. Raises ``FileNotFoundError`` for an unknown bot and
    ``ValueError`` for an unknown/invalid skill or an unwritable
    character file. The rewritten file is re-loaded; if it does
    not load valid, the original text is restored and ``ValueError``
    is raised.
    """
    if not isinstance(char_id, str) or not _SKILL_NAME_RE.match(char_id):
        raise ValueError("unknown character %r" % (char_id,))
    if not isinstance(skill, str) or not _SKILL_NAME_RE.match(skill):
        raise ValueError(
            "skill %r must be lowercase-hyphenated" % (skill,))
    path = Path(characters_dir) / ("%s.md" % char_id)
    if not path.is_file():
        raise FileNotFoundError("unknown character %r" % char_id)
    if not remove:
        known = {s.name for s in load_skills(Path(shelf_dir))
                 if s.status == "valid"}
        if skill not in known:
            raise ValueError(
                "unknown skill %r — known skills: %s"
                % (skill, ", ".join(sorted(known)) or "(none)"))
    original = path.read_text(encoding="utf-8")
    data, _body, errors = parse_frontmatter(original)
    if errors:
        raise ValueError(
            "character file has invalid frontmatter: %s"
            % "; ".join(e.get("message", e.get("code", "")) for e in errors))
    current = data.get("skills", [])
    if not isinstance(current, list):
        raise ValueError("character skills are not a list")
    if remove:
        if skill not in current:
            return load_character(path), False, \
                "bot does not have skill %r" % skill
        new_skills = [s for s in current if s != skill]
    else:
        if skill in current:
            return load_character(path), False, \
                "bot already has skill %r" % skill
        new_skills = list(current) + [skill]
    text = _rewrite_character_skills(path, new_skills)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(Path(characters_dir)),
                                        prefix=".skill-%s-" % char_id,
                                        suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise ValueError("could not write character file")
    updated = load_character(path)
    if not updated.ok:
        try:
            path.write_text(original, encoding="utf-8")
        except OSError:
            pass
        raise ValueError(
            "updated character failed validation: %s"
            % ", ".join(updated.error_codes()))
    return updated, True, \
        ("skill %r removed" % skill if remove else "skill %r assigned" % skill)
