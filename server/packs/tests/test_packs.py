"""Tests for the Dots-style plugin-pack format (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s packs/tests -v

Tests use temp folders only, except test_real_shelf_* which read the real
shelf (read-only unless the converter was run with --write first; they
never write). No network, no model calls, no credentials.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packs.convert import build_manifest, convert_folder, convert_shelf, parse_source_md
from packs.pack import (
    DEFAULT_REVIEW_ACTIONS,
    find_secret_keys,
    validate_manifest,
    validate_pack,
    validate_shelf,
)

# The real shelf, resolved the same way the loader does (workspace root).
SHELF_DIR = Path(__file__).resolve().parents[3] / "grokbot" / "skill-pack"

SKILL_MD = """\
---
name: {name}
description: {description}
version: 1.0.0
---

# {title}

Body text for the skill.
"""

SOURCE_MD = """\
# Source — {name}

- Origin repo: https://github.com/example/{name}
- License: MIT, verified from the repo page.
- Import date: 2026-10-25
"""


def make_skill_folder(base: Path, name: str = "demo-skill",
                      description: str = "A demo skill.") -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        SKILL_MD.format(name=name, description=description,
                        title=name.replace("-", " ").title()),
        encoding="utf-8")
    return folder


def make_crew_folder(base: Path, name: str = "demo-crew") -> Path:
    folder = base / name
    (folder / "characters").mkdir(parents=True)
    (folder / "README.md").write_text(
        "# Crew Pack: Demo Crew\n\nA demo crew of two characters.\n",
        encoding="utf-8")
    (folder / "characters" / "alpha.md").write_text("# Alpha\n\nDoes A.\n",
                                                    encoding="utf-8")
    (folder / "characters" / "beta.md").write_text("# Beta\n\nDoes B.\n",
                                                   encoding="utf-8")
    return folder


def valid_manifest(folder: Path, **overrides) -> dict:
    manifest, errors = build_manifest(folder)
    assert not errors, errors
    manifest.update(overrides)
    return manifest


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="packs-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_skill_manifest_ok(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        result = validate_manifest(manifest, folder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.manifest["kind"], "skill")
        self.assertEqual(result.manifest["skills"], ["demo-skill"])
        self.assertEqual(result.manifest["characters"], [])

    def test_valid_crew_manifest_ok(self):
        folder = make_crew_folder(self.tmp)
        manifest = valid_manifest(folder)
        result = validate_manifest(manifest, folder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.manifest["kind"], "crew")
        self.assertEqual(result.manifest["characters"], ["alpha", "beta"])
        self.assertEqual(result.manifest["skills"], [])

    def test_name_mismatch(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder, name="other-name")
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("name_mismatch", result.error_codes())

    def test_bad_kind(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder, kind="widget")
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_kind", result.error_codes())

    def test_description_too_long(self):
        folder = make_skill_folder(self.tmp, description="x" * 1025)
        # build_manifest fails on the loader side first; validate the
        # manifest rule directly with a hand-built manifest instead.
        folder2 = make_skill_folder(self.tmp, name="demo-skill-2")
        manifest = valid_manifest(folder2)
        manifest["description"] = "x" * 1025
        result = validate_manifest(manifest, folder2)
        self.assertFalse(result.ok)
        self.assertIn("description_too_long", result.error_codes())

    def test_review_missing_default(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["review"] = {"required_before": ["send", "publish"]}
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("review_missing_defaults", result.error_codes())

    def test_review_extra_actions_allowed(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["review"]["required_before"] = (
            list(DEFAULT_REVIEW_ACTIONS) + ["publish-to-store"])
        result = validate_manifest(manifest, folder)
        self.assertTrue(result.ok, result.errors)

    def test_secret_key_in_mcp_rejected(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["mcp"] = {"servers": {"gh": {
            "type": "http", "url": "https://example.invalid/mcp",
            "oauth": {"client_id": "abc", "client_secret": "shh"}}}}
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("secret_in_manifest", result.error_codes())

    def test_secret_key_top_level_rejected(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["api_key"] = "not-a-real-key"
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("secret_in_manifest", result.error_codes())

    def test_find_secret_keys(self):
        found = find_secret_keys({
            "ok": 1,
            "mcp": {"servers": {"x": {"oauth": {"client_secret": "v"}}}},
            "nested": [{"token": "v"}, {"fine": True}],
        })
        self.assertEqual(sorted(found), [
            "mcp.servers.x.oauth.client_secret",
            "nested[0].token",
        ])
        self.assertEqual(find_secret_keys({"name": "x", "mcp": {"servers": {}}}), [])

    def test_skill_missing_skill_md(self):
        folder = self.tmp / "empty-skill"
        folder.mkdir()
        manifest = {"name": "empty-skill", "kind": "skill", "version": "1.0.0",
                    "description": "d", "skills": ["empty-skill"],
                    "characters": [], "source": {"origin": "own-build"},
                    "mcp": {"servers": {}},
                    "review": {"required_before": list(DEFAULT_REVIEW_ACTIONS)},
                    "requires": []}
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("missing_skill_md", result.error_codes())

    def test_skill_with_invalid_skill_md(self):
        folder = self.tmp / "bad-skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text("no frontmatter at all\n",
                                         encoding="utf-8")
        manifest = {"name": "bad-skill", "kind": "skill", "version": "1.0.0",
                    "description": "d", "skills": ["bad-skill"],
                    "characters": [], "source": {"origin": "own-build"},
                    "mcp": {"servers": {}},
                    "review": {"required_before": list(DEFAULT_REVIEW_ACTIONS)},
                    "requires": []}
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("skill_invalid", result.error_codes())

    def test_crew_character_listed_but_missing(self):
        folder = make_crew_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["characters"] = ["alpha", "gamma"]
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("character_missing", result.error_codes())

    def test_crew_without_readme(self):
        folder = make_crew_folder(self.tmp)
        (folder / "README.md").unlink()
        manifest, errors = build_manifest(folder)
        self.assertIsNone(manifest)
        self.assertIn("missing_crew_readme", [e["code"] for e in errors])

    def test_missing_manifest_file(self):
        folder = make_skill_folder(self.tmp)
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("missing_manifest", result.error_codes())

    def test_bad_manifest_json(self):
        folder = make_skill_folder(self.tmp)
        (folder / "manifest.json").write_text("{not json", encoding="utf-8")
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_manifest_json", result.error_codes())

    def test_mcp_server_needs_url_or_command(self):
        folder = make_skill_folder(self.tmp)
        manifest = valid_manifest(folder)
        manifest["mcp"] = {"servers": {"x": {"type": "http"}}}
        result = validate_manifest(manifest, folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_mcp_server_url", result.error_codes())
        manifest["mcp"] = {"servers": {"x": {"type": "stdio", "command": "npx y"}}}
        result = validate_manifest(manifest, folder)
        self.assertTrue(result.ok, result.errors)


class TestConverter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="packs-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_skill_manifest_fields(self):
        folder = make_skill_folder(self.tmp)
        (folder / "SOURCE.md").write_text(
            SOURCE_MD.format(name="demo-skill"), encoding="utf-8")
        manifest, errors = build_manifest(folder)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["name"], "demo-skill")
        self.assertEqual(manifest["kind"], "skill")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["source"]["origin"],
                         "https://github.com/example/demo-skill")
        self.assertEqual(manifest["source"]["license"], "MIT")
        self.assertEqual(manifest["mcp"], {"servers": {}})
        self.assertEqual(manifest["review"]["required_before"],
                         DEFAULT_REVIEW_ACTIONS)

    def test_source_md_unparseable_license(self):
        path = self.tmp / "SOURCE.md"
        path.write_text("# Source\n\n- Origin repo: https://example.invalid/x\n"
                        "- License: see the repo.\n", encoding="utf-8")
        parsed = parse_source_md(path)
        self.assertEqual(parsed["origin"], "https://example.invalid/x")
        self.assertEqual(parsed["license"], "see SOURCE.md")

    def test_build_crew_manifest_fields(self):
        folder = make_crew_folder(self.tmp)
        manifest, errors = build_manifest(folder)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["kind"], "crew")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["description"],
                         "A demo crew of two characters.")
        self.assertEqual(manifest["source"], {"origin": "own-build"})
        self.assertEqual(manifest["characters"], ["alpha", "beta"])

    def test_unknown_pack_kind(self):
        folder = self.tmp / "empty"
        folder.mkdir()
        manifest, errors = build_manifest(folder)
        self.assertIsNone(manifest)
        self.assertIn("unknown_pack_kind", [e["code"] for e in errors])

    def test_convert_writes_and_is_idempotent(self):
        folder = make_skill_folder(self.tmp)
        first = convert_folder(folder, write=True)
        self.assertTrue(first.ok, first.errors)
        self.assertTrue(first.manifest["_wrote"])
        data_first = (folder / "manifest.json").read_bytes()
        second = convert_folder(folder, write=True)
        self.assertTrue(second.ok, second.errors)
        self.assertFalse(second.manifest["_wrote"])
        self.assertEqual((folder / "manifest.json").read_bytes(), data_first)
        # The written manifest validates.
        self.assertTrue(validate_pack(folder).ok)

    def test_convert_dry_run_writes_nothing(self):
        folder = make_skill_folder(self.tmp)
        result = convert_folder(folder, write=False)
        self.assertTrue(result.ok, result.errors)
        self.assertFalse((folder / "manifest.json").exists())

    def test_convert_shelf_tmp(self):
        shelf = self.tmp / "shelf"
        shelf.mkdir()
        make_skill_folder(shelf, "skill-a")
        make_crew_folder(shelf, "crew-b")
        results = convert_shelf(shelf, write=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results), results)
        self.assertTrue(all(validate_pack(shelf / r.name).ok for r in results))


class TestRealShelf(unittest.TestCase):
    """Read-only checks against the real shelf (15 packs)."""

    def test_real_shelf_converts(self):
        if not SHELF_DIR.is_dir():
            self.skipTest("real shelf not present: %s" % SHELF_DIR)
        results = convert_shelf(SHELF_DIR, write=False)
        self.assertEqual(len(results), 15)
        failed = [r for r in results if not r.ok]
        self.assertEqual(failed, [])
        kinds = sorted(r.manifest["kind"] for r in results)
        self.assertEqual(kinds, ["crew"] + ["skill"] * 13 + ["template"])
        names = sorted(r.name for r in results)
        self.assertIn("crew-youtube-team", names)
        self.assertIn("threads-substance", names)
        self.assertIn("daily-briefing-crew", names)
        template = next(r for r in results
                        if r.name == "daily-briefing-crew")
        self.assertEqual(template.manifest["kind"], "template")
        self.assertEqual(template.manifest["characters"],
                         ["Bina", "Jaka"])
        crew = next(r for r in results if r.name == "crew-youtube-team")
        self.assertEqual(crew.manifest["characters"], [
            "chief-officer", "yt-analyst", "yt-idea-scout", "yt-packaging",
            "yt-scriptwriter", "yt-strategist"])
        imported = next(r for r in results if r.name == "threads-substance")
        self.assertEqual(
            imported.manifest["source"]["origin"],
            "https://github.com/fred1357944/threads-substance")
        self.assertEqual(imported.manifest["source"]["license"], "MIT")

    def test_real_shelf_manifests_validate(self):
        if not SHELF_DIR.is_dir():
            self.skipTest("real shelf not present: %s" % SHELF_DIR)
        results = validate_shelf(SHELF_DIR)
        self.assertEqual(len(results), 15)
        failed = [(r.name, r.errors) for r in results if not r.ok]
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
