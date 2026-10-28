"""UI readability tests — the old app cut wide tables off at the
screen edge with no sideways scroll; this build must never do
that. These tests assert the CSS rules that prevent it.
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
CSS = STATIC / "style.css"
JS = STATIC / "app.js"


class ReadabilityTest(unittest.TestCase):
    def test_tables_scroll_sideways(self):
        text = CSS.read_text(encoding="utf-8")
        table_rule = re.search(r"table\s*\{[^}]*\}", text)
        self.assertIsNotNone(table_rule, "no table rule in style.css")
        self.assertIn("overflow-x:auto", table_rule.group(0).replace(" ", ""))
        self.assertIn("max-width:100%", table_rule.group(0).replace(" ", ""))

    def test_content_wraps_instead_of_clipping(self):
        text = CSS.read_text(encoding="utf-8")
        self.assertIn("#messages", text)
        wrap_rule = re.search(
            r"#messages[^{]*\{[^}]*overflow-wrap:anywhere[^}]*\}", text)
        self.assertIsNotNone(
            wrap_rule, "messages/panels must wrap long strings")


class SkillsMailCardTest(unittest.TestCase):
    """Skills chips + email line on bot cards (static markers).

    The mail endpoint is a parallel piece of work, so these tests
    assert only what the static files can prove: the fetch uses
    exactly the agreed contract, rendering reads skills[] and the
    mail cache, failure leaves the honest fallback text, and the
    chip classes exist in the stylesheet.
    """

    def test_mail_fetch_uses_agreed_contract(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("/api/mail?all=1", js)
        self.assertIn("/api/mail?character=", js)
        for field in ("address", "address_present", "inbox_count",
                      "unread", "available", "error"):
            self.assertIn(field, js)

    def test_mail_fetch_is_defensive(self):
        js = JS.read_text(encoding="utf-8")
        # loadMail wraps the fetch in try/catch and the fallback
        # text is the only thing rendered when it fails.
        fn = re.search(r"async function loadMail\(\)\s*\{.*?\n\}",
                       js, re.S)
        self.assertIsNotNone(fn, "no loadMail function in app.js")
        self.assertIn("try", fn.group(0))
        self.assertIn("catch", fn.group(0))
        self.assertIn("email: unavailable", js)

    def test_skills_render_from_characters_payload(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("renderSkillChips", js)
        self.assertIn("c.skills", js)
        self.assertIn("skill-chip", js)
        self.assertIn("renderCharacterList", js)
        self.assertIn("renderDeckCharacters", js)
        # Both character renderings (sidebar list and deck's
        # own-build list) go through the same item renderer.
        self.assertIn("renderCharacterItem", js)
        # Room chips keep the capability without inventing data
        # for legacy bots that carry no skills[] or mail entry.
        self.assertIn("renderRoomSkillChips", js)
        self.assertIn("roomMailLineHtml", js)

    def test_chip_and_mail_classes_defined(self):
        css = CSS.read_text(encoding="utf-8")
        for cls in (".skill-chips", ".skill-chip", ".skill-empty",
                    ".mail-line"):
            self.assertIn(cls, css)
        # Deck-dark variant reuses the amber tag colour.
        deck_chip = re.search(r"#deck \.skill-chip\{[^}]*\}", css)
        self.assertIsNotNone(deck_chip, "no #deck .skill-chip rule")
        self.assertIn("#f5a623", deck_chip.group(0))

    def test_no_forbidden_brand_strings_in_changed_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        for path in (JS, CSS):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text)


class OfficeDeckUiTest(unittest.TestCase):
    """Next-level Phase 1 deck pieces (static markers only).

    The deck renders health, data ages, the Kanban board, and duty
    tags from the /api/deck payload. These tests assert the render
    functions, the HTML ids, and the CSS classes exist — the live
    values are covered by test_office.py.
    """

    def test_health_ages_kanban_render_functions(self):
        js = JS.read_text(encoding="utf-8")
        for fn in ("renderDeckHealth", "renderDeckAges",
                   "renderDeckKanban", "ageText"):
            self.assertIn(fn, js)
        # Health shows the level + reasons; ages label stale data.
        self.assertIn("health.level", js)
        self.assertIn("health.reasons", js)
        self.assertIn("basi", js)
        # The Kanban verdict rendered is the server's (c.verdict) —
        # this file must not contain a second copy of the rule
        # (computing a verdict from status + evidence here).
        self.assertIn("c.verdict", js)
        self.assertNotIn('c.status === "done"', js)
        # loadDeck wires all three + duty into the deck render.
        load_deck = re.search(r"async function loadDeck\(\)\s*\{.*?\n\}",
                              js, re.S)
        self.assertIsNotNone(load_deck, "no loadDeck function in app.js")
        for marker in ("renderDeckHealth(d.health)",
                       "renderDeckAges(d.ages",
                       "renderDeckKanban(d.kanban)",
                       "renderDeckCharacters(d.characters || [], d.duty"):
            self.assertIn(marker, load_deck.group(0))

    def test_duty_tag_on_deck_characters(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("duty-tag", js)
        self.assertIn("lastDeckDuty", js)
        # The sidebar list renders without duty (no duty data there).
        self.assertIn("renderCharacterItem(c)", js)

    def test_deck_html_ids_present(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for element_id in ("deck-health", "deck-ages", "deck-kanban",
                           "deck-kanban-note"):
            self.assertIn('id="%s"' % element_id, html)

    def test_kanban_and_duty_classes_defined(self):
        css = CSS.read_text(encoding="utf-8")
        for cls in ("#deck-kanban", ".kanban-col", ".kanban-verdict",
                    ".duty-tag"):
            self.assertIn(cls, css)
        # The Kanban board wraps instead of clipping (readability
        # rule: no content cut off horizontally).
        kanban_rule = re.search(r"#deck-kanban\s*\{[^}]*\}", css)
        self.assertIsNotNone(kanban_rule, "no #deck-kanban rule")
        self.assertIn("flex-wrap:wrap",
                      kanban_rule.group(0).replace(" ", ""))


class Redesign040Test(unittest.TestCase):
    """0.4.0 redesign (video pattern): no bottom nav, top bar +
    back arrow + Create dialog. Static markers only."""

    def test_no_bottom_nav(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("bottomnav", html)
        self.assertNotIn("tab-btn", html)
        self.assertNotIn("switchTab", js)
        self.assertNotIn(".bottomnav", css)
        self.assertNotIn(".tab-btn", css)
        self.assertIn("showScreen", js)
        # 0.6.0: the list screen is gone — chat, rooms, settings
        # only, and the chat IS the landing screen.
        for view in ("view-chat", "view-rooms", "view-settings"):
            self.assertIn('id="%s"' % view, html)
        self.assertNotIn('id="view-list"', html)
        self.assertNotIn('showScreen("list")', js)
        self.assertNotIn("showScreen('list')", js)
        chat_view = re.search(r'<div class="view active" id="view-chat">',
                              html)
        self.assertIsNotNone(chat_view, "chat must be the active view")

    def test_drawer_top_bar_ids(self):
        # 0.6.0: the old list top bar (owner avatar, bell, search,
        # +) plus the bot list moved into the drawer; the working
        # bar is deleted outright.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        drawer = re.search(r'<aside id="drawer".*?</aside>', html, re.S)
        self.assertIsNotNone(drawer, "no drawer in index.html")
        for element_id in ("owner-avatar", "list-actions", "notif-btn",
                           "notif-badge", "search-btn", "create-btn",
                           "notif-panel", "notif-list", "notif-status",
                           "list-search", "list-search-input",
                           "bot-list", "bot-list-empty"):
            self.assertIn('id="%s"' % element_id, drawer.group(0))
        for gone in ("working-bar", "working-dismiss", "working-gear",
                     "working-stop-btn", "working-search",
                     "working-create"):
            self.assertNotIn('id="%s"' % gone, html)
        self.assertIn("Personal", drawer.group(0))
        self.assertIn("Tap + to create a bot", drawer.group(0))

    def test_create_dialog_ids_and_shape(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        for element_id in ("create-scrim", "create-dialog",
                           "create-actions", "create-new-bot",
                           "create-new-group", "create-new-space",
                           "create-name-step", "create-bot-name",
                           "create-bot-name-submit",
                           "create-group-step", "create-group-name",
                           "create-group-members",
                           "create-group-submit"):
            self.assertIn('id="%s"' % element_id, html)
        # Floating centred card, ~88% wide — NOT a bottom sheet.
        dialog_rule = re.search(r"\.create-dialog\s*\{[^}]*\}", css)
        self.assertIsNotNone(dialog_rule, "no .create-dialog rule")
        self.assertIn("width:88%", dialog_rule.group(0).replace(" ", ""))
        scrim_rule = re.search(r"\.scrim\s*\{[^}]*\}", css)
        self.assertIsNotNone(scrim_rule, "no .scrim rule")
        self.assertIn("align-items:center",
                      scrim_rule.group(0).replace(" ", ""))
        # Dismiss is by tapping outside the dialog — no X button.
        dialog = re.search(r'id="create-dialog".*?</div>\s*</div>',
                           html, re.S)
        self.assertIsNotNone(dialog, "create dialog not found")
        self.assertNotIn("create-close", dialog.group(0))

    def test_onboarding_card_ids(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for element_id in ("onboarding-card", "onboarding-dismiss",
                           "onboarding-error", "onboarding-rename",
                           "chat-date"):
            self.assertIn('id="%s"' % element_id, html)
        for focus in ("day-to-day", "inbox", "research", "everything"):
            self.assertIn('data-focus="%s"' % focus, html)

    def test_chat_composer_ids(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for element_id in ("drawer-open-btn", "chat-pill", "chat-pill-avatar",
                           "chat-pill-name", "chat-stop-pill",
                           "chat-menu-btn", "chat-menu",
                           "chat-menu-memory", "chat-menu-stop",
                           "chat-menu-settings", "composer-plus",
                           "composer-send", "composer-sheet",
                           "sheet-memory", "sheet-look",
                           "sheet-new-group", "voice-ptt-btn",
                           "voice-ptt-status", "memory-panel",
                           "rooms-back", "settings-back"):
            self.assertIn('id="%s"' % element_id, html)
        # 0.6.0: no back arrow on the chat — it is the landing
        # screen, so there is no list to go back to. Rooms and
        # settings each carry their own ← Chat button instead.
        self.assertNotIn('id="chat-back"', html)
        # Voice button lives inside the composer field; the send
        # arrow submits the form; the character select stays in
        # the DOM (hidden) and the stop button stays for its
        # handler.
        self.assertIn('id="character-select" class="hidden"', html)
        self.assertIn('id="stop-btn" class="hidden"', html)

    def test_bot_creation_contract_markers(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn('"/api/characters"', js)
        for focus in ("day-to-day", "inbox", "research", "everything"):
            self.assertIn('"%s"' % focus, js)
        # 201 -> character; 409 adopts the existing bot; errors
        # show the server's text and offer a different name.
        self.assertIn("status === 409", js)
        self.assertIn("data.character", js)
        self.assertIn("onboarding-rename", js)
        self.assertIn("composeIntro", js)
        self.assertIn("/api/rooms", js)
        self.assertIn("/api/messages", js)
        # Intro is composed from real data; failures say so.
        self.assertIn("couldn't reach the deck", js.lower())
        self.assertIn("couldn't load my own memory", js.lower())

    def test_notifications_markers(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("/api/approvals?status=pending", js)
        self.assertIn("/api/approvals/\" + verdict", js)
        self.assertIn("renderBotList", js)
        self.assertIn("refreshBotPreviews", js)
        self.assertIn("setWorking", js)
        self.assertIn("clearWorking", js)
        self.assertIn("updateWorkingUI", js)
        # 0.6.0: the working bar is gone — updateWorkingUI only
        # toggles the chat Stop pill now, and the drawer rows
        # read workingState live.
        self.assertIn('pill.classList.toggle("hidden", !workingState)', js)
        self.assertNotIn("working-bar", js)
        self.assertNotIn("workingBarDismissed", js)

    def test_list_row_structure_markers(self):
        js = JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("bot-row", js)
        self.assertIn("bot-avatar", js)
        self.assertIn("bot-preview", js)
        self.assertIn("bot-time", js)
        self.assertIn("bot-unread", js)
        for cls in (".bot-row", ".bot-avatar", ".bot-preview",
                    ".bot-time", ".bot-unread", ".msg-avatar",
                    ".msg-label"):
            self.assertIn(cls, css)
        # Assistant messages are bubble-less in CSS.
        assistant_rule = re.search(
            r"\.msg\.assistant\s*\{[^}]*\}", css)
        self.assertIsNotNone(assistant_rule, "no .msg.assistant rule")
        self.assertIn("background:transparent",
                      assistant_rule.group(0).replace(" ", ""))
        user_rule = re.search(r"\.msg\.user\s*\{[^}]*\}", css)
        self.assertIsNotNone(user_rule, "no .msg.user rule")
        self.assertIn("#2a2e38", user_rule.group(0))

    def test_version_060(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn("versi 0.6.0", html)
        self.assertNotIn("versi 0.5.0", html)

    def test_no_forbidden_brand_strings_in_new_ui(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for path_text in (JS.read_text(encoding="utf-8"),
                          CSS.read_text(encoding="utf-8"), html):
            low = path_text.lower()
            for word in banned:
                self.assertNotIn(word, low)
            self.assertNotIn("based on", low)
            self.assertNotIn("powered by", low)
            # Video-specific content that must never appear.
            self.assertNotIn("jejak hikmah", low)
            self.assertNotIn("youtube-studio", low)
            self.assertNotIn("mr x", low)


class DrawerLayoutTest(unittest.TestCase):
    """0.6.0 redesign (the owner, 2026-10-25): the chat IS the workspace
    — it is the landing screen — and the bot names live beside it
    in a drawer sliding in from the left. Bot behaviour is
    untouched; only the layout moved. Static markers only."""

    def test_drawer_exists_and_slides_from_left(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        for element_id in ("drawer", "drawer-scrim"):
            self.assertIn('id="%s"' % element_id, html)
        for fn in ("openDrawer", "closeDrawer"):
            self.assertIn("function %s(" % fn, js)
        # Fixed, left, full height, ~80% wide (max ~320px),
        # off-canvas via transform + transition.
        drawer_rule = re.search(r"#drawer\s*\{[^}]*\}", css)
        self.assertIsNotNone(drawer_rule, "no #drawer rule")
        rule = drawer_rule.group(0).replace(" ", "")
        self.assertIn("position:fixed", rule)
        self.assertIn("left:0", rule)
        self.assertIn("width:80%", rule)
        self.assertIn("max-width:320px", rule)
        self.assertIn("translateX(-100%)", rule)
        self.assertIn("transition:transform", rule)
        open_rule = re.search(r"#drawer\.open\s*\{[^}]*\}", css)
        self.assertIsNotNone(open_rule, "no #drawer.open rule")
        self.assertIn("translateX(0)",
                      open_rule.group(0).replace(" ", ""))
        # Close paths: scrim tap, swipe left, Android back
        # (history entry pushed on open, popped on close).
        self.assertIn('getElementById("drawer-scrim")', js)
        self.assertIn("touchend", js)
        self.assertIn("history.pushState", js)
        self.assertIn("history.back()", js)
        self.assertIn("popstate", js)

    def test_chat_is_landing_and_list_screen_is_gone(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        chat_view = re.search(r'<div class="view active" id="view-chat">',
                              html)
        self.assertIsNotNone(chat_view, "chat must be the active view")
        self.assertNotIn('id="view-list"', html)
        self.assertNotIn('showScreen("list")', js)
        self.assertNotIn("showScreen('list')", js)
        self.assertNotIn("view-list", js)

    def test_drawer_contents(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        drawer = re.search(r'<aside id="drawer".*?</aside>', html, re.S)
        self.assertIsNotNone(drawer, "no drawer in index.html")
        body = drawer.group(0)
        for element_id in ("bot-list", "bot-list-empty",
                           "drawer-rooms", "drawer-settings"):
            self.assertIn('id="%s"' % element_id, body)
        self.assertIn("Ruangan", body)
        self.assertIn("Pengaturan", body)
        # The two nav rows are the only way into those screens
        # from the drawer (and each screen has a ← Chat back).
        self.assertIn('id="rooms-back"', html)
        self.assertIn('id="settings-back"', html)

    def test_working_bar_gone_stop_pill_stays(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        for gone in ("working-bar", "working-dismiss", "working-gear",
                     "working-stop-btn", "working-search",
                     "working-create"):
            self.assertNotIn('id="%s"' % gone, html)
            self.assertNotIn(gone, js)
        self.assertIn('id="chat-stop-pill"', html)
        # The working STATE is kept — the chat pill still uses it.
        self.assertIn("function setWorking(", js)
        self.assertIn("function clearWorking(", js)
        self.assertIn("workingState", js)

    def test_no_dead_drawer_or_header_buttons(self):
        js = JS.read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for button_id in ("drawer-open-btn", "drawer-rooms",
                          "drawer-settings", "rooms-back",
                          "settings-back"):
            self.assertIn('id="%s"' % button_id, html)
            self.assertIn('getElementById("%s")' % button_id, js,
                          "no handler for #%s" % button_id)
        # Scrim tap closes the drawer.
        self.assertIn('getElementById("drawer-scrim")', js)
        # Back buttons return to the chat, never to a dead end.
        self.assertIn('showScreen("chat")', js)

    def test_blob_hooks_present_with_blob_visual(self):
        # Builder A's part was the hook only; Builder B landed
        # the visual on top of it (BlobTest below covers the
        # detail). The hook contract stays: a .bot-blob element
        # with data-bot-id and the initials inside, on drawer
        # rows, the chat pill, message avatars and room chips —
        # and the morph + per-bot hue must now EXIST.
        js = JS.read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("bot-blob", js)
        self.assertIn("data-bot-id", js)
        self.assertIn("setChatPill", js)
        self.assertIn('class="chat-avatar bot-blob bot-blob--lg blob-v0"',
                      html)
        self.assertIn("blob-morph", css)
        self.assertIn("@keyframes blob-morph", css)
        self.assertIn("--blob-hue", css)


class BlobTest(unittest.TestCase):
    """0.6.0 living blobs (Builder B, the owner 2026-10-25): each bot
    gets ONE organic blob — an odd, non-circle shape in its own
    colour — derived deterministically from the bot id. Pure
    CSS morph + per-bot hue; no images, no library. The three
    states (idle / working / typing) come only from real state:
    workingState (the same flag behind the Stop pill) and the
    #typing indicator (the same showTyping/hideTyping trigger
    as the "mengetik…" line). Static markers only, except the
    derivation test, which runs the shipped JS function in
    node and checks its actual outputs."""

    def _blob_fn_source(self):
        js = JS.read_text(encoding="utf-8")
        m = re.search(
            r"function blobHash\(id\) \{.*?\n\}\n"
            r"const BLOB_VARIANTS = 5;\n"
            r"function blobStyleFor\(botId\) \{.*?\n\}",
            js, re.S)
        self.assertIsNotNone(
            m, "blob derivation functions not found in app.js")
        return m.group(0)

    def _run_blob_style_for(self, ids):
        import json
        import os
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("node"):
            self.skipTest("node not available to run blobStyleFor")
        harness = (self._blob_fn_source() +
                   "\nconsole.log(JSON.stringify(" +
                   json.dumps(ids) + ".map(blobStyleFor)));")
        with tempfile.NamedTemporaryFile(
                "w", suffix=".mjs", delete=False) as f:
            f.write(harness)
            path = f.name
        try:
            proc = subprocess.run(
                ["node", path], capture_output=True, text=True,
                timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip())

    def test_derivation_is_deterministic_and_distinct(self):
        ids = ["chief-officer", "yt-scriptwriter", "yt-packaging",
               "yt-analyst", "yt-idea-scout", "yt-strategist"]
        # Same id twice -> identical blob; different ids ->
        # different blobs (at least the hue differs).
        results = self._run_blob_style_for(
            ["chief-officer", "chief-officer"] + ids)
        self.assertEqual(results[0], results[1],
                         "same bot id must always derive the same blob")
        by_id = dict(zip(ids, results[2:]))
        hues = {r["hue"] for r in by_id.values()}
        self.assertEqual(len(hues), len(ids),
                         "sample bot ids must all get distinct hues: %s"
                         % by_id)
        pairs = {(r["hue"], r["variant"]) for r in by_id.values()}
        self.assertEqual(len(pairs), len(ids),
                         "sample bot ids must all get distinct blobs: %s"
                         % by_id)
        for r in results:
            self.assertIn("hue", r)
            self.assertIn("variant", r)
            self.assertGreaterEqual(r["hue"], 0)
            self.assertLess(r["hue"], 360)
            self.assertGreaterEqual(r["variant"], 0)
            self.assertLess(r["variant"], 5)
        # No bot id yet (the "General chat" pill) -> the old
        # amber, hue 38, variant 0 — never a random colour.
        self.assertEqual(self._run_blob_style_for([""])[0],
                         {"hue": 38, "variant": 0})
        self.assertEqual(self._run_blob_style_for([None])[0],
                         {"hue": 38, "variant": 0})

    def test_morph_keyframes_and_three_states_in_css(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("@keyframes blob-morph", css)
        for v in range(5):
            self.assertIn(".blob-v%d" % v, css)
        self.assertIn("--blob-hue", css)
        self.assertIn(".blob-working", css)
        self.assertIn(".blob-typing", css)
        # Idle breathes slowly (9s morph), no glow: the base
        # blob rule carries no box-shadow.
        morph = re.search(r"animation:blob-morph\s+([\d.]+)s", css)
        self.assertIsNotNone(morph, "no blob-morph animation duration")
        self.assertEqual(float(morph.group(1)), 9.0)
        base = re.search(r"\.bot-avatar\.bot-blob[^{]*\{[^}]*\}", css)
        self.assertIsNotNone(base, "no base blob visual rule")
        self.assertNotIn("box-shadow", base.group(0))
        # Working pulses faster (2.2s) + glow in the bot's hue.
        working = re.search(
            r"\.blob-working[^{]*\{[^}]*\}", css)
        self.assertIsNotNone(working, "no .blob-working rule")
        self.assertIn("2.2s", working.group(0))
        self.assertIn("box-shadow", working.group(0))
        self.assertIn("var(--blob-hue", working.group(0))
        # Typing shows three dots (via ::after + box-shadow),
        # not a pulse — and hides the initials.
        typing = re.search(r"\.blob-typing::after\s*\{[^}]*\}", css)
        self.assertIsNotNone(typing, "no .blob-typing::after dots rule")
        self.assertIn("box-shadow", typing.group(0))
        self.assertIn("typing-blink", typing.group(0))
        typing_hide = re.search(
            r"\.bot-blob\.blob-typing\s*\{[^}]*\}", css)
        self.assertIsNotNone(typing_hide, "no .blob-typing rule")
        self.assertIn("color:transparent",
                      typing_hide.group(0).replace(" ", ""))
        # The chat-pill blob is the large one.
        self.assertIn(".bot-blob--lg", css)
        lg = re.search(r"\.bot-blob--lg\s*\{[^}]*\}", css)
        self.assertIsNotNone(lg, "no .bot-blob--lg rule")
        self.assertIn("44px", lg.group(0))

    def test_state_classes_only_from_real_state_paths(self):
        js = JS.read_text(encoding="utf-8")
        # Every occurrence of the working/typing blob classes
        # must sit inside syncBlobStates (the single applier,
        # driven by workingState + #typing visibility), inside
        # renderDeckBots next to the isWorking trigger (room
        # chips bake it at render time), or in a comment.
        for cls, allowed in (("blob-working",
                              {"syncBlobStates", "renderDeckBots"}),
                             ("blob-typing", {"syncBlobStates"})):
            for m in re.finditer(re.escape(cls), js):
                line_start = js.rfind("\n", 0, m.start()) + 1
                line = js[line_start:js.find("\n", m.start())]
                if line.lstrip().startswith(("*", "//")):
                    continue  # comment line, not code
                before = js[:m.start()]
                fns = re.findall(r"function (\w+)\(", before)
                self.assertTrue(
                    fns, "%s used outside any function" % cls)
                self.assertIn(
                    fns[-1], allowed,
                    "%s applied in %s, not a real-state path"
                    % (cls, fns[-1]))
        # syncBlobStates itself reads only the two real
        # triggers — no timer, no invented state.
        sync = re.search(r"function syncBlobStates\(\) \{.*?\n\}",
                         js, re.S)
        self.assertIsNotNone(sync, "no syncBlobStates function")
        body = sync.group(0)
        self.assertIn("workingState", body)
        self.assertIn('getElementById("typing")', body)
        self.assertNotIn("setTimeout", body)
        self.assertNotIn("setInterval", body)
        # And it is called from the existing state-change
        # points, so a blob never keeps a state its bot left.
        for caller in ("showTyping", "hideTyping", "renderBotList",
                       "setChatPill", "createStreamingBubble"):
            fn = re.search(r"function %s\([^)]*\) \{.*?\n\}" % caller,
                           js, re.S)
            self.assertIsNotNone(fn, "no %s function" % caller)
            self.assertIn("syncBlobStates()", fn.group(0),
                          "%s never syncs blob states" % caller)

    def test_all_four_render_sites_use_blob(self):
        js = JS.read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # Drawer rows, message avatars, the streaming bubble and
        # room chips all stamp the derived blob class + hue; the
        # chat pill does the same via setChatPill, at large size.
        for fn in ("renderBotList", "renderMessage",
                   "createStreamingBubble", "renderDeckBots"):
            body = re.search(r"function %s\([^)]*\) \{.*?\n\}" % fn,
                             js, re.S)
            self.assertIsNotNone(body, "no %s function" % fn)
            self.assertIn("blobClass(", body.group(0),
                          "%s does not stamp blobClass" % fn)
            self.assertIn("blobStyle(", body.group(0),
                          "%s does not stamp blobStyle" % fn)
            # The initials stay inside the blob, everywhere.
            self.assertIn("voInitials(", body.group(0),
                          "%s lost the initials" % fn)
        pill = re.search(r"function setChatPill\([^)]*\) \{.*?\n\}",
                         js, re.S)
        self.assertIsNotNone(pill, "no setChatPill function")
        self.assertIn("blobStyleFor(", pill.group(0))
        self.assertIn("bot-blob--lg", pill.group(0))
        self.assertIn("voInitials(", pill.group(0))
        self.assertIn(
            'class="chat-avatar bot-blob bot-blob--lg blob-v0"', html)


class StaticRoutesTest(unittest.TestCase):
    """Every local /static/* file index.html loads must have a
    matching route in server.py — otherwise the browser gets a
    404 and the boot dies.

    Regression: 0.5.0 shipped local-data.js in index.html but no
    /static/local-data.js route, so ServerLink was undefined and
    app.js:2330 threw "ServerLink is not defined" — the list
    stayed empty and the connection pill stuck on "Memeriksa".
    """

    def test_every_local_static_reference_is_served(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        server = (Path(__file__).resolve().parents[2] / "app"
                  / "server.py").read_text(encoding="utf-8")
        refs = set(re.findall(r'(?:src|href)="/static/([^"]+)"', html))
        self.assertTrue(refs, "no local /static/ references found")
        for ref in sorted(refs):
            route = 'path == "/static/%s"' % ref
            self.assertIn(route, server,
                          "no server route for /static/%s" % ref)
            self.assertTrue((STATIC / ref).is_file(),
                            "missing file app/static/%s" % ref)


if __name__ == "__main__":
    unittest.main()
