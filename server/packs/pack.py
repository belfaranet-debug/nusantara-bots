"""Plugin-pack format (Dots-style) for the own build.

A pack is one folder on the skill shelf:

    <name>/
      SKILL.md        # kind="skill": frontmatter + body (loader format)
      characters/     # kind="crew": one .md file per character
      README.md       # kind="crew": crew description
      SOURCE.md       # required when imported from outside (origin + license)
      manifest.json   # written by packs.convert, read by packs.pack

The manifest is metadata ONLY. The skill loader keeps reading SKILL.md
and ignores manifest.json, so converting a pack can never break loading.

Two rules are enforced here because the upstream format they were modelled
on gets them wrong:

1. No secrets in manifests. The upstream .mcp.json format ships OAuth
   client secrets in plain text in a public repo. Our validator rejects
   ANY manifest containing a key whose name looks like a credential
   (secret/token/password/api_key/client_secret/...) anywhere in the
   document. Credentials stay BYO: browser localStorage only.
2. Human review is mandatory. Every manifest must list the six default
   actions that require a human before they happen (send, publish, merge,
   deploy, delete, change-production-data). A pack may require review for
   MORE actions, never fewer.

Template packages (kind ``"template"``) carry a third layer, the
``grokbot.team`` package rules (see ``templates/importer.py`` and
``templates/README.md``): a strict schema, a 4 MB / 60-skill cap,
forbidden fields that reject the package outright (chat history,
secrets, model choice, computer, visibility), unknown fields that
are stripped and reported, secret-like values that are rejected and
never echoed, and — at install time — connections that start
DISABLED and routines that start PAUSED. The shared helpers for
those rules live here so the pack validator enforces the same
rules for kind ``"template"`` that the importer enforces on the
way in: ``FORBIDDEN_FIELDS`` / ``find_forbidden_fields``,
``find_secret_values`` / ``redact_secrets`` / ``redact_text``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# The loader lives next door; absolute import from the project root,
# matching the convention the loader and app modules use.
from loader.skills import MAX_DESCRIPTION_CHARS  # noqa: F401  (re-exported)

#: Actions that ALWAYS require a human review before they happen.
#: Taken from the Dots curation rule (send/publish/merge/deploy/delete/
#: change-production-data). A pack may add more, never fewer.
DEFAULT_REVIEW_ACTIONS = [
    "send",
    "publish",
    "merge",
    "deploy",
    "delete",
    "change-production-data",
]

#: Key names that must never appear anywhere in a manifest. Matched
#: case-insensitively against every dict key, at any depth.
_SECRET_KEY_RE = re.compile(
    r"secret|token|password|passwd|api[-_]?key|client_secret|bearer|credential",
    re.IGNORECASE,
)

PACK_KINDS = ("skill", "crew", "template")
MAX_REQUIRES = 16

#: Our own template package format. A ``kind="template"`` pack is a
#: ``grokbot.team`` package: ``template.json`` (+ ``manifest.json`` +
#: ``README.md``) on the shelf. The name is ours; it is not borrowed
#: from any other format. ``template.json`` may carry the format
#: marker (``"format": "grokbot.team"``, ``"format_version": 1``);
#: packs written before the marker existed are still accepted, but
#: a marker with any other value is rejected.
FORMAT_NAME = "grokbot.team"
FORMAT_VERSION = 1

#: Hard caps for a ``grokbot.team`` package (enforced by the importer
#: and by the pack validator for kind "template").
MAX_PACKAGE_BYTES = 4 * 1024 * 1024  # 4 MB of serialised JSON
MAX_SKILLS_PER_PACKAGE = 60          # distinct skills, top-level + per-character
MAX_ROUTINES_PER_PACKAGE = 16
MAX_CONNECTIONS_PER_PACKAGE = 16

#: Fields that must NEVER appear in a template package, at any depth.
#: A package carrying one is REJECTED outright — never stripped
#: silently, because a sender who smuggles a sensitive field must
#: not learn that silence means acceptance. Matched
#: case-insensitively against every dict key.
#:
#: * chat history — a template must never carry past conversations
#:   (``chat_history`` / ``history`` / ``messages`` / ``conversation``
#:   / ``chat`` / ``transcript``);
#: * secrets / credentials — never in a package, in any spelling;
#: * model choice — the user picks the model, never the template;
#: * computer / device — a template must never name a machine;
#: * visibility — a template must never decide who can see it.
FORBIDDEN_FIELDS = frozenset({
    "chat_history", "history", "messages", "conversation", "chat",
    "transcript",
    "secret", "secrets", "api_key", "api_keys", "apikey", "token",
    "tokens", "password", "passwords", "passwd", "credentials",
    "credential", "client_secret", "access_key", "private_key",
    "auth", "authorization", "bearer",
    "model", "model_id", "model_name", "llm", "engine",
    "computer", "computers", "device", "devices", "host", "hostname",
    "machine",
    "visibility", "public", "is_public", "private", "is_private",
    "shared",
})
_FORBIDDEN_LOWER = frozenset(f.lower() for f in FORBIDDEN_FIELDS)

#: Secret-looking VALUE patterns (well-known token prefixes and
#: ``key = value`` assignments with a token-shaped value). Used by
#: ``looks_like_secret`` / ``find_secret_values`` / ``redact_text``:
#: a secret-like value inside an otherwise allowed field (a prompt,
#: a description, a URL) rejects the package, and is never echoed
#: in an error, a log line, or a review payload. Deliberately
#: prefix-based, not entropy-based, so ordinary prose ("use your
#: API key", "YOUR_KEY_HERE") does not trip it.
_SECRET_VALUE_RES = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|passwd)"
               r"\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
)

#: Placeholder that replaces any detected secret in output.
REDACTED = "[REDACTED]"


@dataclass
class PackResult:
    """Outcome of validating (or building) one pack."""

    name: str
    ok: bool
    errors: list = field(default_factory=list)  # [{code, message}]
    manifest: dict | None = None

    def error_codes(self) -> list:
        return [e["code"] for e in self.errors]


def _err(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def find_secret_keys(value, path: str = "") -> list:
    """Return the dotted paths of every secret-like key in a nested value."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                found.append(child_path)
            found.extend(find_secret_keys(child, child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(find_secret_keys(item, f"{path}[{idx}]"))
    return found


def find_forbidden_fields(value, path: str = "") -> list:
    """Return the dotted paths of every forbidden field in a nested
    value. A forbidden field is a ``FORBIDDEN_FIELDS`` key at any
    depth (matched case-insensitively). Values are never inspected
    here — only key names."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in _FORBIDDEN_LOWER:
                found.append(child_path)
            found.extend(find_forbidden_fields(child, child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(find_forbidden_fields(item, f"{path}[{idx}]"))
    return found


def looks_like_secret(value) -> bool:
    """True when a string value looks like a real secret (a known
    token prefix or a ``key = <token>`` assignment). Non-strings
    are never secrets."""
    return (isinstance(value, str)
            and any(rx.search(value) for rx in _SECRET_VALUE_RES))


def find_secret_values(value, path: str = "") -> list:
    """Return the dotted paths of every string VALUE that looks like
    a secret. The paths name the field; the value itself is never
    returned, so the result is safe to put in an error message."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(child, str) and looks_like_secret(child):
                found.append(child_path)
            else:
                found.extend(find_secret_values(child, child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(find_secret_values(item, f"{path}[{idx}]"))
    return found


def redact_text(text: str) -> str:
    """Replace any secret-looking substring in free text with
    ``[REDACTED]``. Run every error message through this before it
    leaves the validator/importer, so a secret can never leak via
    an echoed value."""
    if not isinstance(text, str):
        return text
    for rx in _SECRET_VALUE_RES:
        text = rx.sub(REDACTED, text)
    return text


def redact_secrets(value):
    """Deep-copy ``value`` with every secret replaced by
    ``[REDACTED]``: a secret-like KEY keeps its name but loses its
    value, and a secret-looking string VALUE becomes the
    placeholder. Safe to serialise into logs, errors, and review
    payloads."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_secrets(child)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and looks_like_secret(value):
        return REDACTED
    return value


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_template_routines(template_def, character_names) -> list:
    """Structural validation of a template's ``routines`` list.

    A routine is ``{"title", "character", "schedule"}``; the
    character must be one of the template's own characters and the
    schedule must pass the routine scheduler's validator (lazy
    import — if the scheduler cannot be imported, only the
    schedule kind is checked, so the pack validator never raises).
    There is NO ``enabled`` field in the package schema: routines
    always start paused at install (see templates/importer.py).
    """
    errors = []
    routines = template_def.get("routines")
    if routines is None:
        return errors
    if not isinstance(routines, list):
        return [_err("bad_template_routines",
                     "'routines' must be a list")]
    if len(routines) > MAX_ROUTINES_PER_PACKAGE:
        errors.append(_err("template_too_many_routines",
                           "'routines' has %d entries, limit is %d"
                           % (len(routines), MAX_ROUTINES_PER_PACKAGE)))
    try:
        from routines.scheduler import validate_schedule
    except Exception:
        validate_schedule = None
    for idx, routine in enumerate(routines):
        base = "routines[%d]" % idx
        if not isinstance(routine, dict):
            errors.append(_err("bad_template_routine",
                               "%s must be an object" % base))
            continue
        title = routine.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(_err("bad_routine_title",
                               "%s needs a non-empty 'title'" % base))
        elif len(title) > 256:
            errors.append(_err("routine_title_too_long",
                               "%s 'title' is too long" % base))
        character = routine.get("character")
        if not isinstance(character, str) or not character.strip():
            errors.append(_err("bad_routine_character",
                               "%s needs a non-empty 'character'" % base))
        elif character not in character_names:
            errors.append(_err("routine_unknown_character",
                               "%s names character %r, which is not in "
                               "this template" % (base, character)))
        schedule = routine.get("schedule")
        if not isinstance(schedule, dict):
            errors.append(_err("bad_routine_schedule",
                               "%s needs a 'schedule' object" % base))
        elif validate_schedule is not None:
            try:
                validate_schedule(schedule)
            except ValueError as exc:
                errors.append(_err("bad_routine_schedule",
                                   "%s has an invalid schedule: %s"
                                   % (base, exc)))
        elif schedule.get("kind") not in ("interval", "daily", "cron"):
            errors.append(_err("bad_routine_schedule",
                               "%s 'schedule.kind' must be one of "
                               "interval/daily/cron" % base))
    return errors


def _validate_template_connections(template_def) -> list:
    """Structural validation of a template's ``connections`` list.

    A connection is ``{"name", "type", "url" | "command", "tools"?}``
    — an MCP server the template brings. There is NO ``enabled``
    field in the package schema: connections always start DISABLED
    at install (see templates/importer.py); a human enables each
    one explicitly afterwards.
    """
    errors = []
    connections = template_def.get("connections")
    if connections is None:
        return errors
    if not isinstance(connections, list):
        return [_err("bad_template_connections",
                     "'connections' must be a list")]
    if len(connections) > MAX_CONNECTIONS_PER_PACKAGE:
        errors.append(_err("template_too_many_connections",
                           "'connections' has %d entries, limit is %d"
                           % (len(connections),
                              MAX_CONNECTIONS_PER_PACKAGE)))
    seen = set()
    for idx, conn in enumerate(connections):
        base = "connections[%d]" % idx
        if not isinstance(conn, dict):
            errors.append(_err("bad_template_connection",
                               "%s must be an object" % base))
            continue
        name = conn.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(_err("bad_connection_name",
                               "%s needs a non-empty 'name'" % base))
        elif len(name) > 64:
            errors.append(_err("connection_name_too_long",
                               "%s 'name' is too long" % base))
        elif name in seen:
            errors.append(_err("duplicate_connection_name",
                               "connection name %r appears twice" % name))
        else:
            seen.add(name)
        conn_type = conn.get("type")
        if conn_type not in ("http", "stdio"):
            errors.append(_err("bad_connection_type",
                               "%s.type must be 'http' or 'stdio'" % base))
        elif conn_type == "http":
            url = conn.get("url")
            if not isinstance(url, str) or not url.strip():
                errors.append(_err("bad_connection_url",
                                   "%s.url must be a non-empty string"
                                   % base))
            elif not (url.startswith("http://")
                      or url.startswith("https://")):
                errors.append(_err("bad_connection_url",
                                   "%s.url must start with http:// or "
                                   "https://" % base))
        else:
            command = conn.get("command")
            if not isinstance(command, str) or not command.strip():
                errors.append(_err("bad_connection_command",
                                   "%s.command must be a non-empty string"
                                   % base))
        tools = conn.get("tools")
        if tools is not None and not _is_str_list(tools):
            errors.append(_err("bad_connection_tools",
                               "%s.tools must be a list of strings" % base))
    return errors


def validate_manifest(manifest: dict, folder: Path) -> PackResult:
    """Validate a manifest dict against its pack folder. Never raises."""
    folder = Path(folder)
    name = folder.name
    errors: list = []

    if not isinstance(manifest, dict):
        return PackResult(name=name, ok=False,
                          errors=[_err("manifest_not_object",
                                       "manifest.json must contain a JSON object")])

    # --- identity -------------------------------------------------------
    m_name = manifest.get("name")
    if not isinstance(m_name, str) or not m_name:
        errors.append(_err("missing_name", "manifest has no 'name'"))
    elif m_name != name:
        errors.append(_err("name_mismatch",
                           "manifest name %r != folder name %r" % (m_name, name)))

    kind = manifest.get("kind")
    if kind not in PACK_KINDS:
        errors.append(_err("bad_kind",
                           "manifest 'kind' must be one of %s" % (list(PACK_KINDS),)))

    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        errors.append(_err("missing_version", "manifest has no 'version'"))

    description = manifest.get("description")
    if not isinstance(description, str) or not description:
        errors.append(_err("missing_description", "manifest has no 'description'"))
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(_err("description_too_long",
                           "description is %d chars, limit is %d"
                           % (len(description), MAX_DESCRIPTION_CHARS)))

    # --- skills / characters --------------------------------------------
    skills = manifest.get("skills")
    characters = manifest.get("characters")
    if not _is_str_list(skills):
        errors.append(_err("bad_skills", "'skills' must be a list of strings"))
    if not _is_str_list(characters):
        errors.append(_err("bad_characters", "'characters' must be a list of strings"))
    if kind == "skill" and _is_str_list(skills) and skills != [name]:
        errors.append(_err("skill_skills_mismatch",
                           "a skill pack's 'skills' must be exactly [%r]" % name))
    if kind == "skill" and _is_str_list(characters) and characters:
        errors.append(_err("skill_has_characters",
                           "a skill pack's 'characters' must be empty"))
    if kind == "crew" and _is_str_list(characters) and not characters:
        errors.append(_err("crew_no_characters",
                           "a crew pack's 'characters' must not be empty"))
    if kind == "template" and _is_str_list(characters) and not characters:
        errors.append(_err("template_no_characters",
                           "a template pack's 'characters' must not be empty"))
    if kind == "template" and _is_str_list(skills) \
            and len(set(skills)) > MAX_SKILLS_PER_PACKAGE:
        errors.append(_err("template_too_many_skills",
                           "a template pack declares %d distinct skills, "
                           "limit is %d"
                           % (len(set(skills)), MAX_SKILLS_PER_PACKAGE)))

    # --- source -----------------------------------------------------------
    source = manifest.get("source")
    if not isinstance(source, dict):
        errors.append(_err("bad_source", "'source' must be an object"))
    else:
        origin = source.get("origin")
        if not isinstance(origin, str) or not origin:
            errors.append(_err("missing_source_origin",
                               "'source.origin' must be a non-empty string"))
        elif origin != "own-build":
            license_ = source.get("license")
            if isinstance(license_, str) and license_ == "see SOURCE.md":
                if not (folder / "SOURCE.md").is_file():
                    errors.append(_err("missing_source_md",
                                       "license is 'see SOURCE.md' but no SOURCE.md exists"))
            elif not isinstance(license_, str) or not license_:
                errors.append(_err("missing_source_license",
                                   "imported pack needs 'source.license' or a SOURCE.md"))

    # --- mcp ---------------------------------------------------------------
    mcp = manifest.get("mcp")
    if not isinstance(mcp, dict):
        errors.append(_err("bad_mcp", "'mcp' must be an object"))
    else:
        servers = mcp.get("servers")
        if not isinstance(servers, dict):
            errors.append(_err("bad_mcp_servers", "'mcp.servers' must be an object"))
        else:
            for srv_name, srv in servers.items():
                base = "mcp.servers.%s" % srv_name
                if not isinstance(srv, dict):
                    errors.append(_err("bad_mcp_server", "%s must be an object" % base))
                    continue
                srv_type = srv.get("type")
                if srv_type not in ("http", "stdio"):
                    errors.append(_err("bad_mcp_server_type",
                                       "%s.type must be 'http' or 'stdio'" % base))
                elif srv_type == "http" and (not isinstance(srv.get("url"), str)
                                             or not srv["url"]):
                    errors.append(_err("bad_mcp_server_url",
                                       "%s.url must be a non-empty string" % base))
                elif srv_type == "stdio" and (not isinstance(srv.get("command"), str)
                                              or not srv["command"]):
                    errors.append(_err("bad_mcp_server_command",
                                       "%s.command must be a non-empty string" % base))
                tools = srv.get("tools")
                if tools is not None and not _is_str_list(tools):
                    errors.append(_err("bad_mcp_server_tools",
                                       "%s.tools must be a list of strings" % base))

    # --- human review -------------------------------------------------------
    review = manifest.get("review")
    if not isinstance(review, dict):
        errors.append(_err("bad_review", "'review' must be an object"))
    else:
        required = review.get("required_before")
        if not _is_str_list(required):
            errors.append(_err("bad_review_actions",
                               "'review.required_before' must be a list of strings"))
        else:
            missing = [a for a in DEFAULT_REVIEW_ACTIONS if a not in required]
            if missing:
                errors.append(_err("review_missing_defaults",
                                   "review.required_before is missing %s" % (missing,)))

    # --- requires ------------------------------------------------------------
    requires = manifest.get("requires")
    if not _is_str_list(requires):
        errors.append(_err("bad_requires", "'requires' must be a list of strings"))
    elif len(requires) > MAX_REQUIRES:
        errors.append(_err("too_many_requires",
                           "'requires' has %d entries, limit is %d"
                           % (len(requires), MAX_REQUIRES)))

    # --- no secrets, anywhere -------------------------------------------------
    secret_paths = find_secret_keys(manifest)
    if secret_paths:
        errors.append(_err("secret_in_manifest",
                           "manifest contains credential-like keys: %s"
                           % (", ".join(secret_paths),)))

    # --- folder cross-checks ----------------------------------------------------
    if kind == "skill":
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            errors.append(_err("missing_skill_md", "no SKILL.md in folder %s" % folder))
        else:
            from loader.skills import load_skill
            skill = load_skill(folder)
            if not skill.ok:
                errors.append(_err("skill_invalid",
                                   "SKILL.md failed loader validation: %s"
                                   % (", ".join(skill.error_codes()),)))
    elif kind == "crew":
        chars_dir = folder / "characters"
        if not chars_dir.is_dir():
            errors.append(_err("missing_characters_dir",
                               "no characters/ directory in folder %s" % folder))
        else:
            present = sorted(p.stem for p in chars_dir.glob("*.md"))
            if not present:
                errors.append(_err("crew_no_character_files",
                                   "characters/ has no .md files"))
            elif _is_str_list(characters):
                for stem in characters:
                    if stem not in present:
                        errors.append(_err("character_missing",
                                           "manifest lists character %r but "
                                           "characters/%s.md does not exist"
                                           % (stem, stem)))
        if not (folder / "README.md").is_file():
            errors.append(_err("missing_crew_readme",
                               "a crew pack needs README.md"))
    elif kind == "template":
        # A template pack is a staged crew definition: template.json
        # (the definition) + README.md. The manifest must mirror
        # the definition — same name/version/description and the
        # same character names — and template.json itself must not
        # carry credential-like keys (the no-secrets rule covers
        # the definition, not just the manifest).
        template_path = folder / "template.json"
        template_def = None
        if not template_path.is_file():
            errors.append(_err("missing_template_json",
                               "a template pack needs template.json"))
        else:
            try:
                template_def = json.loads(
                    template_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                errors.append(_err("bad_template_json",
                                   "template.json is unreadable: %s" % exc))
            if isinstance(template_def, dict):
                # --- grokbot.team format marker -------------------
                # Absent = a pack written before the marker existed
                # (accepted); present with another value = rejected.
                fmt = template_def.get("format")
                if fmt is not None and fmt != FORMAT_NAME:
                    errors.append(_err(
                        "bad_template_format",
                        "template.json 'format' must be %r"
                        % FORMAT_NAME))
                fmt_version = template_def.get("format_version")
                if fmt_version is not None \
                        and fmt_version != FORMAT_VERSION:
                    errors.append(_err(
                        "bad_template_format_version",
                        "template.json 'format_version' must be %r"
                        % FORMAT_VERSION))
                # --- forbidden fields: reject, never strip --------
                forbidden = find_forbidden_fields(template_def)
                if forbidden:
                    errors.append(_err(
                        "forbidden_field_in_template_json",
                        "template.json contains forbidden fields (chat "
                        "history, secrets, model choice, computer, "
                        "visibility are never accepted in a package): "
                        "%s" % (", ".join(forbidden),)))
                # --- secret-like values: reject, never echo -------
                secret_values = find_secret_values(template_def)
                if secret_values:
                    errors.append(_err(
                        "secret_value_in_template_json",
                        "template.json contains secret-like values at: "
                        "%s" % (", ".join(secret_values),)))
                if template_def.get("name") != name:
                    errors.append(_err(
                        "template_name_mismatch",
                        "template.json name %r != folder name %r"
                        % (template_def.get("name"), name)))
                if template_def.get("version") != version:
                    errors.append(_err(
                        "template_version_mismatch",
                        "template.json version %r != manifest version %r"
                        % (template_def.get("version"), version)))
                if template_def.get("description") != description:
                    errors.append(_err(
                        "template_description_mismatch",
                        "template.json description != manifest description"))
                t_chars = template_def.get("characters")
                if not isinstance(t_chars, list) or not t_chars:
                    errors.append(_err(
                        "template_no_characters",
                        "template.json 'characters' must be a non-empty list"))
                else:
                    bad = [c for c in t_chars
                           if not isinstance(c, dict)
                           or not isinstance(c.get("name"), str)
                           or not c["name"].strip()
                           or not isinstance(c.get("role"), str)
                           or not c["role"].strip()
                           or not isinstance(c.get("prompt"), str)
                           or not c["prompt"].strip()]
                    if bad:
                        errors.append(_err(
                            "template_bad_characters",
                            "every template character needs a non-empty "
                            "name, role, and prompt"))
                    elif _is_str_list(characters) and sorted(
                            c["name"] for c in t_chars) != sorted(characters):
                        errors.append(_err(
                            "template_characters_mismatch",
                            "template.json characters %s != manifest "
                            "characters %s"
                            % (sorted(c["name"] for c in t_chars),
                               sorted(characters))))
                secret_paths = find_secret_keys(template_def)
                if secret_paths:
                    errors.append(_err(
                        "secret_in_template_json",
                        "template.json contains credential-like keys: %s"
                        % (", ".join(secret_paths),)))
                # --- skill cap (union of top-level + per-character)
                all_skills = set()
                t_skills = template_def.get("skills")
                if _is_str_list(t_skills):
                    all_skills.update(t_skills)
                if isinstance(t_chars, list):
                    for char in t_chars:
                        if isinstance(char, dict) \
                                and _is_str_list(char.get("skills")):
                            all_skills.update(char["skills"])
                if len(all_skills) > MAX_SKILLS_PER_PACKAGE:
                    errors.append(_err(
                        "template_too_many_skills",
                        "template.json declares %d distinct skills, "
                        "limit is %d"
                        % (len(all_skills), MAX_SKILLS_PER_PACKAGE)))
                # --- routines + connections (structural) -----------
                char_names = []
                if isinstance(t_chars, list):
                    char_names = [c.get("name") for c in t_chars
                                  if isinstance(c, dict)
                                  and isinstance(c.get("name"), str)]
                errors.extend(_validate_template_routines(
                    template_def, char_names))
                errors.extend(_validate_template_connections(template_def))
                # --- the manifest itself: no forbidden fields, and
                # every connection it declares must be disabled.
                # A template pack's connections start DISABLED and
                # only a human enables them after install — a
                # manifest that claims otherwise is invalid.
                forbidden_manifest = find_forbidden_fields(manifest)
                if forbidden_manifest:
                    errors.append(_err(
                        "forbidden_field_in_manifest",
                        "manifest contains forbidden fields: %s"
                        % (", ".join(forbidden_manifest),)))
                if isinstance(mcp, dict) \
                        and isinstance(mcp.get("servers"), dict):
                    for srv_name, srv in mcp["servers"].items():
                        if isinstance(srv, dict) \
                                and srv.get("enabled") is not False:
                            errors.append(_err(
                                "template_connection_not_disabled",
                                "mcp.servers.%s must carry "
                                "'enabled': false — template connections "
                                "start disabled until a human approves "
                                "them" % srv_name))
            elif template_def is not None:
                errors.append(_err("bad_template_json",
                                   "template.json must contain a JSON object"))
        if not (folder / "README.md").is_file():
            errors.append(_err("missing_template_readme",
                               "a template pack needs README.md"))

    return PackResult(name=name, ok=not errors, errors=errors,
                      manifest=manifest if not errors else None)


def validate_pack(folder: Path) -> PackResult:
    """Validate one pack folder (reads its manifest.json). Never raises."""
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return PackResult(name=folder.name, ok=False,
                          errors=[_err("missing_manifest",
                                       "no manifest.json in folder %s" % folder)])
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return PackResult(name=folder.name, ok=False,
                          errors=[_err("unreadable_manifest", str(exc))])
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        return PackResult(name=folder.name, ok=False,
                          errors=[_err("bad_manifest_json", str(exc))])
    return validate_manifest(manifest, folder)


def validate_shelf(shelf_dir: Path) -> list:
    """Validate every pack folder in a shelf. Returns one PackResult each."""
    shelf_dir = Path(shelf_dir)
    results = []
    if not shelf_dir.is_dir():
        return results
    for folder in sorted(p for p in shelf_dir.iterdir()
                         if p.is_dir() and not p.name.startswith(".")):
        results.append(validate_pack(folder))
    return results
