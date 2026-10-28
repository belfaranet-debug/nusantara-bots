"""Grok Bot (own build) — skill + character loader, PHASE 2.

Zero-dependency, Python standard library only. The choice of Python
over Node is consistency: the Phase 1 memory service in this project
is Python + SQLite stdlib, so the loader shares its runtime, its
test runner (``unittest``), and its deployment story — one
interpreter, no ``npm install``, no lockfile, nothing to download
on a self-hosted server. That matters for the ownable/sellable bar:
an installable pack must load on the owner's own server with no
provider, no registry, and no network at install time.

Public API (import from ``loader``)::

    from loader import load_skills, load_skill, load_characters, load_character
    from loader import CHARACTERS_DIR, SKILL_SHELF_DIR

    skills = load_skills(SKILL_SHELF_DIR)        # -> list[Skill]
    characters = load_characters(CHARACTERS_DIR)  # -> list[Character]

Every loader returns ALL items it found, valid or not — nothing is
silently dropped. Check ``item.status == "valid"`` (or ``item.ok``)
and ``item.error_codes()`` before using an item in the runtime.
"""

from .characters import CHARACTERS_DIR, Character, load_character, load_characters
from .frontmatter import parse_frontmatter
from .skills import MAX_DESCRIPTION_CHARS, SKILL_SHELF_DIR, Skill, load_skill, load_skills

__all__ = [
    "CHARACTERS_DIR",
    "MAX_DESCRIPTION_CHARS",
    "SKILL_SHELF_DIR",
    "Character",
    "Skill",
    "load_character",
    "load_characters",
    "load_skill",
    "load_skills",
    "parse_frontmatter",
]
