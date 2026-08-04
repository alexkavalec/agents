"""
Chat-driven overrides to today's mechanical trading behavior — proposed by chat.py
from a plain-English dashboard message, confirmed by the user in the UI, then read
by Trader.one_best_trade() every cycle. Nothing here picks markets or places trades;
it only adjusts three existing mechanical knobs for the rest of the day:

  focus_trader      — copy only this one whale today (bypasses the normal 2+ whale
                       consensus requirement for that trader alone)
  trade_count_cap   — stop trading once this many trades have filled today
  size_override_usd — flat $ size for today's trade(s) instead of 25% of balance

All overrides expire automatically at UTC midnight — the stored "date" is checked
against today's date on every load, so a stale file from a prior day is treated as
empty rather than silently carrying over.
"""

import os
import json
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", ".")
OVERRIDES_FILE = os.path.join(DATA_DIR, "bot_overrides.json")

_FIELDS = ("focus_trader", "trade_count_cap", "size_override_usd")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_overrides() -> dict:
    """Return today's active overrides. Anything from a prior UTC day is expired
    and reported as empty rather than applied."""
    defaults = {"date": _today(), "focus_trader": None, "trade_count_cap": None, "size_override_usd": None}
    try:
        with open(OVERRIDES_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return defaults
    if data.get("date") != _today():
        return defaults
    return {**defaults, **{k: data.get(k) for k in _FIELDS}}


def save_override(**fields) -> dict:
    """Merge the given (non-None) fields into today's overrides and persist.
    Automatically resets to today's defaults first if the stored state is from
    a prior day. Returns the resulting overrides dict."""
    current = load_overrides()
    for k, v in fields.items():
        if k in _FIELDS and v is not None:
            current[k] = v
    current["date"] = _today()
    try:
        with open(OVERRIDES_FILE, "w") as f:
            json.dump(current, f, indent=2)
    except Exception as e:
        print(f"  [Overrides] could not save: {e}")
    return current


def clear_overrides() -> dict:
    """Cancel all active overrides immediately (before end of day)."""
    empty = {"date": _today(), "focus_trader": None, "trade_count_cap": None, "size_override_usd": None}
    try:
        with open(OVERRIDES_FILE, "w") as f:
            json.dump(empty, f, indent=2)
    except Exception as e:
        print(f"  [Overrides] could not save: {e}")
    return empty
