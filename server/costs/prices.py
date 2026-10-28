"""Price table + cost estimation for the own build (FinOps-lite).

The numbers here are ESTIMATES, not a bill. Providers change
prices, and a BYO provider may charge something else entirely
(list price vs. actual invoice, discounts, cached-input pricing,
per-minute audio pricing). The ledger records the estimate next
to the raw token counts, and flags every record whose model has
no known price (``price_known: false``, cost 0) instead of
inventing a number.

The table is configurable: see ``costs/defaults.json`` (shipped
defaults) and the runtime file ``data/cost_settings.json`` —
the owner edits those, never this file. Prices are USD per 1M tokens,
split into input (prompt) and output (completion), matching the
``usage`` object OpenAI-compatible providers return
(``prompt_tokens`` / ``completion_tokens``).

Zero-dependency, Python standard library only.
"""

from __future__ import annotations

import json
import os

# ESTIMATE — see module docstring. Defaults for common
# OpenAI-compatible models, in USD per 1M tokens. These are the
# public list prices as known at build time (Oct 2026); a BYO
# provider can charge differently, so treat every cost_usd the
# ledger reports as an estimate.
DEFAULT_PRICES = {
    "gpt-4o": {"input_per_1m": 2.50, "output_per_1m": 10.00},
    "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    "gpt-4.1": {"input_per_1m": 2.00, "output_per_1m": 8.00},
    "gpt-4.1-mini": {"input_per_1m": 0.40, "output_per_1m": 1.60},
    "gpt-4.1-nano": {"input_per_1m": 0.10, "output_per_1m": 0.40},
    "o4-mini": {"input_per_1m": 1.10, "output_per_1m": 4.40},
    "gpt-5": {"input_per_1m": 1.25, "output_per_1m": 10.00},
    "gpt-5-mini": {"input_per_1m": 0.25, "output_per_1m": 2.00},
    "gpt-5-nano": {"input_per_1m": 0.05, "output_per_1m": 0.40},
}

ESTIMATE_NOTE = (
    "ESTIMASI: harga per-1M token dari tabel konfigurabel "
    "(defaults: tarif publik per Okt 2026). Provider BYO bisa "
    "menagih berbeda — angka cost_usd di ledger adalah perkiraan, "
    "bukan tagihan."
)


def _clean_price_entry(model, entry):
    """A validated {"input_per_1m": float, "output_per_1m": float}
    for one model, or None when the entry is unusable."""
    if not isinstance(entry, dict):
        return None
    try:
        price_in = float(entry["input_per_1m"])
        price_out = float(entry["output_per_1m"])
    except (KeyError, TypeError, ValueError):
        return None
    if price_in < 0 or price_out < 0:
        return None
    return {"input_per_1m": price_in, "output_per_1m": price_out}


def merge_prices(*tables):
    """Merge price tables left to right; later tables win per model.

    A model entry that fails validation is skipped (the earlier
    value, if any, survives) — a bad row never wipes the table.
    """
    merged = {}
    for table in tables:
        if not isinstance(table, dict):
            continue
        for model, entry in table.items():
            cleaned = _clean_price_entry(model, entry)
            if cleaned is not None:
                merged[str(model)] = cleaned
    return merged


def load_prices_file(path):
    """The "prices" object from a JSON settings file, or {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    prices = data.get("prices")
    if not isinstance(prices, dict):
        return {}
    return {str(k): v for k, v in prices.items()}


def load_prices_env():
    """Prices from GROKBOT_COST_PRICES_JSON (a JSON object), or {}.

    Env wins over files: it is the quickest way for the owner to patch
    one price without touching a file.
    """
    raw = os.environ.get("GROKBOT_COST_PRICES_JSON") or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()}


def lookup_price(model, prices=None):
    """The price entry for a model, or None when unknown.

    Exact match first; then the longest known model name that is
    a prefix of the requested name followed by "-" — so dated
    variants such as "gpt-4o-mini-2024-07-18" price like their
    base model "gpt-4o-mini". Anything else is unknown: the
    ledger records the tokens and flags the price, it never
    guesses.
    """
    if not isinstance(model, str) or not model:
        return None
    table = prices if isinstance(prices, dict) else DEFAULT_PRICES
    if model in table:
        return _clean_price_entry(model, table[model])
    best = None
    for known in table:
        if model.startswith(known + "-"):
            if best is None or len(known) > len(best):
                best = known
    if best is None:
        return None
    return _clean_price_entry(best, table[best])


def estimate_cost(model, prompt_tokens, completion_tokens, prices=None):
    """(cost_usd, price_known) for one provider usage report.

    Token counts must be non-negative ints (bools rejected).
    Unknown model -> (0.0, False): tokens are still recorded by
    the caller, the cost is honestly zero, and the record carries
    the flag so the deck can show "harga tak diketahui".
    """
    for name, value in (("prompt_tokens", prompt_tokens),
                        ("completion_tokens", completion_tokens)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be an integer" % name)
        if value < 0:
            raise ValueError("%s must not be negative" % name)
    entry = lookup_price(model, prices)
    if entry is None:
        return 0.0, False
    cost = (prompt_tokens * entry["input_per_1m"]
            + completion_tokens * entry["output_per_1m"]) / 1_000_000
    return round(cost, 8), True
