import json
import datetime
import os

TRADE_HISTORY_FILE = "trade_history.json"
FORECASTER_LOG_FILE = "forecaster_log.json"


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
    history.append({
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "date": datetime.date.today().isoformat(),
        "question": question,
        "market_id": str(market_id),
        "token_id": str(token_id),
        "side": side,
        "bot_estimate": round(bot_estimate, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "amount_usd": round(amount_usd, 2),
        "status": status,
    })
    _save(TRADE_HISTORY_FILE, history)


def log_lesson(
    question: str,
    side: str,
    entry_price: float,
    pnl_pct: float,
    outcome: str,
    lesson: str,
) -> None:
    """Append one post-mortem lesson to forecaster_log.json.

    outcome values: 'closed_profit', 'closed_loss'
    """
    logs = _load(FORECASTER_LOG_FILE)
    logs.append({
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "date": datetime.date.today().isoformat(),
        "question": question,
        "side": side,
        "entry_price": round(entry_price, 4),
        "pnl_pct": round(pnl_pct, 4),
        "outcome": outcome,
        "lesson": lesson,
    })
    _save(FORECASTER_LOG_FILE, logs)


def get_recent_lessons(n: int = 5) -> list:
    """Return the n most recent lessons for injection into the superforecaster prompt."""
    logs = _load(FORECASTER_LOG_FILE)
    return logs[-n:] if logs else []


def get_stats() -> dict:
    """Return a quick summary of all logged trades."""
    history = _load(TRADE_HISTORY_FILE)
    filled = [t for t in history if t.get("status") == "filled"]
    lessons = _load(FORECASTER_LOG_FILE)
    profits = [l for l in lessons if l.get("outcome") == "closed_profit"]
    losses = [l for l in lessons if l.get("outcome") == "closed_loss"]
    return {
        "total_attempts": len(history),
        "filled": len(filled),
        "fok_killed": len([t for t in history if t.get("status") == "fok_killed"]),
        "closed_profit": len(profits),
        "closed_loss": len(losses),
    }
