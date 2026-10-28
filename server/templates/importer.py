"""Stage a template definition as a pack on the shelf — and install
a reviewed template, safely.

The package format is our own, ``grokbot.team`` (a ``grokbot.package``):
``template.json`` + ``manifest.json`` + ``README.md`` inside a
``kind="template"`` pack folder. Nothing in it is borrowed from any
other format.

Import is STAGING, not installation: the template lands as a pack
that passes the pack validator. Its characters do NOT become live
characters, its routines do NOT start running, and its connections
do NOT become live — installing is a separate, explicit, reviewed
step (see ``review_template_package`` /
``install_reviewed_template`` below).

Security rules for the ``grokbot.team`` format (enforced here AND
by the pack validator for kind ``"template"`` in ``packs/pack.py``,
so the two can never drift apart):

* Strict schema: ``template.json`` must carry the required fields
  with the right types; anything else fails validation with a
  clear error. A package that fails validation is refused, never
  staged half-valid.
* Forbidden fields are REJECTED, never stripped silently: chat
  history, secrets, model choice, computer, visibility — if one
  of those fields is present anywhere in the package, import is
  refused and the field is named. A sender who smuggles a
  sensitive field must not learn that silence means acceptance.
* Unknown, non-sensitive fields are STRIPPED and reported: the
  review payload (and the import result) lists every stripped
  field, so nothing disappears without a trace.
* Secrets are REDACTED everywhere: a secret-like value (known
  token prefixes, ``key = <token>`` assignments) anywhere in the
  package rejects it, and the value itself never appears in an
  error message, a log line, or a review payload — only the field
  path is named, and redacted output shows ``[REDACTED]``.
* Size caps: a package is at most 4 MB of serialised JSON and
  holds at most 60 distinct skills.
* Two-stage install: ``review_template_package`` validates the
  package and returns a review payload (what will be installed:
  characters, skills, routines, connections; which fields were
  stripped; warnings). Nothing is installed at review time.
  ``install_reviewed_template`` installs ONLY with a valid
  ``review_id`` from that review — installing without one fails.
  A review is single-use and expires after 30 minutes.
* Connections start DISABLED: a connection the template brings
  is recorded with ``enabled: false`` (in the manifest's
  ``mcp.servers`` and in ``install-state.json``) until a human
  enables it explicitly. There is no ``enabled`` field in the
  package schema at all — a package cannot pre-enable anything.
* Routines start PAUSED: a routine the template brings is created
  with ``enabled: false`` in the routine store, so it never fires
  until a human resumes it. There is no ``enabled`` field in the
  package schema for routines either.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from packs.pack import (
    DEFAULT_REVIEW_ACTIONS,
    FORMAT_NAME,
    FORMAT_VERSION,
    MAX_CONNECTIONS_PER_PACKAGE,
    MAX_PACKAGE_BYTES,
    MAX_ROUTINES_PER_PACKAGE,
    MAX_SKILLS_PER_PACKAGE,
    REDACTED,
    find_forbidden_fields,
    find_secret_keys,
    find_secret_values,
    redact_secrets,
    redact_text,
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_DESCRIPTION_CHARS = 1024
MAX_CHARACTERS = 16
MAX_CHARACTER_NAME_CHARS = 128
MAX_CHARACTER_ROLE_CHARS = 256
MAX_PROMPT_CHARS = 8000
MAX_SKILL_NAME_CHARS = 64
MAX_ROUTINE_TITLE_CHARS = 256
MAX_CONNECTION_NAME_CHARS = 64
MAX_CONNECTION_URL_CHARS = 2048
MAX_CONNECTION_COMMAND_CHARS = 512

#: A review lives this long; installing with an older review fails.
REVIEW_TTL_SECONDS = 1800  # 30 minutes

#: Fields the resolver attaches that are NOT part of the package:
#: known, carried through for provenance, never staged, never
#: reported as stripped.
_RESOLVER_FIELDS = ("template_id", "source_url", "partial")

#: The package schema. Anything outside these keys at each level is
#: unknown: stripped (and reported) unless it is a forbidden field,
#: which is kept so validation can reject it.
_TEMPLATE_FIELDS = frozenset({
    "name", "version", "description", "characters", "skills",
    "source", "review", "routines", "connections",
    "format", "format_version",
})
_CHARACTER_FIELDS = frozenset({"name", "role", "skills", "prompt"})
_ROUTINE_FIELDS = frozenset({"title", "character", "schedule"})
_SCHEDULE_FIELDS = frozenset({"kind", "seconds", "time", "expression"})
_CONNECTION_FIELDS = frozenset({"name", "type", "url", "command", "tools"})
_SOURCE_FIELDS = frozenset({"origin", "license"})
_REVIEW_FIELDS = frozenset({"required_before"})

# Lower-cased forbidden names, for the sanitiser's keep-vs-strip
# decision (the authoritative set lives in packs/pack.py).
from packs.pack import FORBIDDEN_FIELDS as _FORBIDDEN_FIELDS  # noqa: E402
_FORBIDDEN_LOWER = frozenset(f.lower() for f in _FORBIDDEN_FIELDS)


class TemplateImportError(ValueError):
    """The definition failed validation or the shelf refused it."""


class TemplateInstallError(ValueError):
    """The install step failed: unknown, expired, or already-used
    review; or the staged pack could not be installed."""


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


# -- sanitising: strip unknown fields, report them -------------------

def _strip_mapping(mapping, allowed, path, stripped):
    """Copy ``mapping`` keeping only ``allowed`` keys.

    Unknown keys are dropped and their dotted path recorded in
    ``stripped`` — EXCEPT forbidden keys, which are kept in the
    copy so validation can reject the package for them. Stripping
    a sensitive field silently would hide the attempt.
    """
    cleaned = {}
    for key, value in mapping.items():
        child_path = "%s.%s" % (path, key) if path else str(key)
        if key not in allowed:
            if isinstance(key, str) and key.lower() in _FORBIDDEN_LOWER:
                cleaned[key] = value
            else:
                stripped.append(child_path)
            continue
        cleaned[key] = value
    return cleaned


def sanitize_template_definition(definition):
    """Return ``(cleaned, stripped_fields)``.

    ``cleaned`` is the definition with every unknown, non-sensitive
    field removed (recursively: top level, characters, routines and
    their schedules, connections, source, review). ``stripped_fields``
    lists the dotted paths that were removed, so the review payload
    can show exactly what will not be installed. Forbidden fields
    are NOT stripped — they stay in ``cleaned`` so validation
    rejects the package for them. A non-dict input is returned
    unchanged with no stripped fields (validation reports it).
    """
    if not isinstance(definition, dict):
        return definition, []
    stripped = []
    cleaned = {}
    for key, value in definition.items():
        if key in _RESOLVER_FIELDS:
            continue  # resolver metadata: known, not part of the package
        if key not in _TEMPLATE_FIELDS:
            if isinstance(key, str) and key.lower() in _FORBIDDEN_LOWER:
                cleaned[key] = value
            else:
                stripped.append(str(key))
            continue
        cleaned[key] = value
    # Recurse into the structured sections (only when well-formed
    # enough to walk; anything else is left for validation to
    # reject with a precise error).
    characters = cleaned.get("characters")
    if isinstance(characters, list):
        cleaned["characters"] = [
            _strip_mapping(c, _CHARACTER_FIELDS, "characters[%d]" % i,
                           stripped) if isinstance(c, dict) else c
            for i, c in enumerate(characters)]
    routines = cleaned.get("routines")
    if isinstance(routines, list):
        out = []
        for i, routine in enumerate(routines):
            if not isinstance(routine, dict):
                out.append(routine)
                continue
            entry = _strip_mapping(routine, _ROUTINE_FIELDS,
                                   "routines[%d]" % i, stripped)
            schedule = entry.get("schedule")
            if isinstance(schedule, dict):
                entry["schedule"] = _strip_mapping(
                    schedule, _SCHEDULE_FIELDS,
                    "routines[%d].schedule" % i, stripped)
            out.append(entry)
        cleaned["routines"] = out
    connections = cleaned.get("connections")
    if isinstance(connections, list):
        cleaned["connections"] = [
            _strip_mapping(c, _CONNECTION_FIELDS, "connections[%d]" % i,
                           stripped) if isinstance(c, dict) else c
            for i, c in enumerate(connections)]
    source = cleaned.get("source")
    if isinstance(source, dict):
        cleaned["source"] = _strip_mapping(source, _SOURCE_FIELDS,
                                           "source", stripped)
    review = cleaned.get("review")
    if isinstance(review, dict):
        cleaned["review"] = _strip_mapping(review, _REVIEW_FIELDS,
                                           "review", stripped)
    return cleaned, stripped


# -- validation -------------------------------------------------------

def _validate_routines(definition, character_names, errors):
    """Validate the ``routines`` section (schedule via the routine
    scheduler; character must be one of this template's own)."""
    from routines.scheduler import validate_schedule

    routines = definition.get("routines")
    if routines is None:
        return
    if not isinstance(routines, list):
        errors.append({"code": "bad_template_routines",
                       "message": "'routines' must be a list"})
        return
    if len(routines) > MAX_ROUTINES_PER_PACKAGE:
        errors.append({"code": "template_too_many_routines",
                       "message": "'routines' has %d entries, limit is %d"
                                  % (len(routines),
                                     MAX_ROUTINES_PER_PACKAGE)})
    for idx, routine in enumerate(routines):
        base = "routines[%d]" % idx
        if not isinstance(routine, dict):
            errors.append({"code": "bad_template_routine",
                           "message": "%s must be an object" % base})
            continue
        title = routine.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append({"code": "bad_routine_title",
                           "message": "%s needs a non-empty 'title'" % base})
        elif len(title) > MAX_ROUTINE_TITLE_CHARS:
            errors.append({"code": "routine_title_too_long",
                           "message": "%s 'title' is too long" % base})
        character = routine.get("character")
        if not isinstance(character, str) or not character.strip():
            errors.append({"code": "bad_routine_character",
                           "message": "%s needs a non-empty 'character'"
                                      % base})
        elif character not in character_names:
            errors.append({"code": "routine_unknown_character",
                           "message": "%s names character %r, which is not "
                                      "in this template"
                                      % (base, character)})
        schedule = routine.get("schedule")
        if not isinstance(schedule, dict):
            errors.append({"code": "bad_routine_schedule",
                           "message": "%s needs a 'schedule' object" % base})
        else:
            try:
                validate_schedule(schedule)
            except ValueError as exc:
                errors.append({"code": "bad_routine_schedule",
                               "message": "%s has an invalid schedule: %s"
                                          % (base, exc)})


def _validate_connections(definition, errors):
    """Validate the ``connections`` section (MCP servers the
    template brings). No ``enabled`` field exists in the schema:
    connections always start disabled at install."""
    connections = definition.get("connections")
    if connections is None:
        return
    if not isinstance(connections, list):
        errors.append({"code": "bad_template_connections",
                       "message": "'connections' must be a list"})
        return
    if len(connections) > MAX_CONNECTIONS_PER_PACKAGE:
        errors.append({"code": "template_too_many_connections",
                       "message": "'connections' has %d entries, limit "
                                  "is %d" % (len(connections),
                                             MAX_CONNECTIONS_PER_PACKAGE)})
    seen = set()
    for idx, conn in enumerate(connections):
        base = "connections[%d]" % idx
        if not isinstance(conn, dict):
            errors.append({"code": "bad_template_connection",
                           "message": "%s must be an object" % base})
            continue
        name = conn.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append({"code": "bad_connection_name",
                           "message": "%s needs a non-empty 'name'" % base})
        elif len(name) > MAX_CONNECTION_NAME_CHARS:
            errors.append({"code": "connection_name_too_long",
                           "message": "%s 'name' is too long" % base})
        elif name in seen:
            errors.append({"code": "duplicate_connection_name",
                           "message": "connection name %r appears twice"
                                      % name})
        else:
            seen.add(name)
        conn_type = conn.get("type")
        if conn_type not in ("http", "stdio"):
            errors.append({"code": "bad_connection_type",
                           "message": "%s.type must be 'http' or 'stdio'"
                                      % base})
        elif conn_type == "http":
            url = conn.get("url")
            if not isinstance(url, str) or not url.strip():
                errors.append({"code": "bad_connection_url",
                               "message": "%s.url must be a non-empty "
                                          "string" % base})
            elif len(url) > MAX_CONNECTION_URL_CHARS:
                errors.append({"code": "connection_url_too_long",
                               "message": "%s.url is too long" % base})
            elif not (url.startswith("http://")
                      or url.startswith("https://")):
                errors.append({"code": "bad_connection_url",
                               "message": "%s.url must start with http:// "
                                          "or https://" % base})
        else:
            command = conn.get("command")
            if not isinstance(command, str) or not command.strip():
                errors.append({"code": "bad_connection_command",
                               "message": "%s.command must be a non-empty "
                                          "string" % base})
            elif len(command) > MAX_CONNECTION_COMMAND_CHARS:
                errors.append({"code": "connection_command_too_long",
                               "message": "%s.command is too long" % base})
        tools = conn.get("tools")
        if tools is not None and not _is_str_list(tools):
            errors.append({"code": "bad_connection_tools",
                           "message": "%s.tools must be a list of strings"
                                      % base})


def validate_template_definition(definition):
    """Return a list of {code, message} errors (empty = valid).

    Never raises: a non-dict, a missing field, or a wrong type is
    an error entry, not an exception. Every message is passed
    through ``redact_text`` first, so a secret can never leak via
    an echoed value. Callers should validate the SANITISED
    definition (see ``sanitize_template_definition``); validating
    a raw definition also works — unknown fields are simply
    ignored here, forbidden fields are reported.
    """
    errors = []
    if not isinstance(definition, dict):
        return [{"code": "definition_not_object",
                 "message": "template definition must be a JSON object"}]
    # --- package size -------------------------------------------------
    try:
        size = len(json.dumps(definition, ensure_ascii=False)
                   .encode("utf-8"))
    except (TypeError, ValueError):
        size = None
    if size is not None and size > MAX_PACKAGE_BYTES:
        errors.append({"code": "package_too_large",
                       "message": "package is %d bytes, limit is %d"
                                  % (size, MAX_PACKAGE_BYTES)})
    # --- format marker ------------------------------------------------
    fmt = definition.get("format")
    if fmt is not None and fmt != FORMAT_NAME:
        errors.append({"code": "bad_template_format",
                       "message": "template 'format' must be %r"
                                  % FORMAT_NAME})
    fmt_version = definition.get("format_version")
    if fmt_version is not None and fmt_version != FORMAT_VERSION:
        errors.append({"code": "bad_template_format_version",
                       "message": "template 'format_version' must be %r"
                                  % FORMAT_VERSION})
    # --- forbidden fields: reject, never strip ----------------------
    forbidden = find_forbidden_fields(definition)
    if forbidden:
        errors.append({"code": "forbidden_field",
                       "message": "template contains forbidden fields "
                                  "(chat history, secrets, model choice, "
                                  "computer, visibility are never accepted "
                                  "in a package): %s"
                                  % (", ".join(forbidden),)})
    # --- secret-like values: reject, never echo ---------------------
    secret_values = find_secret_values(definition)
    if secret_values:
        errors.append({"code": "secret_value_detected",
                       "message": "template contains secret-like values "
                                  "at: %s — remove them; values are never "
                                  "shown" % (", ".join(secret_values),)})
    name = definition.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name or ""):
        errors.append({"code": "bad_template_name",
                       "message": "template 'name' must be a folder-safe "
                                  "slug (letters/digits/_/-, max 64 chars)"})
    version = definition.get("version")
    if not isinstance(version, str) or not version.strip():
        errors.append({"code": "missing_template_version",
                       "message": "template has no 'version'"})
    description = definition.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append({"code": "missing_template_description",
                       "message": "template has no 'description'"})
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.append({"code": "template_description_too_long",
                       "message": "description is %d chars, limit is %d"
                                  % (len(description),
                                     MAX_DESCRIPTION_CHARS)})
    characters = definition.get("characters")
    character_names = []
    all_skills = set()
    if not isinstance(characters, list) or not characters:
        errors.append({"code": "template_no_characters",
                       "message": "'characters' must be a non-empty list"})
    elif len(characters) > MAX_CHARACTERS:
        errors.append({"code": "template_too_many_characters",
                       "message": "'characters' has %d entries, limit is %d"
                                  % (len(characters), MAX_CHARACTERS)})
    else:
        seen = set()
        for idx, char in enumerate(characters):
            base = "characters[%d]" % idx
            if not isinstance(char, dict):
                errors.append({"code": "bad_template_character",
                               "message": "%s must be an object" % base})
                continue
            cname = char.get("name")
            if not isinstance(cname, str) or not cname.strip():
                errors.append({"code": "bad_character_name",
                               "message": "%s needs a non-empty 'name'"
                                         % base})
            elif len(cname) > MAX_CHARACTER_NAME_CHARS:
                errors.append({"code": "character_name_too_long",
                               "message": "%s 'name' is too long" % base})
            elif cname in seen:
                errors.append({"code": "duplicate_character_name",
                               "message": "character name %r appears twice"
                                         % cname})
            else:
                seen.add(cname)
                character_names.append(cname)
            role = char.get("role")
            if not isinstance(role, str) or not role.strip():
                errors.append({"code": "bad_character_role",
                               "message": "%s needs a non-empty 'role'"
                                         % base})
            elif len(role) > MAX_CHARACTER_ROLE_CHARS:
                errors.append({"code": "character_role_too_long",
                               "message": "%s 'role' is too long" % base})
            prompt = char.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append({"code": "bad_character_prompt",
                               "message": "%s needs a non-empty 'prompt'"
                                         % base})
            elif len(prompt) > MAX_PROMPT_CHARS:
                errors.append({"code": "character_prompt_too_long",
                               "message": "%s 'prompt' is too long" % base})
            skills = char.get("skills")
            if skills is not None and not _is_str_list(skills):
                errors.append({"code": "bad_character_skills",
                               "message": "%s 'skills' must be a list of "
                                          "strings" % base})
            elif isinstance(skills, list) and any(
                    len(s) > MAX_SKILL_NAME_CHARS for s in skills):
                errors.append({"code": "character_skill_too_long",
                               "message": "%s has a skill name that is too "
                                          "long" % base})
            elif isinstance(skills, list):
                all_skills.update(skills)
    skills = definition.get("skills")
    if skills is not None and not _is_str_list(skills):
        errors.append({"code": "bad_template_skills",
                       "message": "'skills' must be a list of strings"})
    elif isinstance(skills, list):
        all_skills.update(skills)
    if len(all_skills) > MAX_SKILLS_PER_PACKAGE:
        errors.append({"code": "too_many_skills",
                       "message": "package declares %d distinct skills, "
                                  "limit is %d"
                                  % (len(all_skills),
                                     MAX_SKILLS_PER_PACKAGE)})
    _validate_routines(definition, character_names, errors)
    _validate_connections(definition, errors)
    source = definition.get("source")
    if source is not None:
        if not isinstance(source, dict) \
                or not isinstance(source.get("origin"), str) \
                or not source["origin"].strip():
            errors.append({"code": "bad_template_source",
                           "message": "'source.origin' must be a non-empty "
                                      "string when present"})
    review = definition.get("review")
    if review is not None:
        if not isinstance(review, dict) \
                or not _is_str_list(review.get("required_before")):
            errors.append({"code": "bad_template_review",
                           "message": "'review.required_before' must be a "
                                      "list of strings when present"})
        else:
            missing = [a for a in DEFAULT_REVIEW_ACTIONS
                       if a not in review["required_before"]]
            if missing:
                errors.append({"code": "review_missing_defaults",
                               "message": "review.required_before is "
                                          "missing %s" % (missing,)})
    secret_paths = find_secret_keys(definition)
    if secret_paths:
        errors.append({"code": "secret_in_template",
                       "message": "template contains credential-like keys: "
                                  "%s" % (", ".join(secret_paths),)})
    if definition.get("partial"):
        errors.append({"code": "template_partial",
                       "message": "definition is partial (name/description "
                                  "only, no characters) — a partial "
                                  "definition cannot be staged as a pack"})
    # A secret must never leak through an echoed value in any
    # message (e.g. a duplicated character name that happens to
    # look like a token).
    return [{"code": e["code"], "message": redact_text(e["message"])}
            for e in errors]


