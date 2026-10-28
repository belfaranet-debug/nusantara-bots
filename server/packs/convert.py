"""Convert existing shelf packs to the Dots-style pack format.

Builds a manifest.json for a pack folder from the files that are already
there (SKILL.md frontmatter for skills, README.md + characters/ for crews,
SOURCE.md for import origin + license) and writes it next to them.

The converter is ADDITIVE and IDEMPOTENT:

* it only writes manifest.json — never touches SKILL.md, README.md,
  characters/, SOURCE.md or the .zip snapshots;
* running it twice produces byte-identical manifest.json files;
* the skill loader ignores manifest.json, so conversion cannot break
  loading (the loader keeps reading SKILL.md only).

Usage (from ~/workspace/grokbot-own):

    python3 -m packs.convert --shelf ../grokbot/skill-pack        # dry run
    python3 -m packs.convert --shelf ../grokbot/skill-pack --write
    python3 -m packs.convert --shelf ../grokbot/skill-pack --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .pack import DEFAULT_REVIEW_ACTIONS, PackResult, validate_manifest

_MANIFEST_KEYS = [
    "name", "kind", "version", "description", "skills", "characters",
    "source", "mcp", "review", "requires",
]

_ORIGIN_RE = re.compile(r"^-\s*Origin repo:\s*(\S+)\s*$", re.MULTILINE)
_LICENSE_RE = re.compile(r"^-\s*License:\s*([A-Za-z0-9.+\-]+)", re.MULTILINE)


def parse_source_md(path: Path) -> dict:
    """Read origin + license out of a SOURCE.md file.

    Returns {"origin": <url or "">, "license": <SPDX or "see SOURCE.md">}.
    The license line in our SOURCE.md files reads like
    "- License: MIT, verified from ..." so only the first SPDX-ish token
    is taken. A token with no uppercase letter and no digit (e.g. "see")
    is not a license identifier, so it falls back to "see SOURCE.md" and
    the validator accepts it as long as the file exists.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    origin = _ORIGIN_RE.search(text)
    license_ = _LICENSE_RE.search(text)
    token = license_.group(1) if license_ else ""
    if token and not re.search(r"[A-Z0-9]", token):
        token = ""
    return {
        "origin": origin.group(1) if origin else "",
        "license": token if token else "see SOURCE.md",
    }


def _source_for(folder: Path) -> dict:
    source_md = folder / "SOURCE.md"
    if not source_md.is_file():
        return {"origin": "own-build"}
    parsed = parse_source_md(source_md)
    if not parsed["origin"]:
        # SOURCE.md exists but carries no parseable origin line: keep the
        # file as the source of truth rather than inventing a URL.
        return {"origin": "see SOURCE.md", "license": parsed["license"]}
    return parsed


