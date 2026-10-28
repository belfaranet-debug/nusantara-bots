"""Resolve a public template link into a template definition.

Link format (from the official app's App Links, RE report §3):
``x.ai/bot/<id>`` where ``<id>`` is 20 base64url characters. The
official gallery is a vendor page we do not control and cannot log
into, so this module is deliberately defensive:

* ``parse_template_link`` validates the shape only — host, path,
  and the 20-character id. Anything else is a ``ValueError``.
* ``resolve_template`` fetches the PUBLIC page (urllib, no auth,
  short timeout) and extracts a definition ONLY when the page
  carries one in a recognisable form:

  1. embedded JSON (``<script type="application/json">`` or the
     ``__NEXT_DATA__`` blob): walked for an object shaped like a
     template (a ``characters`` list of objects with ``name``);
  2. meta tags (``og:title`` / ``og:description`` and friends):
     name + description only, flagged ``partial``.

  A page in an unrecognised format raises ``TemplateResolveError``
  with a clear message. Content is NEVER invented to fill the gaps:
  a partial definition says it is partial, and an empty page says
  it is empty.

The fetcher is injectable (``fetcher(url) -> str``) so tests can
pass fixture HTML without touching the network. The default fetcher
makes exactly one GET with a short timeout and no credentials.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

#: The template id: base64url characters. The RE report (§3) says
#: ``x.ai/bot/<20 karakter>``, but the one VERIFIED real example —
#: the link the owner clicked, ``Ub3T7usX-c6yRQibQq83P`` — is 21
#: characters (the NanoID default length, same alphabet). The
#: report's "20" was an eyeball count; the parser accepts 20–21 so
#: it never rejects the very link it was built for.
TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,21}$")

#: Accepted link shapes: optional scheme, optional www, host x.ai,
#: path /bot/<id>, optional trailing path/query/fragment.
_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?x\.ai/bot/([A-Za-z0-9_-]{20,21})(?:[/?#].*)?$")

#: Embedded-JSON script blobs to try, in order.
_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)

_META_RE = re.compile(
    r'<meta[^>]+(?:property|name)="([^"]+)"[^>]+content="([^"]*)"',
    re.IGNORECASE)

FETCH_TIMEOUT_SECONDS = 8
USER_AGENT = "grokbot-own-template-resolver/1.0"


class TemplateResolveError(Exception):
    """The link is well-formed but no template definition could be
    extracted from the page (or the page could not be fetched)."""


def parse_template_link(url):
    """Return the template id for a ``x.ai/bot/<id>`` link.

    Raises ValueError when the link is not that shape: wrong host,
    wrong path, or an id that is not exactly 20 base64url
    characters. The message names the problem; it never guesses.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("template link must be a non-empty string")
    url = url.strip()
    m = _LINK_RE.match(url)
    if not m:
        raise ValueError(
            "not a template link: want x.ai/bot/<20-21 character id>, "
            "got %r" % url[:120])
    template_id = m.group(1)
    if not TEMPLATE_ID_RE.match(template_id):
        raise ValueError(
            "template id %r is not 20-21 base64url characters"
            % template_id)
    return template_id


def default_fetcher(url, timeout=FETCH_TIMEOUT_SECONDS):
    """Fetch one public page as text. No auth, no cookies, one GET.

    Raises TemplateResolveError on any transport or HTTP failure,
    and on a non-HTML response — the caller must know the fetch
    failed instead of parsing an error page as a template.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if resp.status != 200:
                raise TemplateResolveError(
                    "template page answered HTTP %d" % resp.status)
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type and "json" not in content_type:
                raise TemplateResolveError(
                    "template page is not HTML/JSON (Content-Type: %s)"
                    % (content_type or "unknown"))
            raw = resp.read()
    except TemplateResolveError:
        raise
    except urllib.error.HTTPError as e:
        raise TemplateResolveError(
            "template page answered HTTP %d" % e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TemplateResolveError(
            "could not fetch template page: %s" % e) from e
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _walk_for_template(value, depth=0):
    """Find a template-shaped object inside parsed JSON.

    A template-shaped object is a dict with a ``characters`` list
    whose items are dicts with a ``name``. The walk also looks
    inside a ``template`` wrapper key. Returns the dict or None.
    Depth-capped so a hostile page cannot recurse us to death.
    """
    if depth > 6:
        return None
    if isinstance(value, dict):
        chars = value.get("characters")
        if (isinstance(chars, list) and chars
                and all(isinstance(c, dict) and isinstance(c.get("name"), str)
                        for c in chars)):
            return value
        for key in ("template", "data", "props", "pageProps"):
            found = _walk_for_template(value.get(key), depth + 1)
            if found is not None:
                return found
        for child in value.values():
            found = _walk_for_template(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _walk_for_template(item, depth + 1)
            if found is not None:
                return found
    return None


def _extract_embedded_json(html_text):
    """Yield parsed JSON blobs from embedded-JSON script tags."""
    for pattern in (_NEXT_DATA_RE, _JSON_SCRIPT_RE, _LD_JSON_RE):
        for raw in pattern.findall(html_text):
            raw = html_lib.unescape(raw).strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue


def _extract_meta(html_text):
    """Name/description from meta tags (og: / twitter:), if any."""
    meta = {}
    for name, content in _META_RE.findall(html_text):
        key = name.strip().lower()
        if key in ("og:title", "twitter:title", "title"):
            meta.setdefault("name", html_lib.unescape(content).strip())
        elif key in ("og:description", "twitter:description",
                     "description"):
            meta.setdefault("description",
                            html_lib.unescape(content).strip())
    return {k: v for k, v in meta.items() if v}


def _normalise_definition(found, template_id, source_url):
    """Keep only the fields our template format defines."""
    definition = {"template_id": template_id, "source_url": source_url}
    if isinstance(found.get("name"), str) and found["name"].strip():
        definition["name"] = found["name"].strip()
    if isinstance(found.get("version"), str) and found["version"].strip():
        definition["version"] = found["version"].strip()
    if isinstance(found.get("description"), str) \
            and found["description"].strip():
        definition["description"] = found["description"].strip()
    if isinstance(found.get("characters"), list):
        definition["characters"] = found["characters"]
    if isinstance(found.get("skills"), list):
        definition["skills"] = found["skills"]
    definition["source"] = {"origin": source_url}
    return definition


def resolve_template(url, fetcher=None):
    """Resolve a template link into a definition dict.

    Returns the definition (see ``_normalise_definition``) plus
    ``partial: True`` when only meta tags were found — a partial
    definition has a name/description at most, never characters.
    Raises ValueError for a malformed link and
    TemplateResolveError when the page cannot be fetched or carries
    no recognisable template definition.
    """
    template_id = parse_template_link(url)
    fetch = fetcher or default_fetcher
    try:
        html_text = fetch(url)
    except TemplateResolveError:
        raise
    except Exception as e:
        raise TemplateResolveError(
            "could not fetch template page: %s" % e) from e
    if not isinstance(html_text, str) or not html_text.strip():
        raise TemplateResolveError(
            "template page %r returned no content" % url)
    for blob in _extract_embedded_json(html_text):
        found = _walk_for_template(blob)
        if found is not None:
            return _normalise_definition(found, template_id, url)
    meta = _extract_meta(html_text)
    if meta:
        definition = {"template_id": template_id, "source_url": url,
                      "partial": True, "source": {"origin": url}}
        definition.update(meta)
        return definition
    raise TemplateResolveError(
        "page at %r carries no recognisable template definition "
        "(no embedded template JSON, no title/description meta tags); "
        "the page format is unknown, so nothing was invented" % url)