def validate_package_bytes(raw):
    """Check raw package bytes (the upload path for the review
    endpoint) and return the parsed definition dict.

    The byte-size cap is checked BEFORE parsing, so an oversized
    body is refused without ever being decoded. Raises
    TemplateImportError with a redacted message.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TemplateImportError("package body must be bytes")
    if len(raw) > MAX_PACKAGE_BYTES:
        raise TemplateImportError(
            "package is %d bytes, limit is %d"
            % (len(raw), MAX_PACKAGE_BYTES))
    try:
        definition = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TemplateImportError(
            "package is not valid JSON: %s"
            % redact_text(str(exc))) from exc
    if not isinstance(definition, dict):
        raise TemplateImportError("package must contain a JSON object")
    return definition


# -- review: two-stage install -----------------------------------------

def _review_payload(cleaned, stripped, review_id, source_url=None):
    """The payload the review screen shows: what WILL be installed,
    what was stripped, and the warnings. Fully redacted — prompts
    appear only as a short preview (first 200 chars) plus their
    length, never in full, and every value passes through
    ``redact_secrets``."""
    from routines.scheduler import validate_schedule

    characters = []
    for char in cleaned["characters"]:
        prompt = char["prompt"]
        characters.append({
            "name": char["name"],
            "role": char["role"],
            "skills": list(char.get("skills") or []),
            "prompt_chars": len(prompt),
            "prompt_preview": prompt[:200],
        })
    routines = []
    for routine in cleaned.get("routines") or []:
        routines.append({
            "title": routine["title"],
            "character": routine["character"],
            "schedule": validate_schedule(routine["schedule"]),
            "enabled": False,
            "note": "starts PAUSED — resume it explicitly after install",
        })
    connections = []
    for conn in cleaned.get("connections") or []:
        entry = {"name": conn["name"], "type": conn["type"],
                 "enabled": False,
                 "note": "starts DISABLED — enable it explicitly "
                         "after install"}
        if conn["type"] == "http":
            entry["url"] = conn["url"]
        else:
            entry["command"] = conn["command"]
        if conn.get("tools"):
            entry["tools"] = list(conn["tools"])
        connections.append(entry)
    all_skills = set(cleaned.get("skills") or [])
    for char in cleaned["characters"]:
        all_skills.update(char.get("skills") or [])
    size = len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8"))
    warnings = []
    if connections:
        warnings.append(
            "%d connection(s) will start DISABLED — each one needs "
            "explicit human approval before it can be used."
            % len(connections))
    if routines:
        warnings.append(
            "%d routine(s) will start PAUSED — each one needs to be "
            "resumed explicitly before it can fire."
            % len(routines))
    if stripped:
        warnings.append(
            "%d unknown field(s) were stripped from the package and "
            "will not be installed: %s"
            % (len(stripped), ", ".join(stripped)))
    source = cleaned.get("source") or {"origin": "own-build"}
    payload = {
        "review_id": review_id,
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "package": {
            "name": cleaned["name"],
            "version": cleaned["version"],
            "description": cleaned["description"],
            "size_bytes": size,
            "character_count": len(cleaned["characters"]),
            "skill_count": len(all_skills),
            "routine_count": len(routines),
            "connection_count": len(connections),
        },
        "characters": characters,
        "skills": sorted(all_skills),
        "routines": routines,
        "connections": connections,
        "source": {"origin": source["origin"]},
        "stripped_fields": list(stripped),
        "warnings": warnings,
        "requires_review_before": list(DEFAULT_REVIEW_ACTIONS),
        "expires_in_seconds": REVIEW_TTL_SECONDS,
    }
    if source_url:
        payload["source"]["link"] = source_url
    return redact_secrets(payload)


def _as_epoch(now):
    """Epoch seconds for ``now``: None (now), a number, a datetime,
    or an ISO string."""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        dt = now if now.tzinfo is not None else now.replace(
            tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(now, str):
        try:
            dt = datetime.fromisoformat(now)
        except ValueError:
            raise TemplateInstallError(
                "now must be a datetime, ISO string, or epoch number")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return float(now)


class TemplateReviewStore:
    """Pending template reviews: in-memory, single-use, TTL'd.

    ``create`` validates + sanitises a package and opens a review
    for it (nothing is installed). ``take`` hands the stored
    definition to the installer and marks the review used, so a
    second install with the same id fails; ``release`` undoes a
    take when staging failed, so the review stays usable for a
    retry. Reviews older than the TTL are refused. Thread-safe;
    the server holds one instance on AppState.
    """

    def __init__(self, ttl_seconds=REVIEW_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._reviews = {}

    def create(self, definition, source_url=None, now=None):
        """Validate + sanitise ``definition`` and open a review.

        Returns the review payload. Raises TemplateImportError
        when the package is rejected (same errors as
        ``import_template`` would raise).
        """
        cleaned, stripped = sanitize_template_definition(definition)
        errors = validate_template_definition(cleaned)
        if errors:
            raise TemplateImportError(
                "template definition invalid: %s"
                % "; ".join("%s: %s" % (e["code"], e["message"])
                            for e in errors))
        review_id = uuid.uuid4().hex
        payload = _review_payload(cleaned, stripped, review_id,
                                  source_url=source_url)
        created = _as_epoch(now)
        with self._lock:
            self._reviews[review_id] = {
                "review_id": review_id,
                "definition": cleaned,
                "stripped_fields": stripped,
                "payload": payload,
                "created_at": created,
                "installed": False,
            }
        return payload

    def get(self, review_id):
        """The stored payload for ``review_id``, or None. A copy:
        callers cannot mutate the stored review."""
        with self._lock:
            rec = self._reviews.get(review_id)
            if rec is None:
                return None
            return json.loads(json.dumps(rec["payload"]))

    def _is_expired(self, rec, now):
        return (now - rec["created_at"]) > self.ttl_seconds

    def take(self, review_id, now=None):
        """Hand out the stored definition and mark the review used.

        Raises TemplateInstallError for an unknown, expired, or
        already-used review id.
        """
        now = _as_epoch(now)
        with self._lock:
            rec = self._reviews.get(review_id)
            if rec is None:
                raise TemplateInstallError(
                    "unknown review %r — review the template first"
                    % (review_id,))
            if self._is_expired(rec, now):
                raise TemplateInstallError(
                    "review %r expired — review the template again"
                    % (review_id,))
            if rec["installed"]:
                raise TemplateInstallError(
                    "review %r was already installed — review the "
                    "template again to install it a second time"
                    % (review_id,))
            rec["installed"] = True
            return json.loads(json.dumps(rec["definition"]))

    def release(self, review_id):
        """Undo a ``take`` (staging failed): the review becomes
        usable again until it expires."""
        with self._lock:
            rec = self._reviews.get(review_id)
            if rec is not None:
                rec["installed"] = False


#: Process-wide default review store (convenience for direct
#: callers; the server owns its own instance on AppState).
_DEFAULT_REVIEW_STORE = TemplateReviewStore()


def review_template_package(definition, store=None, source_url=None):
    """Validate a package and open a review for it.

    Returns the review payload (what will be installed, what was
    stripped, warnings). Installs NOTHING. Raises
    TemplateImportError when the package is rejected.
    """
    store = store or _DEFAULT_REVIEW_STORE
    return store.create(definition, source_url=source_url)


# -- staging (the pack on the shelf) ------------------------------------

def _normalise_definition(definition):
    """The template.json that gets staged: only our fields, in
    order, with review defaults filled in when absent. Routines
    keep their cleaned schedules; connections keep only their
    schema fields. Neither carries an ``enabled`` flag — that is
    install-time state, never package content."""
    from routines.scheduler import validate_schedule

    characters = []
    for char in definition["characters"]:
        entry = {"name": char["name"].strip(),
                 "role": char["role"].strip(),
                 "skills": list(char.get("skills") or []),
                 "prompt": char["prompt"]}
        characters.append(entry)
    top_skills = list(definition.get("skills") or [])
    for char in characters:
        for skill in char["skills"]:
            if skill not in top_skills:
                top_skills.append(skill)
    source = definition.get("source") or {"origin": "own-build"}
    review = definition.get("review")
    required = list(review["required_before"]) if isinstance(review, dict) \
        else list(DEFAULT_REVIEW_ACTIONS)
    for action in DEFAULT_REVIEW_ACTIONS:
        if action not in required:
            required.append(action)
    origin = source["origin"]
    license_ = source.get("license")
    if origin == "own-build":
        source_out = {"origin": origin}
    elif isinstance(license_, str) and license_.strip() \
            and license_ != "see SOURCE.md":
        source_out = {"origin": origin, "license": license_.strip()}
    else:
        # Imported template, no usable license string: the pack
        # validator accepts "see SOURCE.md" only when the file
        # exists, so import_template writes one (below).
        source_out = {"origin": origin, "license": "see SOURCE.md"}
    staged = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "name": definition["name"],
        "version": definition["version"],
        "description": definition["description"],
        "characters": characters,
        "skills": top_skills,
        "source": source_out,
        "review": {"required_before": required},
    }
    routines = definition.get("routines")
    if routines:
        staged["routines"] = [
            {"title": r["title"], "character": r["character"],
             "schedule": validate_schedule(r["schedule"])}
            for r in routines]
    connections = definition.get("connections")
    if connections:
        staged_connections = []
        for conn in connections:
            entry = {"name": conn["name"], "type": conn["type"]}
            if conn["type"] == "http":
                entry["url"] = conn["url"]
            else:
                entry["command"] = conn["command"]
            if conn.get("tools"):
                entry["tools"] = list(conn["tools"])
            staged_connections.append(entry)
        staged["connections"] = staged_connections
    return staged


def _manifest_for(staged):
    servers = {}
    for conn in staged.get("connections") or []:
        # Connections start DISABLED: the manifest records that
        # explicitly, and the pack validator rejects a template
        # manifest that claims otherwise.
        entry = {"type": conn["type"], "enabled": False}
        if conn["type"] == "http":
            entry["url"] = conn["url"]
        else:
            entry["command"] = conn["command"]
        if conn.get("tools"):
            entry["tools"] = list(conn["tools"])
        servers[conn["name"]] = entry
    return {
        "name": staged["name"],
        "kind": "template",
        "version": staged["version"],
        "description": staged["description"],
        "skills": staged["skills"],
        "characters": [c["name"] for c in staged["characters"]],
        "source": staged["source"],
        "mcp": {"servers": servers},
        "review": staged["review"],
        "requires": [],
    }


def _readme_for(staged, source_url=None):
    lines = ["# Template: %s" % staged["name"], "",
             staged["description"], "",
             "A template pack (kind `template`, format `%s`): one crew,"
             % FORMAT_NAME,
             "staged on the shelf, NOT installed as live characters.",
             "Installing a template's characters is a separate, explicit",
             "step that starts from a review screen.",
             "",
             "## Characters", ""]
    for char in staged["characters"]:
        skills = ", ".join(char["skills"]) if char["skills"] else "no skills"
        lines.append("- **%s** — %s (skills: %s)"
                     % (char["name"], char["role"], skills))
    if staged.get("routines"):
        lines += ["", "## Routines (start PAUSED)", ""]
        for routine in staged["routines"]:
            lines.append("- **%s** — %s, %s"
                         % (routine["title"], routine["character"],
                            routine["schedule"]))
        lines.append("")
        lines.append("Routines created from this template start paused; "
                     "resume each one explicitly.")
    if staged.get("connections"):
        lines += ["", "## Connections (start DISABLED)", ""]
        for conn in staged["connections"]:
            lines.append("- **%s** (%s)" % (conn["name"], conn["type"]))
        lines.append("")
        lines.append("Connections from this template start disabled; "
                     "enable each one explicitly after install.")
    lines += ["", "## Source", "",
              "- Origin: %s" % staged["source"]["origin"]]
    if source_url and source_url != staged["source"]["origin"]:
        lines.append("- Template link: %s" % source_url)
    lines += ["- Version: %s" % staged["version"], ""]
    return "\n".join(lines)


def import_template(definition, shelf_dir, source_url=None):
    """Stage a validated definition as a template pack.

    Sanitises first (unknown fields stripped — the stripped list
    is returned, so nothing disappears silently), then validates
    strictly, then writes ``<shelf>/<name>/`` with
    ``template.json``, ``manifest.json`` and ``README.md``.
    Refuses (raises TemplateImportError) when the definition is
    invalid, when a pack with the same name already exists (no
    overwrite), or when the staged pack would fail the pack
    validator. Returns ``{"name", "path", "manifest",
    "stripped_fields"}``. Never creates character files and never
    touches any other pack. Staging is inert: no live characters,
    no routines created, no connections enabled.
    """
    cleaned, stripped = sanitize_template_definition(definition)
    errors = validate_template_definition(cleaned)
    if errors:
        raise TemplateImportError(
            "template definition invalid: %s"
            % "; ".join("%s: %s" % (e["code"], e["message"])
                        for e in errors))
    staged = _normalise_definition(cleaned)
    shelf = Path(shelf_dir)
    folder = shelf / staged["name"]
    if folder.exists():
        raise TemplateImportError(
            "a pack named %r already exists on the shelf; refusing to "
            "overwrite it" % staged["name"])
    manifest = _manifest_for(staged)
    # The staged pack must pass the validator before anything is
    # written: validate against the not-yet-written folder by
    # checking the manifest + template content in memory first.
    from packs.pack import validate_manifest
    probe = validate_manifest(manifest, folder)
    # validate_manifest's folder cross-checks (template.json,
    # README.md) cannot pass before the files exist, so only the
    # manifest-shape errors count at this stage; the full check
    # runs again after writing, below.
    shape_errors = [e for e in probe.errors
                    if e["code"] not in ("missing_template_json",
                                         "bad_template_json",
                                         "missing_template_readme",
                                         "missing_source_md",
                                         "template_name_mismatch",
                                         "template_version_mismatch",
                                         "template_description_mismatch",
                                         "template_characters_mismatch",
                                         "secret_in_template_json")]
    if shape_errors:
        raise TemplateImportError(
            "staged manifest would fail validation: %s"
            % "; ".join("%s: %s" % (e["code"], e["message"])
                        for e in shape_errors))
    folder.mkdir(parents=True)
    try:
        (folder / "template.json").write_text(
            json.dumps(staged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        (folder / "README.md").write_text(
            _readme_for(staged, source_url=source_url), encoding="utf-8")
        if staged["source"]["origin"] != "own-build":
            # The manifest says license "see SOURCE.md" for imported
            # templates, so the file must exist for the validator.
            (folder / "SOURCE.md").write_text(
                "# Source — %s\n\n" % staged["name"]
                + "- Origin: %s\n" % staged["source"]["origin"]
                + "- License: see the template's source page; the "
                  "content was staged as published.\n"
                + "- Imported via template link"
                  + (" %s" % source_url if source_url else "")
                  + ".\n",
                encoding="utf-8")
    except OSError as e:
        raise TemplateImportError(
            "could not write template pack: %s" % e) from e
    final = validate_manifest(manifest, folder)
    if not final.ok:
        raise TemplateImportError(
            "staged pack failed validation after writing: %s"
            % "; ".join("%s: %s" % (e["code"], e["message"])
                        for e in final.errors))
    return {"name": staged["name"], "path": str(folder),
            "manifest": manifest, "stripped_fields": stripped}


# -- install: the gated step --------------------------------------------

def _install_state_payload(review_id, staged_name, characters, skills,
                           routines_out, connections_out, installed_at):
    return {
        "review_id": review_id,
        "installed_at": installed_at,
        "name": staged_name,
        "characters": characters,
        "skills": skills,
        "routines": routines_out,
        "connections": connections_out,
    }


def install_reviewed_template(review_id, shelf_dir, store=None,
                              routine_store=None, now=None,
                              source_url=None):
    """Install a template that was reviewed first.

    ``review_id`` must come from ``review_template_package`` (or
    ``TemplateReviewStore.create``) — installing without a review
    fails with TemplateInstallError, as does an expired or
    already-used review. On success the pack is staged via
    ``import_template``, every routine the template brings is
    created in the routine store with ``enabled=False`` (paused),
    and every connection is recorded with ``enabled=False``
    (disabled) in the manifest and in ``install-state.json``.
    The review is single-use: a staging failure releases it for a
    retry, a successful install consumes it.

    ``routine_store`` is injectable (tests pass a temp store);
    without one a default ``RoutineStore`` is used. ``now`` is an
    epoch float or a datetime (tests pass a fixed time).

    Returns ``{"name", "path", "review_id", "characters", "skills",
    "routines", "connections", "manifest", "install_state_path"}``.
    """
    store = store or _DEFAULT_REVIEW_STORE
    definition = store.take(review_id, now=now)
    try:
        staged = import_template(definition, shelf_dir,
                                 source_url=source_url)
    except Exception:
        store.release(review_id)
        raise
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = now if now.tzinfo is not None else now.replace(
            tzinfo=timezone.utc)
    else:
        now_dt = datetime.fromtimestamp(float(now), tz=timezone.utc)
    if routine_store is None:
        from routines.scheduler import RoutineStore
        routine_store = RoutineStore()
    routines_out = []
    for routine in definition.get("routines") or []:
        rec = routine_store.create_routine(
            routine["character"], staged["name"], routine["schedule"],
            enabled=False, title=routine["title"], now=now_dt)
        routines_out.append({
            "id": rec["id"],
            "title": rec["title"],
            "character": rec["character"],
            "schedule": rec["schedule"],
            "enabled": False,
        })
    connections_out = []
    for conn in definition.get("connections") or []:
        entry = {"name": conn["name"], "type": conn["type"],
                 "enabled": False}
        if conn["type"] == "http":
            entry["url"] = conn["url"]
        else:
            entry["command"] = conn["command"]
        if conn.get("tools"):
            entry["tools"] = list(conn["tools"])
        connections_out.append(entry)
    folder = Path(staged["path"])
    installed_at = now_dt.isoformat(timespec="seconds")
    state = _install_state_payload(
        review_id, staged["name"],
        [c["name"] for c in definition["characters"]],
        staged["manifest"]["skills"],
        routines_out, connections_out, installed_at)
    state_path = folder / "install-state.json"
    try:
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError as e:
        raise TemplateInstallError(
            "installed the pack but could not write install-state.json: "
            "%s" % e) from e
    return {
        "name": staged["name"],
        "path": staged["path"],
        "review_id": review_id,
        "characters": state["characters"],
        "skills": state["skills"],
        "routines": routines_out,
        "connections": connections_out,
        "manifest": staged["manifest"],
        "install_state_path": str(state_path),
    }
