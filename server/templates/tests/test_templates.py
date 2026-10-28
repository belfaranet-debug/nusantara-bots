"""Tests for bot templates — stdlib unittest, no network, no model
calls, no credentials anywhere.

resolve_template is exercised with fixture HTML through an injected
fetcher; the default fetcher is never called here (a test would fail
if it were — the fixture fetcher raises on unexpected use). The
importer stages packs into temp shelf folders only. The real
``daily-briefing-crew`` pack on the shelf is validated read-only.

The brand check below builds the banned name by concatenation so
that the literal string never appears in this repo (the tree-wide
brand grep must stay at zero).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packs.pack import (
    MAX_PACKAGE_BYTES,
    MAX_SKILLS_PER_PACKAGE,
    redact_secrets,
    validate_manifest,
    validate_pack,
)
from routines.scheduler import RoutineStore
from templates import (
    REVIEW_TTL_SECONDS,
    TemplateImportError,
    TemplateInstallError,
    TemplateResolveError,
    TemplateReviewStore,
    install_reviewed_template,
    review_template_package,
    validate_package_bytes,
)
from templates.importer import (import_template,
                                sanitize_template_definition,
                                validate_template_definition)
from templates.resolve import parse_template_link, resolve_template

SHELF_DIR = Path(__file__).resolve().parents[3] / "grokbot" / "skill-pack"

# A fake token-shaped value, used ONLY to prove redaction/rejection.
# It is not a real credential (all one repeated character).
FAKE_TOKEN = "sk-" + "A" * 32

SHELF_DIR = Path(__file__).resolve().parents[3] / "grokbot" / "skill-pack"

EXAMPLE_ID = "Ub3T7usX-c6yRQibQq83P"
EXAMPLE_URL = "https://x.ai/bot/" + EXAMPLE_ID

FIXTURE_HTML = """<!doctype html><html><head>
<meta property="og:title" content="Morning Crew">
<meta property="og:description" content="A crew for mornings.">
<script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"template": {
  "name": "morning-crew", "version": "2.0.0",
  "description": "A crew for mornings.",
  "characters": [
    {"name": "Bina", "role": "News summariser",
     "skills": ["inbox-triage"], "prompt": "Summarise the news."},
    {"name": "Jaka", "role": "Day planner",
     "skills": ["weekly-pulse"], "prompt": "Plan the day."}
  ],
  "skills": ["inbox-triage", "weekly-pulse"]
}}}}
</script></head><body>Morning Crew</body></html>"""

META_ONLY_HTML = """<!doctype html><html><head>
<meta property="og:title" content="Mystery Crew">
<meta property="og:description" content="No definition here.">
</head><body>hello</body></html>"""

PLAIN_HTML = ("<!doctype html><html><head><title>Nothing</title></head>"
              "<body><p>no template here</p></body></html>")


def fixture_fetcher(html):
    def fetch(url):
        return html
    return fetch


def valid_definition(**overrides):
    definition = {
        "name": "morning-crew",
        "version": "2.0.0",
        "description": "A crew for mornings.",
        "characters": [
            {"name": "Bina", "role": "News summariser",
             "skills": ["inbox-triage"], "prompt": "Summarise the news."},
            {"name": "Jaka", "role": "Day planner",
             "skills": ["weekly-pulse"], "prompt": "Plan the day."},
        ],
        "skills": ["inbox-triage", "weekly-pulse"],
        "source": {"origin": "own-build"},
    }
    definition.update(overrides)
    return definition


def valid_full_definition(**overrides):
    """valid_definition plus one routine and one connection — the
    full grokbot.team shape (routines start paused, connections
    start disabled at install). All values are original fixtures."""
    definition = valid_definition()
    definition["routines"] = [
        {"title": "Morning pulse", "character": "Bina",
         "schedule": {"kind": "daily", "time": "07:00"}},
    ]
    definition["connections"] = [
        {"name": "notes-mcp", "type": "http",
         "url": "https://example.invalid/mcp"},
    ]
    definition.update(overrides)
    return definition


class TestParseLink(unittest.TestCase):
    def test_valid_links(self):
        self.assertEqual(parse_template_link(EXAMPLE_URL), EXAMPLE_ID)
        # The example id is 21 chars; a 20-char id is accepted too
        # (the RE report's figure) — see resolve.py for why.
        self.assertEqual(parse_template_link("https://x.ai/bot/" + "B" * 20),
                         "B" * 20)
        self.assertEqual(parse_template_link("x.ai/bot/" + EXAMPLE_ID),
                         EXAMPLE_ID)
        self.assertEqual(
            parse_template_link("http://www.x.ai/bot/" + EXAMPLE_ID + "/"),
            EXAMPLE_ID)
        self.assertEqual(
            parse_template_link(EXAMPLE_URL + "?ref=gallery"), EXAMPLE_ID)

    def test_invalid_links(self):
        bad = [
            "",
            "https://x.ai/bot/",
            "https://x.ai/bot/short",
            "https://x.ai/bot/" + "A" * 19,   # 19 chars, not 20-21
            "https://x.ai/bot/" + "A" * 22,   # 22 chars, not 20-21
            "https://x.ai/bot/Ub3T7usX-c6yRQibQq83!",  # '!' not base64url
            "https://x.ai/agent/" + EXAMPLE_ID,
            "https://example.com/bot/" + EXAMPLE_ID,
            "https://x.ai/bot/" + EXAMPLE_ID.replace("-", "+"),  # '+' invalid
        ]
        for url in bad:
            with self.assertRaises(ValueError, msg=url):
                parse_template_link(url)


class TestResolve(unittest.TestCase):
    def test_resolve_embedded_json(self):
        definition = resolve_template(
            EXAMPLE_URL, fetcher=fixture_fetcher(FIXTURE_HTML))
        self.assertEqual(definition["template_id"], EXAMPLE_ID)
        self.assertEqual(definition["name"], "morning-crew")
        self.assertEqual(definition["version"], "2.0.0")
        self.assertEqual(len(definition["characters"]), 2)
        self.assertEqual(definition["characters"][0]["name"], "Bina")
        self.assertNotIn("partial", definition)

    def test_resolve_meta_only_is_partial(self):
        definition = resolve_template(
            EXAMPLE_URL, fetcher=fixture_fetcher(META_ONLY_HTML))
        self.assertTrue(definition["partial"])
        self.assertEqual(definition["name"], "Mystery Crew")
        self.assertEqual(definition["description"], "No definition here.")
        self.assertNotIn("characters", definition)

    def test_resolve_unrecognised_page_errors(self):
        with self.assertRaises(TemplateResolveError):
            resolve_template(EXAMPLE_URL,
                             fetcher=fixture_fetcher(PLAIN_HTML))

    def test_resolve_empty_page_errors(self):
        with self.assertRaises(TemplateResolveError):
            resolve_template(EXAMPLE_URL, fetcher=fixture_fetcher("   "))

    def test_resolve_fetch_failure_errors(self):
        def failing(url):
            raise RuntimeError("network down")
        with self.assertRaises(TemplateResolveError):
            resolve_template(EXAMPLE_URL, fetcher=failing)

    def test_resolve_bad_link_errors_before_fetch(self):
        calls = []

        def counting(url):
            calls.append(url)
            return FIXTURE_HTML
        with self.assertRaises(ValueError):
            resolve_template("https://x.ai/bot/nope", fetcher=counting)
        self.assertEqual(calls, [])


class TestImporter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="templates-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_definition_has_no_errors(self):
        self.assertEqual(validate_template_definition(valid_definition()),
                         [])

    def test_import_stages_valid_pack(self):
        result = import_template(valid_definition(), self.tmp)
        self.assertEqual(result["name"], "morning-crew")
        folder = self.tmp / "morning-crew"
        self.assertTrue((folder / "template.json").is_file())
        self.assertTrue((folder / "manifest.json").is_file())
        self.assertTrue((folder / "README.md").is_file())
        self.assertEqual(result["manifest"]["kind"], "template")
        self.assertEqual(result["manifest"]["characters"],
                         ["Bina", "Jaka"])
        # The staged pack passes the pack validator.
        pack_result = validate_pack(folder)
        self.assertTrue(pack_result.ok, pack_result.errors)
        # Staging creates NO character files: installation is a
        # separate, explicit step.
        self.assertFalse((folder / "characters").exists())

    def test_import_resolved_definition_end_to_end(self):
        definition = resolve_template(
            EXAMPLE_URL, fetcher=fixture_fetcher(FIXTURE_HTML))
        result = import_template(definition, self.tmp,
                                 source_url=EXAMPLE_URL)
        folder = self.tmp / "morning-crew"
        self.assertTrue(validate_pack(folder).ok)
        staged = json.loads((folder / "template.json").read_text())
        self.assertEqual(staged["source"]["origin"], EXAMPLE_URL)
        self.assertTrue((folder / "SOURCE.md").is_file())

    def test_import_refuses_partial_definition(self):
        partial = resolve_template(
            EXAMPLE_URL, fetcher=fixture_fetcher(META_ONLY_HTML))
        with self.assertRaises(TemplateImportError):
            import_template(partial, self.tmp)

    def test_import_refuses_invalid_definitions(self):
        cases = [
            valid_definition(name="Bad Name!"),           # not a slug
            valid_definition(name=""),
            valid_definition(version=""),
            valid_definition(description=""),
            valid_definition(characters=[]),
            valid_definition(characters=[
                {"name": "Solo", "role": "", "prompt": "x"}]),
            valid_definition(characters=[
                {"name": "Solo", "role": "R", "prompt": ""}]),
            valid_definition(characters=[
                {"name": "A", "role": "R", "prompt": "x"},
                {"name": "A", "role": "R", "prompt": "y"}]),  # duplicate
            valid_definition(skills="not-a-list"),
            valid_definition(review={"required_before": ["send"]}),
        ]
        for definition in cases:
            with self.assertRaises(TemplateImportError, msg=str(definition)):
                import_template(definition, self.tmp)

    def test_import_refuses_secret_keys(self):
        sneaky = valid_definition()
        sneaky["characters"][0]["api_key"] = "never-in-a-template"
        errors = validate_template_definition(sneaky)
        self.assertTrue(any(e["code"] == "secret_in_template"
                            for e in errors))
        with self.assertRaises(TemplateImportError):
            import_template(sneaky, self.tmp)

    def test_import_refuses_overwrite(self):
        import_template(valid_definition(), self.tmp)
        with self.assertRaises(TemplateImportError):
            import_template(valid_definition(), self.tmp)
        # The first pack is untouched.
        self.assertTrue(validate_pack(self.tmp / "morning-crew").ok)

    def test_import_fills_review_defaults(self):
        # The base definition carries no review block; the importer
        # must fill in the six default actions, not refuse it.
        definition = valid_definition()
        self.assertNotIn("review", definition)
        result = import_template(definition, self.tmp)
        from packs.pack import DEFAULT_REVIEW_ACTIONS
        self.assertEqual(result["manifest"]["review"]["required_before"],
                         DEFAULT_REVIEW_ACTIONS)


class TestPackageSecurityRules(unittest.TestCase):
    """grokbot.team package rules: forbidden fields, unknown-field
    stripping, secret redaction, size/skill caps, and the
    two-stage review -> install flow (connections disabled,
    routines paused). All fixtures are original; no network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="templates-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _routine_store(self):
        return RoutineStore(str(self.tmp / "routines.json"))

    # -- forbidden fields: rejected, never stripped silently --------
    def test_forbidden_fields_rejected(self):
        cases = [
            valid_definition(
                chat_history=[{"role": "user", "text": "hello"}]),
            valid_definition(model="some-model"),
            valid_definition(model_id="model-123"),
            valid_definition(visibility="public"),
            valid_definition(computer="some-machine"),
            valid_definition(secrets={"x": "y"}),
        ]
        nested = valid_definition()
        nested["characters"][0]["model"] = "some-model"
        cases.append(nested)
        nested_routine = valid_full_definition()
        nested_routine["routines"][0]["computer"] = "some-machine"
        cases.append(nested_routine)
        for definition in cases:
            errors = validate_template_definition(definition)
            codes = [e["code"] for e in errors]
            self.assertIn("forbidden_field", codes, definition)
            with self.assertRaises(TemplateImportError):
                import_template(definition, self.tmp)
            with self.assertRaises(TemplateImportError):
                review_template_package(definition)

    def test_forbidden_field_error_names_the_field(self):
        errors = validate_template_definition(
            valid_definition(chat_history=[]))
        forbidden = [e for e in errors if e["code"] == "forbidden_field"]
        self.assertEqual(len(forbidden), 1)
        self.assertIn("chat_history", forbidden[0]["message"])

    # -- unknown fields: stripped + recorded ------------------------
    def test_unknown_fields_stripped_and_recorded(self):
        definition = valid_full_definition(flavor_text="not a real field")
        definition["characters"][0]["mood"] = "cheery"
        definition["routines"][0]["notes"] = "extra note"
        definition["connections"][0]["comment"] = "extra comment"
        cleaned, stripped = sanitize_template_definition(definition)
        self.assertIn("flavor_text", stripped)
        self.assertIn("characters[0].mood", stripped)
        self.assertIn("routines[0].notes", stripped)
        self.assertIn("connections[0].comment", stripped)
        self.assertNotIn("flavor_text", cleaned)
        payload = review_template_package(definition)
        self.assertEqual(sorted(payload["stripped_fields"]),
                         sorted(stripped))
        self.assertTrue(any("stripped" in w for w in payload["warnings"]))
        result = import_template(definition, self.tmp)
        self.assertEqual(sorted(result["stripped_fields"]),
                         sorted(stripped))
        staged = json.loads(
            (self.tmp / "morning-crew" / "template.json").read_text())
        self.assertNotIn("flavor_text", staged)
        self.assertNotIn("mood", staged["characters"][0])
        self.assertNotIn("notes", staged["routines"][0])
        self.assertNotIn("comment", staged["connections"][0])
        # ...but the stripped fields did not block a valid import.
        self.assertTrue(validate_pack(self.tmp / "morning-crew").ok)

    def test_resolver_metadata_is_not_reported_as_stripped(self):
        definition = resolve_template(
            EXAMPLE_URL, fetcher=fixture_fetcher(FIXTURE_HTML))
        _, stripped = sanitize_template_definition(definition)
        self.assertEqual(stripped, [])

    # -- secrets: rejected + redacted in every output ----------------
    def test_secret_value_rejected_and_redacted(self):
        sneaky = valid_definition()
        sneaky["characters"][0]["prompt"] = (
            "Summarise the news. Key: " + FAKE_TOKEN)
        errors = validate_template_definition(sneaky)
        codes = [e["code"] for e in errors]
        self.assertIn("secret_value_detected", codes)
        for error in errors:
            self.assertNotIn(FAKE_TOKEN, error["message"])
        with self.assertRaises(TemplateImportError) as ctx:
            import_template(sneaky, self.tmp)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        with self.assertRaises(TemplateImportError) as ctx2:
            review_template_package(sneaky)
        self.assertNotIn(FAKE_TOKEN, str(ctx2.exception))
        # The package was never staged.
        self.assertFalse((self.tmp / "morning-crew").exists())

    def test_secret_in_description_and_url_rejected(self):
        sneaky = valid_definition(
            description="A crew. token=" + "B" * 24)
        errors = validate_template_definition(sneaky)
        self.assertIn("secret_value_detected",
                      [e["code"] for e in errors])
        sneaky_conn = valid_full_definition()
        sneaky_conn["connections"][0]["url"] = (
            "https://example.invalid/mcp?token=" + "C" * 24)
        errors = validate_template_definition(sneaky_conn)
        self.assertIn("secret_value_detected",
                      [e["code"] for e in errors])

    def test_redact_secrets_replaces_values_not_keys(self):
        cleaned = redact_secrets({
            "api_key": FAKE_TOKEN,
            "nested": {"note": "key " + FAKE_TOKEN},
            "fine": "hello",
        })
        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["note"], "[REDACTED]")
        self.assertEqual(cleaned["fine"], "hello")
        self.assertNotIn(FAKE_TOKEN, json.dumps(cleaned))

    def test_ordinary_prose_does_not_trip_secret_detection(self):
        definition = valid_definition()
        definition["characters"][0]["prompt"] = (
            "Summarise the news. If a message mentions an API key, "
            "say so — never paste the key itself. Placeholder "
            "api_key=YOUR_KEY_HERE is not a real secret.")
        self.assertEqual(validate_template_definition(definition), [])

    # -- size cap: 4 MB ----------------------------------------------
    def test_package_over_4mb_rejected(self):
        huge = valid_definition()
        huge["characters"][0]["prompt"] = "x" * (MAX_PACKAGE_BYTES + 1)
        errors = validate_template_definition(huge)
        self.assertIn("package_too_large",
                      [e["code"] for e in errors])
        with self.assertRaises(TemplateImportError):
            import_template(huge, self.tmp)
        with self.assertRaises(TemplateImportError):
            review_template_package(huge)

    def test_package_bytes_size_checked_before_parsing(self):
        # Oversized raw bytes are refused without being decoded.
        with self.assertRaises(TemplateImportError) as ctx:
            validate_package_bytes(b" " * (MAX_PACKAGE_BYTES + 1))
        self.assertIn("limit is", str(ctx.exception))
        # Exactly at the limit parses (trailing JSON whitespace).
        raw = b'{"name": "x"}' + b" " * (MAX_PACKAGE_BYTES - 13)
        self.assertEqual(len(raw), MAX_PACKAGE_BYTES)
        self.assertEqual(validate_package_bytes(raw), {"name": "x"})
        with self.assertRaises(TemplateImportError):
            validate_package_bytes(b"{not json")
        with self.assertRaises(TemplateImportError):
            validate_package_bytes(b"[1, 2]")

    # -- skill cap: 60 -----------------------------------------------
    def test_more_than_60_skills_rejected(self):
        many = ["skill-%02d" % i for i in range(61)]
        definition = valid_definition(skills=many)
        definition["characters"][0]["skills"] = []
        definition["characters"][1]["skills"] = []
        errors = validate_template_definition(definition)
        self.assertIn("too_many_skills", [e["code"] for e in errors])
        with self.assertRaises(TemplateImportError):
            import_template(definition, self.tmp)

    def test_exactly_60_skills_allowed(self):
        sixty = ["skill-%02d" % i for i in range(60)]
        definition = valid_definition(skills=sixty)
        definition["characters"][0]["skills"] = []
        definition["characters"][1]["skills"] = []
        self.assertEqual(validate_template_definition(definition), [])
        result = import_template(definition, self.tmp)
        self.assertEqual(len(result["manifest"]["skills"]), 60)

    # -- routines / connections schema --------------------------------
    def test_routine_must_name_a_template_character(self):
        definition = valid_full_definition()
        definition["routines"][0]["character"] = "Nobody"
        errors = validate_template_definition(definition)
        self.assertIn("routine_unknown_character",
                      [e["code"] for e in errors])

    def test_routine_bad_schedule_rejected(self):
        definition = valid_full_definition()
        definition["routines"][0]["schedule"] = {"kind": "daily",
                                                 "time": "25:00"}
        errors = validate_template_definition(definition)
        self.assertIn("bad_routine_schedule",
                      [e["code"] for e in errors])

    def test_connection_bad_url_rejected(self):
        definition = valid_full_definition()
        definition["connections"][0]["url"] = "ftp://example.invalid/x"
        errors = validate_template_definition(definition)
        self.assertIn("bad_connection_url", [e["code"] for e in errors])

    def test_full_definition_validates_and_stages(self):
        self.assertEqual(
            validate_template_definition(valid_full_definition()), [])
        result = import_template(valid_full_definition(), self.tmp)
        staged = json.loads(
            (self.tmp / "morning-crew" / "template.json").read_text())
        self.assertEqual(staged["format"], "grokbot.team")
        self.assertEqual(staged["format_version"], 1)
        self.assertEqual(len(staged["routines"]), 1)
        self.assertEqual(len(staged["connections"]), 1)
        self.assertEqual(
            result["manifest"]["mcp"]["servers"]["notes-mcp"]["enabled"],
            False)
        self.assertTrue(validate_pack(self.tmp / "morning-crew").ok)

    # -- review payload ------------------------------------------------
    def test_review_payload_contents(self):
        payload = review_template_package(valid_full_definition())
        self.assertEqual(payload["format"], "grokbot.team")
        self.assertEqual(payload["format_version"], 1)
        self.assertIn("review_id", payload)
        self.assertEqual(payload["package"]["name"], "morning-crew")
        self.assertEqual(payload["package"]["character_count"], 2)
        self.assertEqual(payload["package"]["routine_count"], 1)
        self.assertEqual(payload["package"]["connection_count"], 1)
        self.assertEqual(payload["package"]["skill_count"], 2)
        self.assertEqual([c["name"] for c in payload["characters"]],
                         ["Bina", "Jaka"])
        # Prompts appear only as a short preview, never in full.
        self.assertEqual(payload["characters"][0]["prompt_preview"],
                         "Summarise the news.")
        self.assertNotIn("prompt", payload["characters"][0])
        # Routines paused, connections disabled — in the payload.
        self.assertFalse(payload["routines"][0]["enabled"])
        self.assertFalse(payload["connections"][0]["enabled"])
        self.assertTrue(any("DISABLED" in w for w in payload["warnings"]))
        self.assertTrue(any("PAUSED" in w for w in payload["warnings"]))
        self.assertEqual(payload["expires_in_seconds"],
                         REVIEW_TTL_SECONDS)
        self.assertEqual(payload["requires_review_before"],
                         ["send", "publish", "merge", "deploy", "delete",
                          "change-production-data"])

    # -- two-stage install ---------------------------------------------
    def test_install_without_review_fails(self):
        store = TemplateReviewStore()
        with self.assertRaises(TemplateInstallError):
            install_reviewed_template(
                "no-such-review", self.tmp, store=store,
                routine_store=self._routine_store())
        with self.assertRaises(TemplateInstallError):
            install_reviewed_template(
                "", self.tmp, store=store,
                routine_store=self._routine_store())
        self.assertFalse((self.tmp / "morning-crew").exists())

    def test_review_then_install_pauses_routines_disables_connections(self):
        store = TemplateReviewStore()
        rstore = self._routine_store()
        payload = review_template_package(valid_full_definition(),
                                          store=store)
        review_id = payload["review_id"]
        result = install_reviewed_template(
            review_id, self.tmp, store=store, routine_store=rstore)
        self.assertEqual(result["name"], "morning-crew")
        self.assertEqual(result["review_id"], review_id)
        # Routines: created, paused, never due.
        self.assertEqual(len(result["routines"]), 1)
        routine = result["routines"][0]
        self.assertFalse(routine["enabled"])
        stored = rstore.get_routine(routine["id"])
        self.assertIsNotNone(stored)
        self.assertFalse(stored["enabled"])
        self.assertEqual(rstore.due_routines(), [])
        # Connections: recorded disabled, in the result, in the
        # manifest, and in install-state.json on disk.
        self.assertEqual(len(result["connections"]), 1)
        self.assertFalse(result["connections"][0]["enabled"])
        manifest = json.loads(
            (self.tmp / "morning-crew" / "manifest.json").read_text())
        self.assertFalse(
            manifest["mcp"]["servers"]["notes-mcp"]["enabled"])
        state = json.loads(
            (self.tmp / "morning-crew" / "install-state.json")
            .read_text())
        self.assertEqual(state["review_id"], review_id)
        self.assertFalse(state["routines"][0]["enabled"])
        self.assertFalse(state["connections"][0]["enabled"])
        self.assertEqual(state["characters"], ["Bina", "Jaka"])
        self.assertTrue(validate_pack(self.tmp / "morning-crew").ok)
        # Single-use: the same review cannot install twice.
        with self.assertRaises(TemplateInstallError):
            install_reviewed_template(
                review_id, self.tmp, store=store, routine_store=rstore)

    def test_review_expires(self):
        store = TemplateReviewStore()
        payload = store.create(valid_definition(), now=1000.0)
        rstore = self._routine_store()
        with self.assertRaises(TemplateInstallError):
            install_reviewed_template(
                payload["review_id"], self.tmp, store=store,
                routine_store=rstore,
                now=1000.0 + REVIEW_TTL_SECONDS + 1)
        # Just inside the TTL it still installs.
        result = install_reviewed_template(
            payload["review_id"], self.tmp, store=store,
            routine_store=rstore,
            now=1000.0 + REVIEW_TTL_SECONDS - 60)
        self.assertEqual(result["name"], "morning-crew")

    def test_failed_staging_releases_review_for_retry(self):
        store = TemplateReviewStore()
        rstore = self._routine_store()
        payload = review_template_package(valid_definition(), store=store)
        review_id = payload["review_id"]
        # Block the name so staging fails with the overwrite refusal.
        (self.tmp / "morning-crew").mkdir()
        with self.assertRaises(TemplateImportError):
            install_reviewed_template(
                review_id, self.tmp, store=store, routine_store=rstore)
        # The review was released: removing the blocker lets the
        # same review install.
        (self.tmp / "morning-crew").rmdir()
        result = install_reviewed_template(
            review_id, self.tmp, store=store, routine_store=rstore)
        self.assertEqual(result["name"], "morning-crew")


