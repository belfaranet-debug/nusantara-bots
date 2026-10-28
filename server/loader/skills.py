"""Skill loading and validation.

A skill is one folder containing ``SKILL.md``. The frontmatter must
carry ``name``, ``description`` and a version. The version may live
at the top level (``version: 1.0.0``) OR one level down
(``metadata:`` / ``version: 1.0.0``) — the real shelf uses the
``metadata.version`` form, so the loader accepts both and records
which one it found in ``version_source``. A skill with neither is
invalid (``missing_version``), not silently defaulted.

Validation rules (all must pass for status ``"valid"``):

* ``SKILL.md`` exists in the folder (else ``missing_skill_md``).
* Frontmatter parses (else the parser's error code).
* ``name`` is present and exactly equals the folder name
  (else ``missing_name`` / ``name_mismatch``).
* ``description`` is present and at most 1024 characters
  (else ``missing_description`` / ``description_too_long``).
* ``version`` is present and non-empty (else ``missing_version``).
* The body after the frontmatter is non-empty
  (else ``empty_body``).

A crew pack (a folder with ``README.md`` + ``characters/`` and no
``SKILL.md``, like ``crew-youtube-team``) is NOT a skill and is
reported as ``missing_skill_md`` — that is the honest result, not a
reason to invent a SKILL.md for it. The crew installer (a later
phase) is the component that reads crew packs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import parse_frontmatter

MAX_DESCRIPTION_CHARS = 1024

# Default skill shelf (read-only; this loader never writes to it).
SKILL_SHELF_DIR = Path(__file__).resolve().parents[2] / "grokbot" / "skill-pack"


@dataclass
class Skill:
    """One loaded skill folder, valid or not (never dropped)."""

    name: str
    description: str
    version: str
    version_source: str  # "version" | "metadata.version" | ""
    path: Path
    body: str
    status: str  # "valid" | "invalid"
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "valid"

    def error_codes(self) -> list:
        return [e["code"] for e in self.errors]


def load_skill(folder: Path) -> Skill:
    """Load and validate one skill folder. Never raises on bad input."""
    folder = Path(folder)
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return Skill(
            name=folder.name, description="", version="", version_source="",
            path=folder, body="", status="invalid",
            errors=[{"code": "missing_skill_md",
                     "message": "no SKILL.md in folder %s" % folder}],
        )
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Skill(
            name=folder.name, description="", version="", version_source="",
            path=folder, body="", status="invalid",
            errors=[{"code": "unreadable_skill_md", "message": str(exc)}],
        )
    data, body, errors = parse_frontmatter(text)
    if any(e["code"] in ("missing_frontmatter", "unclosed_frontmatter")
           for e in errors):
        # No frontmatter to validate against: report the root cause
        # only, not a cascade of missing-field errors.
        return Skill(
            name=folder.name, description="", version="", version_source="",
            path=folder, body="", status="invalid", errors=errors)
    name = data.get("name", "") if isinstance(data.get("name", ""), str) else ""
    description = data.get("description", "") if isinstance(data.get("description", ""), str) else ""
    version, version_source = "", ""
    if isinstance(data.get("version"), str) and data["version"]:
        version, version_source = data["version"], "version"
    elif isinstance(data.get("metadata"), dict) and isinstance(data["metadata"].get("version"), str) \
            and data["metadata"]["version"]:
        version, version_source = data["metadata"]["version"], "metadata.version"
    if not name:
        errors.append({"code": "missing_name",
                       "message": "frontmatter has no 'name'"})
    elif name != folder.name:
        errors.append({"code": "name_mismatch",
                       "message": "frontmatter name %r != folder name %r" % (name, folder.name)})
    if not description:
        errors.append({"code": "missing_description",
                       "message": "frontmatter has no 'description'"})
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.append({"code": "description_too_long",
                       "message": "description is %d chars, limit is %d"
                                  % (len(description), MAX_DESCRIPTION_CHARS)})
    if not version:
        errors.append({"code": "missing_version",
                       "message": "frontmatter has neither 'version' nor 'metadata.version'"})
    if not body:
        errors.append({"code": "empty_body",
                       "message": "no body after the frontmatter"})
    return Skill(
        name=name or folder.name, description=description, version=version,
        version_source=version_source, path=folder, body=body,
        status="valid" if not errors else "invalid", errors=errors,
    )


def load_skills(shelf_dir: Path) -> list:
    """Load every sub-folder of ``shelf_dir`` as a skill, sorted by name.

    Plain files in the shelf (e.g. ``*.zip`` bundles, loose ``*.md``)
    are ignored: only directories are skill-folder candidates.
    """
    shelf_dir = Path(shelf_dir)
    if not shelf_dir.is_dir():
        return []
    return [load_skill(d) for d in sorted(
        (p for p in shelf_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name)]