def _crew_description(folder: Path) -> str:
    """First non-heading, non-empty line of the crew README.md."""
    text = (folder / "README.md").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def build_manifest(folder: Path) -> tuple[dict | None, list]:
    """Build a manifest dict for one pack folder.

    Returns (manifest, errors). On error manifest is None and errors
    lists {code, message} dicts. Never raises on bad input.
    """
    from loader.skills import load_skill

    folder = Path(folder)
    errors: list = []

    skill_md = folder / "SKILL.md"
    chars_dir = folder / "characters"
    template_json = folder / "template.json"
    if skill_md.is_file():
        kind = "skill"
    elif chars_dir.is_dir():
        kind = "crew"
    elif template_json.is_file():
        # A staged template pack: the manifest mirrors template.json
        # (name/version/description/characters/skills). The
        # converter never rewrites template.json itself — it only
        # rebuilds manifest.json from it, same as for skill/crew.
        try:
            definition = json.loads(
                template_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return None, [{"code": "bad_template_json",
                           "message": "template.json is unreadable: %s"
                                      % exc}]
        if not isinstance(definition, dict):
            return None, [{"code": "bad_template_json",
                           "message": "template.json must contain a JSON "
                                      "object"}]
        # Connections the template brings become manifest MCP
        # servers — always DISABLED (enabled: false); a template
        # pack's connections start disabled until a human approves
        # them, and the pack validator rejects a template manifest
        # that claims otherwise.
        servers = {}
        for conn in (definition.get("connections") or []):
            if not isinstance(conn, dict):
                continue
            entry = {"type": conn.get("type"), "enabled": False}
            if conn.get("type") == "http":
                entry["url"] = conn.get("url")
            elif conn.get("type") == "stdio":
                entry["command"] = conn.get("command")
            if conn.get("tools"):
                entry["tools"] = list(conn["tools"])
            if isinstance(conn.get("name"), str):
                servers[conn["name"]] = entry
        # The manifest mirrors template.json: skills is the union
        # of the top-level list and every character's skills (the
        # same union the importer stages).
        top_skills = list(definition.get("skills") or [])
        for char in (definition.get("characters") or []):
            if isinstance(char, dict):
                for skill in (char.get("skills") or []):
                    if skill not in top_skills:
                        top_skills.append(skill)
        manifest = {
            "name": definition.get("name") or folder.name,
            "kind": "template",
            "version": definition.get("version") or "1.0.0",
            "description": definition.get("description") or "",
            "skills": top_skills,
            "characters": [c.get("name") for c in
                           (definition.get("characters") or [])
                           if isinstance(c, dict)
                           and isinstance(c.get("name"), str)],
            "source": definition.get("source") or {"origin": "own-build"},
            "mcp": {"servers": servers},
            "review": {"required_before": list(DEFAULT_REVIEW_ACTIONS)},
            "requires": [],
        }
        manifest = {key: manifest[key] for key in _MANIFEST_KEYS}
        result = validate_manifest(manifest, folder)
        if not result.ok:
            return None, result.errors
        return manifest, []
    else:
        return None, [{"code": "unknown_pack_kind",
                       "message": "folder %s has neither SKILL.md, "
                                  "characters/, nor template.json"
                                  % folder}]

    if kind == "skill":
        skill = load_skill(folder)
        if not skill.ok:
            return None, [{"code": "skill_invalid",
                           "message": "SKILL.md failed loader validation: %s"
                                      % (", ".join(skill.error_codes()),)}]
        name = skill.name
        description = skill.description
        version = skill.version or "1.0.0"
        skills = [name]
        characters: list = []
    else:
        name = folder.name
        characters = sorted(p.stem for p in chars_dir.glob("*.md"))
        if not characters:
            return None, [{"code": "crew_no_character_files",
                           "message": "characters/ has no .md files"}]
        if not (folder / "README.md").is_file():
            return None, [{"code": "missing_crew_readme",
                           "message": "a crew pack needs README.md"}]
        description = _crew_description(folder)
        if not description:
            return None, [{"code": "missing_description",
                           "message": "README.md has no description line"}]
        version = "1.0.0"
        # Crew character files are plain markdown (no frontmatter), so the
        # crew declares no shelf-skill dependencies here. The installer
        # wires skills to characters separately; the manifest must not
        # invent dependencies the pack files do not state.
        skills = []

    manifest = {
        "name": name,
        "kind": kind,
        "version": version,
        "description": description,
        "skills": skills,
        "characters": characters,
        "source": _source_for(folder),
        "mcp": {"servers": {}},
        "review": {"required_before": list(DEFAULT_REVIEW_ACTIONS)},
        "requires": [],
    }
    # Keep keys in the documented order.
    manifest = {key: manifest[key] for key in _MANIFEST_KEYS}
    result = validate_manifest(manifest, folder)
    if not result.ok:
        return None, result.errors
    return manifest, []


def manifest_bytes(manifest: dict) -> bytes:
    """Canonical serialisation: indent 2, trailing newline, stable order."""
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def convert_folder(folder: Path, write: bool = False) -> PackResult:
    """Build (and optionally write) the manifest for one pack folder."""
    folder = Path(folder)
    manifest, errors = build_manifest(folder)
    if errors:
        return PackResult(name=folder.name, ok=False, errors=errors)
    wrote = False
    if write:
        target = folder / "manifest.json"
        data = manifest_bytes(manifest)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
            wrote = True
    return PackResult(name=folder.name, ok=True, errors=[],
                      manifest={**manifest, "_wrote": wrote} if write else manifest)


def convert_shelf(shelf_dir: Path, write: bool = False) -> list:
    """Convert every pack folder in a shelf. Returns one PackResult each."""
    shelf_dir = Path(shelf_dir)
    results = []
    if not shelf_dir.is_dir():
        return results
    for folder in sorted(p for p in shelf_dir.iterdir()
                         if p.is_dir() and not p.name.startswith(".")):
        results.append(convert_folder(folder, write=write))
    return results


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shelf", required=True, help="skill shelf directory")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true",
                       help="write manifest.json into each pack folder")
    group.add_argument("--check", action="store_true",
                       help="validate existing manifest.json files, do not write")
    args = parser.parse_args(argv)

    shelf = Path(args.shelf)
    if not shelf.is_dir():
        print("shelf not found: %s" % shelf, file=sys.stderr)
        return 2

    if args.check:
        from .pack import validate_shelf
        results = validate_shelf(shelf)
        mode = "check"
    else:
        results = convert_shelf(shelf, write=args.write)
        mode = "write" if args.write else "dry-run"

    failed = 0
    for result in results:
        if result.ok:
            extra = ""
            if args.write and result.manifest and result.manifest.get("_wrote"):
                extra = " (wrote manifest.json)"
            print("OK   %s%s" % (result.name, extra))
        else:
            failed += 1
            codes = ", ".join(e["code"] for e in result.errors)
            print("FAIL %s: %s" % (result.name, codes))
            for err in result.errors:
                print("     - %s" % err["message"])
    print("%s: %d ok, %d failed (%s)"
          % (shelf, len(results) - failed, failed, mode))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
