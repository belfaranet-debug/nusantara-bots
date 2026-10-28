"""Cost ledger / spend cap per bot (FinOps-lite) — Phase 2/3.

Server-side only. The server NEVER sees a credential: the
browser reads the ``usage`` object from the provider's own
response (the call the browser already made with its BYO key,
Bearer ONLY to the provider URL) and POSTs just the numbers +
the model name to ``/api/costs/record``.

Pieces:
* ``prices`` — configurable price table (USD per 1M tokens) +
  cost estimation. Every number is an ESTIMATE (see
  ``prices.ESTIMATE_NOTE``); an unknown model costs 0 and is
  flagged ``price_known: false`` instead of being guessed.
* ``config`` — daily caps: default UNLIMITED until the owner sets one,
  from env (``GROKBOT_DAILY_CAP_USD`` global,
  ``GROKBOT_DAILY_CAP_USD_<BOT>`` per bot) or the settings files
  (``costs/defaults.json`` shipped, ``data/cost_settings.json``
  runtime — set via ``CostConfig.set_cap``).
* ``ledger`` — ``CostLedger``: per-task records, daily aggregates
  per bot, and the cap gate (``check_cap``) the task-start path
  consults BEFORE starting a task.

No keys of any kind live in this package. No billing, no
invoicing, no external service: this is an internal ledger only.
"""

from .config import CostConfig
from .ledger import CAP_REACHED_MESSAGE, CostLedger
from .prices import (DEFAULT_PRICES, ESTIMATE_NOTE, estimate_cost,
                     lookup_price, merge_prices)

__all__ = [
    "CAP_REACHED_MESSAGE",
    "DEFAULT_PRICES",
    "ESTIMATE_NOTE",
    "CostConfig",
    "CostLedger",
    "estimate_cost",
    "lookup_price",
    "merge_prices",
]
