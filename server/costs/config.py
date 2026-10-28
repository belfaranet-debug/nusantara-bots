"""Daily spend-cap configuration for the own build (FinOps-lite).

Default: NO CAP (unlimited) for every bot, until the owner sets one.
A cap is a daily USD limit on the ledger's *estimated* cost for
one bot (UTC day). When a bot's spend for today reaches its cap,
new tasks for that bot are refused with a clear message — the
cap is a gate, never a bill: nothing here charges anyone.

Where a cap comes from, strongest first:
  1. per-bot cap in the runtime file (``data/cost_settings.json``,
     env ``GROKBOT_COST_SETTINGS_FILE``) — set via the settings
     endpoint / ``CostConfig.set_cap``;
  2. per-bot cap in the shipped file (``costs/defaults.json``,
     env ``GROKBOT_COST_DEFAULTS_FILE``);
  3. per-bot env var ``GROKBOT_DAILY_CAP_USD_<BOT>`` — the bot id
     upper-cased, anything not [A-Z0-9] turned into "_"
     (e.g. bot ``yt-scriptwriter`` -> ``GROKBOT_DAILY_CAP_USD_YT_SCRIPTWRITER``);
  4. a global cap: runtime file ``global_cap_usd``, then shipped
     file ``global_cap_usd``, then env ``GROKBOT_DAILY_CAP_USD``.

A cap value must be a number > 0. Zero, negative, non-numeric
and missing values all mean "no cap" — a typo can never lock a
bot out by accident. (To freeze a bot on purpose, set a tiny
cap such as 0.01, not 0.)

Prices are resolved the same way (shipped file, runtime file
overlay, env ``GROKBOT_COST_PRICES_JSON`` last) — see prices.py.

Zero-dependency, Python standard library only.
"""

from __future__ import annotations

import json
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS_FILENAME = "defaults.json"
DEFAULT_COST_DEFAULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), DEFAULTS_FILENAME)
DEFAULTS_ENV_VAR = "GROKBOT_COST_DEFAULTS_FILE"
DEFAULT_RUNTIME_SETTINGS_FILE = os.path.join(
    _REPO_ROOT, "data", "cost_settings.json")
SETTINGS_ENV_VAR = "GROKBOT_COST_SETTINGS_FILE"

GLOBAL_CAP_ENV_VAR = "GROKBOT_DAILY_CAP_USD"
PER_BOT_CAP_ENV_PREFIX = "GROKBOT_DAILY_CAP_USD_"


def _coerce_cap(value):
    """A positive float cap, or None for anything invalid/absent.

    Accepts numbers and numeric strings. False/zero/negative/
    non-numeric -> None (unlimited). A bool is never a cap.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        cap = float(value)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return cap


def _env_name_for_bot(bot):
    """GROKBOT_DAILY_CAP_USD_<BOT> for a bot id."""
    safe = re.sub(r"[^A-Za-z0-9]", "_", str(bot)).upper()
    return PER_BOT_CAP_ENV_PREFIX + safe


def _read_settings_file(path):
    """A settings file as a dict, or {} when missing/corrupt.

    Keys starting with "_" are comments/notes and ignored.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not str(k).startswith("_")}


class CostConfig:
    """Cap + price-table configuration. Reads env/files, never
    writes except through ``set_cap`` / ``clear_cap`` (the human
    settings path)."""

    def __init__(self, settings_path=None, defaults_path=None):
        self.settings_path = settings_path or os.environ.get(
            SETTINGS_ENV_VAR, DEFAULT_RUNTIME_SETTINGS_FILE)
        self.defaults_path = defaults_path or os.environ.get(
            DEFAULTS_ENV_VAR, DEFAULT_COST_DEFAULTS_FILE)

    # -- caps -------------------------------------------------------
    def _file_caps(self):
        """(per-bot caps, global cap) merged from the two files.

        Runtime file wins per key; the shipped file fills the rest.
        """
        caps = {}
        global_cap = None
        for path in (self.defaults_path, self.settings_path):
            data = _read_settings_file(path)
            section = data.get("caps")
            if isinstance(section, dict):
                for bot, value in section.items():
                    cap = _coerce_cap(value)
                    if cap is not None:
                        caps[str(bot)] = cap
            file_global = _coerce_cap(data.get("global_cap_usd"))
            if file_global is not None:
                global_cap = file_global
        return caps, global_cap

    def caps(self):
        """Effective per-bot caps: {bot: cap_usd}.

        File caps only — env per-bot caps are resolved per bot in
        ``cap_for`` so a bot never inherits another bot's env var.
        """
        file_caps, _ = self._file_caps()
        return dict(file_caps)

    def global_cap(self):
        """The effective global cap (USD/day), or None (unlimited).

        Env GROKBOT_DAILY_CAP_USD wins over both files.
        """
        env_cap = _coerce_cap(os.environ.get(GLOBAL_CAP_ENV_VAR))
        if env_cap is not None:
            return env_cap
        _, file_global = self._file_caps()
        return file_global

    def cap_for(self, bot):
        """The daily USD cap for one bot, or None (unlimited).

        Per-bot beats global: file per-bot, then per-bot env var,
        then the global cap.
        """
        if not isinstance(bot, str) or not bot.strip():
            return None
        file_caps, _ = self._file_caps()
        if bot in file_caps:
            return file_caps[bot]
        env_cap = _coerce_cap(os.environ.get(_env_name_for_bot(bot)))
        if env_cap is not None:
            return env_cap
        return self.global_cap()

    def set_cap(self, bot, cap_usd):
        """Set (or clear, with cap_usd=None) one bot's daily cap
        in the runtime file. Returns the effective per-bot caps.

        Raises ValueError on an empty bot id or a cap that is not
        a positive number (use clear_cap to remove a cap).
        """
        if not isinstance(bot, str) or not bot.strip():
            raise ValueError("bot must be a non-empty string")
        if cap_usd is not None and _coerce_cap(cap_usd) is None:
            raise ValueError("cap_usd must be a positive number")
        runtime = _read_settings_file(self.settings_path)
        caps = runtime.get("caps")
        if not isinstance(caps, dict):
            caps = {}
        if cap_usd is None:
            caps.pop(bot, None)
        else:
            caps[bot] = _coerce_cap(cap_usd)
        runtime["caps"] = caps
        directory = os.path.dirname(os.path.abspath(self.settings_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(runtime, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.settings_path)
        return self.caps()

    def clear_cap(self, bot):
        """Remove one bot's cap (back to unlimited / global)."""
        return self.set_cap(bot, None)

    # -- prices -----------------------------------------------------
    def prices(self):
        """The effective price table (see prices.merge_prices).

        Shipped defaults, runtime file overlay, env JSON last.
        """
        from .prices import (DEFAULT_PRICES, load_prices_env,
                             load_prices_file, merge_prices)
        return merge_prices(
            DEFAULT_PRICES,
            load_prices_file(self.defaults_path),
            load_prices_file(self.settings_path),
            load_prices_env())
