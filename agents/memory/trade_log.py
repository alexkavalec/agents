import json
import os
import datetime

# DATA_DIR lets state survive Railway redeploys — set it to a mounted Volume path
# (e.g. /data) in the service's env vars. Defaults to the working directory, which
# on a container platform without a volume attached is wiped on every deploy.
DATA_DIR = os.environ.get("DATA_DIR", ".")
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.json")


def _load(filepath: str) -> list:
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save(filepath: str, data: list) -> None:
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"WARN: could not save {filepath}: {e}")


def log_trade(
    question: str,
    token_id: str,
    side: str,
    bot_estimate: float,
    market_price: float,
    edge: float,
    amount_usd: float,
    status: str,
    market_id: str = "",
) -> None:
    """Append one trade attempt to trade_history.json.

    status values: 'filled', 'fok_killed', 'error'
    """
    history = _load(TRADE_HISTORY_FILE)
    now = datetime.datetime.utcnow()
    history.append({
        "timestamp": now.isoformat(),
        "date": now.date().isoformat(),  # UTC, matching timestamp — used by count_filled_today()
        "question": question,
        "market_id": str(market_id),
        "token_id": str(token_id),
        "side": side,
        "bot_estimate": round(bot_estimate, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "amount_usd": round(amount_usd, 2),
        "status": status,
        "outcome": "pending" if status == "filled" else "n/a",
        "exit_price": None,
        "pnl_usd": None,
    })
    _save(TRADE_HISTORY_FILE, history)


def get_stats() -> dict:
    """Return a quick summary of all logged trades."""
    history = _load(TRADE_HISTORY_FILE)
    filled = [t for t in history if t.get("status") == "filled"]
    return {
        "total_attempts": len(history),
        "filled": len(filled),
        "fok_killed": len([t for t in history if t.get("status") == "fok_killed"]),
        "untradeable": len([t for t in history if t.get("status") == "untradeable"]),
    }


def count_filled_today() -> int:
    """How many trades have filled today (UTC) — backs the chat-driven
    trade_count_cap override (see overrides.py / trade.py)."""
    today = datetime.datetime.utcnow().date().isoformat()
    history = _load(TRADE_HISTORY_FILE)
    return sum(1 for t in history if t.get("status") == "filled" and t.get("date") == today)
