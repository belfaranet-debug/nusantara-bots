"""Tests for the skill + character loader (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s loader/tests -v

Fixtures live in loader/tests/fixtures/ (committed, inspectable):
skills/ has one folder per case, characters/ has one file per case.
Tests assert both directions: valid fixtures PASS and each invalid
fixture FAILS with the expected error code.
"""

import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loader import load_character, load_characters, load_skill, load_skills

FIX = Path(__file__).resolve().parent / "fixtures"


class SkillTests(unittest.TestCase):
    def test_valid_skill_passes(self):
        s = load_skill(FIX / "skills" / "valid-skill")
        self.assertTrue(s.ok, s.errors)
        self.assertEqual(s.version, "1.0.0")
        self.assertEqual(s.version_source, "version")

    def test_metadata_version_accepted(self):
        s = load_skill(FIX / "skills" / "metadata-version-skill")
        self.assertTrue(s.ok, s.errors)
        self.assertEqual(s.version, "2.0.0")
        self.assertEqual(s.version_source, "metadata.version")

    def test_wrong_name_rejected(self):
        s = load_skill(FIX / "skills" / "wrong-name")
        self.assertFalse(s.ok)
        self.assertIn("name_mismatch", s.error_codes())

    def test_long_description_rejected(self):
        s = load_skill(FIX / "skills" / "long-description")
        self.assertFalse(s.ok)
        self.assertIn("description_too_long", s.error_codes())
        self.assertGreater(len(s.description), 1024)

    def test_missing_frontmatter_rejected(self):
        s = load_skill(FIX / "skills" / "missing-frontmatter")
        self.assertFalse(s.ok)
        self.assertIn("missing_frontmatter", s.error_codes())

    def test_unclosed_frontmatter_rejected(self):
        s = load_skill(FIX / "skills" / "unclosed-frontmatter")
        self.assertFalse(s.ok)
        self.assertIn("unclosed_frontmatter", s.error_codes())

    def test_missing_version_rejected(self):
        s = load_skill(FIX / "skills" / "missing-version")
        self.assertFalse(s.ok)
        self.assertIn("missing_version", s.error_codes())

    def test_empty_body_rejected(self):
        s = load_skill(FIX / "skills" / "empty-body")
        self.assertFalse(s.ok)
        self.assertIn("empty_body", s.error_codes())

    def test_missing_skill_md_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            s = load_skill(Path(d) / "no-skill-md-here")
            self.assertFalse(s.ok)
            self.assertIn("missing_skill_md", s.error_codes())

    def test_load_skills_lists_all(self):
        skills = load_skills(FIX / "skills")
        by_name = {s.path.name: s for s in skills}
        self.assertEqual(len(skills), 8)
        self.assertTrue(by_name["valid-skill"].ok)
        self.assertFalse(by_name["wrong-name"].ok)


class CharacterTests(unittest.TestCase):
    def test_valid_block_lists_pass(self):
        c = load_character(FIX / "characters" / "valid-character.md")
        self.assertTrue(c.ok, c.errors)
        self.assertEqual(c.skills, ["team-memory", "bot-live-presence"])
        self.assertEqual(c.tools, ["read_file"])
        self.assertEqual(c.memory_scope, "private+team")
        self.assertIn("valid test character", c.prompt)

    def test_valid_inline_lists_pass(self):
        c = load_character(FIX / "characters" / "valid-inline.md")
        self.assertTrue(c.ok, c.errors)
        self.assertEqual(c.skills, ["team-memory"])
        self.assertEqual(c.tools, [])
        self.assertEqual(c.memory_scope, "team")

    def test_legacy_header_rejected(self):
        c = load_character(FIX / "characters" / "legacy-header.md")
        self.assertFalse(c.ok)
        self.assertIn("missing_frontmatter", c.error_codes())

    def test_malformed_list_rejected(self):
        c = load_character(FIX / "characters" / "malformed-list.md")
        self.assertFalse(c.ok)
        self.assertIn("malformed_list", c.error_codes())

    def test_missing_role_rejected(self):
        c = load_character(FIX / "characters" / "missing-role.md")
        self.assertFalse(c.ok)
        self.assertIn("missing_role", c.error_codes())

    def test_bad_memory_scope_rejected(self):
        c = load_character(FIX / "characters" / "bad-memory-scope.md")
        self.assertFalse(c.ok)
        self.assertIn("invalid_memory_scope", c.error_codes())

    def test_empty_body_rejected(self):
        c = load_character(FIX / "characters" / "empty-body.md")
        self.assertFalse(c.ok)
        self.assertIn("empty_body", c.error_codes())

    def test_scalar_skills_rejected(self):
        c = load_character(FIX / "characters" / "scalar-skills.md")
        self.assertFalse(c.ok)
        self.assertIn("not_a_list", c.error_codes())

    def test_load_characters_lists_all(self):
        chars = load_characters(FIX / "characters")
        self.assertEqual(len(chars), 8)
        valid = [c for c in chars if c.ok]
        self.assertEqual(len(valid), 2)


if __name__ == "__main__":
    unittest.main()
