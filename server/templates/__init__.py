"""Bot templates — import a crew template from a public link.

Our answer to the official app's template gallery
(``ListPublicGrokBotMarketplaceListings``,
``GetGrokBotTemplateForSourceAgent`` / ``ImportDetails`` / ``Version``
/ ``ExportPolicy``, ``CreateGrokBotAgentFromTemplate`` in the RE
report): a template is a pack (kind ``"template"``) that bundles
several characters into one installable crew. See ``README.md`` for
the link format, the expected import flow, and the limits of what
this module can know about the official gallery.

Two hard rules:

* The resolver NEVER invents template content. It fetches the public
  page (no auth) and extracts a definition only when the page
  actually carries one in a recognisable form (embedded JSON first,
  meta tags as a fallback for name/description). An unrecognised
  page is an error, not a guessed template.
* Import STAGES the template as a pack on the shelf. It never
  becomes live characters by itself — installing a template's
  characters is a separate, explicit step (not built yet).
"""

from .importer import (  # noqa: F401
    MAX_PACKAGE_BYTES,
    MAX_SKILLS_PER_PACKAGE,
    REVIEW_TTL_SECONDS,
    TemplateInstallError,
    TemplateImportError,
    TemplateReviewStore,
    install_reviewed_template,
    import_template,
    review_template_package,
    sanitize_template_definition,
    validate_package_bytes,
    validate_template_definition,
)
from .resolve import (TemplateResolveError,  # noqa: F401
                      default_fetcher, parse_template_link,
                      resolve_template)

__all__ = [
    "MAX_PACKAGE_BYTES",
    "MAX_SKILLS_PER_PACKAGE",
    "REVIEW_TTL_SECONDS",
    "TemplateInstallError",
    "TemplateImportError",
    "TemplateResolveError",
    "TemplateReviewStore",
    "default_fetcher",
    "import_template",
    "install_reviewed_template",
    "parse_template_link",
    "resolve_template",
    "review_template_package",
    "sanitize_template_definition",
    "validate_package_bytes",
    "validate_template_definition",
]