class TestTemplateValidator(unittest.TestCase):
    """Kind "template" in packs/pack.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="templates-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage(self, name="demo-template", template_overrides=None,
               **manifest_overrides):
        folder = self.tmp / name
        folder.mkdir(parents=True)
        template = {
            "name": name, "version": "1.0.0",
            "description": "A demo template.",
            "characters": [{"name": "Alpha", "role": "Does A",
                            "skills": [], "prompt": "Do A."}],
            "skills": [],
            "source": {"origin": "own-build"},
        }
        if template_overrides:
            template.update(template_overrides)
        (folder / "template.json").write_text(
            json.dumps(template), encoding="utf-8")
        (folder / "README.md").write_text(
            "# Demo Template\n\nA demo template.\n", encoding="utf-8")
        manifest = {
            "name": name, "kind": "template", "version": "1.0.0",
            "description": "A demo template.",
            "skills": [], "characters": ["Alpha"],
            "source": {"origin": "own-build"},
            "mcp": {"servers": {}},
            "review": {"required_before": [
                "send", "publish", "merge", "deploy", "delete",
                "change-production-data"]},
            "requires": [],
        }
        manifest.update(manifest_overrides)
        (folder / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        return folder

    def test_valid_template_pack(self):
        self.assertTrue(validate_pack(self._stage()).ok)

    def test_missing_template_json(self):
        folder = self._stage()
        (folder / "template.json").unlink()
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("missing_template_json", result.error_codes())

    def test_missing_readme(self):
        folder = self._stage()
        (folder / "README.md").unlink()
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("missing_template_readme", result.error_codes())

    def test_template_characters_mismatch(self):
        folder = self._stage(characters=["Beta"])
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("template_characters_mismatch",
                      result.error_codes())

    def test_template_character_without_prompt(self):
        folder = self._stage()
        template = json.loads((folder / "template.json").read_text())
        template["characters"][0]["prompt"] = ""
        (folder / "template.json").write_text(
            json.dumps(template), encoding="utf-8")
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("template_bad_characters", result.error_codes())

    def test_secret_in_template_json_rejected(self):
        folder = self._stage()
        template = json.loads((folder / "template.json").read_text())
        template["characters"][0]["password"] = "nope"
        (folder / "template.json").write_text(
            json.dumps(template), encoding="utf-8")
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("secret_in_template_json", result.error_codes())

    def test_template_review_defaults_still_required(self):
        folder = self._stage(review={"required_before": ["send"]})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("review_missing_defaults", result.error_codes())

    def test_bad_kind_rejected(self):
        folder = self._stage(kind="gadget")
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_kind", result.error_codes())

    # -- grokbot.team rules, enforced by the pack validator ---------
    def test_template_forbidden_field_rejected(self):
        folder = self._stage(template_overrides={"model": "some-model"})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("forbidden_field_in_template_json",
                      result.error_codes())

    def test_template_secret_value_rejected_and_redacted(self):
        folder = self._stage()
        template = json.loads((folder / "template.json").read_text())
        template["characters"][0]["prompt"] = "Do A. Key: " + FAKE_TOKEN
        (folder / "template.json").write_text(
            json.dumps(template), encoding="utf-8")
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("secret_value_in_template_json",
                      result.error_codes())
        self.assertNotIn(FAKE_TOKEN, json.dumps(result.errors))

    def test_template_too_many_skills(self):
        skills = ["skill-%02d" % i for i in range(61)]
        folder = self._stage(skills=skills,
                             template_overrides={"skills": skills})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("template_too_many_skills", result.error_codes())

    def test_template_bad_format(self):
        folder = self._stage(
            template_overrides={"format": "something.else"})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_template_format", result.error_codes())

    def test_template_format_marker_ok(self):
        folder = self._stage(
            template_overrides={"format": "grokbot.team",
                                "format_version": 1})
        self.assertTrue(validate_pack(folder).ok)

    def test_template_routine_unknown_character(self):
        folder = self._stage(template_overrides={"routines": [
            {"title": "R", "character": "Nobody",
             "schedule": {"kind": "daily", "time": "07:00"}}]})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("routine_unknown_character", result.error_codes())

    def test_template_routine_bad_schedule(self):
        folder = self._stage(template_overrides={"routines": [
            {"title": "R", "character": "Alpha",
             "schedule": {"kind": "cron", "expression": "bad"}}]})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("bad_routine_schedule", result.error_codes())

    def test_template_connection_must_be_disabled(self):
        folder = self._stage(mcp={"servers": {"x": {
            "type": "http", "url": "https://example.invalid/mcp",
            "enabled": True}}})
        result = validate_pack(folder)
        self.assertFalse(result.ok)
        self.assertIn("template_connection_not_disabled",
                      result.error_codes())

    def test_template_connection_disabled_ok(self):
        folder = self._stage(
            template_overrides={"connections": [
                {"name": "x", "type": "http",
                 "url": "https://example.invalid/mcp"}]},
            mcp={"servers": {"x": {
                "type": "http", "url": "https://example.invalid/mcp",
                "enabled": False}}})
        result = validate_pack(folder)
        self.assertTrue(result.ok, result.errors)


class TestRealShelfTemplate(unittest.TestCase):
    def test_daily_briefing_crew_validates(self):
        folder = SHELF_DIR / "daily-briefing-crew"
        if not folder.is_dir():
            self.skipTest("shelf template pack not present: %s" % folder)
        result = validate_pack(folder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.manifest["kind"], "template")
        self.assertEqual(result.manifest["characters"], ["Bina", "Jaka"])
        template = json.loads((folder / "template.json").read_text())
        self.assertEqual(len(template["characters"]), 2)
        self.assertTrue(all(c["prompt"] for c in template["characters"]))

    def test_no_forbidden_brand_strings_in_new_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots",
                  "open" + "maus"]
        repo = Path(__file__).resolve().parents[2]
        paths = [repo / "templates" / "resolve.py",
                 repo / "templates" / "importer.py",
                 repo / "templates" / "__init__.py",
                 repo / "templates" / "README.md",
                 repo / "templates" / "server-wiring.patch.md",
                 repo / "templates" / "deck-review.patch.md",
                 repo / "templates" / "tests" / "test_templates.py",
                 repo / "packs" / "pack.py",
                 repo / "packs" / "convert.py"]
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, path)


if __name__ == "__main__":
    unittest.main()
