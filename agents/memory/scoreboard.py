"""
Scoreboard — tracks trade outcomes (win / loss / push) and prints a
running record at the start of each cycle.

Reads from trade_history.json (written by log_trade) and detects
resolved positions by checking the Polymarket positions API for
redeemable=True entries, then matching on token_id.

Win/Loss logic (binary markets):
  - Held YES, market resolved YES → curPrice ≈ 1.0 → WIN
  - Held YES, market resolved NO  → curPrice ≈ 0.0 → LOSS
  - Held NO,  market resolved NO  → curPrice ≈ 1.0 → WIN
  - Held NO,  market resolved YES → curPrice ≈ 0.0 → LOSS
  - curPrice between 0.05–0.95    → still open (skip)
"""

import json
import os
import datetime

from agents.memory.trade_log import TRADE_HISTORY_FILE  # single source of truth for the path

RESOLUTION_THRESHOLD = 0.90   # curPrice >= 0.90 = payout confirmed


def _load() -> list:
    try:
        with open(TRADE_HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list) -> None:
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  [Scoreboard] Could not save: {e}")


def resolve_completed(polymarket_client) -> int:
    """
    Check Polymarket for resolved positions and update trade_history.json
    with outcome = 'win' or 'loss'. Returns count of newly resolved trades.
    """
    history = _load()
    pending = [t for t in history if t.get("outcome", "pending") == "pending"
               and t.get("status") == "filled"]
    if not pending:
        return 0

    # Fetch all positions including redeemable (resolved) ones
    try:
        address = os.getenv("POLYGON_FUNDER_ADDRESS") or os.getenv("POLYGON_ADDRESS")
        if not address:
            return 0
        import httpx
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": address, "limit": 500, "sizeThreshold": "0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return 0
        all_positions = resp.json()
    except Exception as e:
        print(f"  [Scoreboard] Could not fetch positions: {e}")
        return 0

    # Build lookup: token_id → position
    pos_by_token = {str(p.get("asset", "")): p for p in all_positions}

    resolved_count = 0
    for trade in history:
        if trade.get("outcome", "pending") != "pending":
            continue
        if trade.get("status") != "filled":
            continue

        token_id = str(trade.get("token_id", ""))
        pos = pos_by_token.get(token_id)
        if not pos:
            continue

        redeemable = pos.get("redeemable", False)
        # A resolved LOSS commonly has curPrice == 0, a legitimate value — `x or -1`
        # would treat that 0 as falsy and wrongly substitute -1, corrupting the
        # recorded exit_price even though it happens not to flip win/loss below.
        raw_cur = pos.get("curPrice")
        if raw_cur is None:
            raw_cur = pos.get("currentValue")
        cur_price = float(raw_cur) if raw_cur is not None else -1.0

        # Only resolve when Polymarket has marked the position as redeemable (actual resolution).
        # Price-only checks cause false wins/losses on volatile open markets.
        if not redeemable:
            continue

        # Determine outcome from current price at resolution
        if cur_price >= RESOLUTION_THRESHOLD:
            outcome = "win"
        elif cur_price <= (1 - RESOLUTION_THRESHOLD):
            outcome = "loss"
        else:
            continue  # redeemable but price ambiguous — skip

        entry  = float(trade.get("market_price", trade.get("bot_estimate", 0.5)))
        amount = float(trade.get("amount_usd", 0))
        if outcome == "win":
            pnl = amount * (1.0 / entry - 1)   # profit = (payout - cost)
        else:
            pnl = -amount                        # total loss of stake

        trade["outcome"]    = outcome
        trade["exit_price"] = round(cur_price, 4)
        trade["pnl_usd"]    = round(pnl, 2)
        trade["resolved_at"] = datetime.datetime.utcnow().isoformat()
        resolved_count += 1

        emoji = "✓ WIN" if outcome == "win" else "✗ LOSS"
        print(f"  [Scoreboard] {emoji}: {trade['question'][:55]} "
              f"({trade['side']}) +${pnl:.2f}" if outcome == "win"
              else f"  [Scoreboard] {emoji}: {trade['question'][:55]} "
              f"({trade['side']}) -${amount:.2f}")

    if resolved_count:
        _save(history)

    return resolved_count


def get_scoreboard_stats() -> dict:
    """Return the win/loss record as structured data (used by get_scoreboard_line and the dashboard)."""
    history = _load()
    filled = [t for t in history if t.get("status") == "filled"]

    wins    = [t for t in filled if t.get("outcome") == "win"]
    losses  = [t for t in filled if t.get("outcome") == "loss"]
    pushes  = [t for t in filled if t.get("outcome") == "push"]
    pending = [t for t in filled if t.get("outcome", "pending") == "pending"]

    total_resolved = len(wins) + len(losses) + len(pushes)
    win_pct = (len(wins) / total_resolved * 100) if total_resolved > 0 else 0.0

    resolved  = [t for t in filled if t.get("outcome") in ("win", "loss", "push")]
    total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in resolved)
    # ROI on resolved trades only — money still in open/pending positions isn't
    # counted as staked yet, so it can't be counted as a return yet either.
    total_staked = sum(t.get("amount_usd", 0) or 0 for t in resolved)
    roi_pct = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0

    return {
        "wins": len(wins),
        "losses": len(losses),
        "pushes": len(pushes),
        "pending": len(pending),
        "win_pct": round(win_pct, 1),
        "total_pnl": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
        "roi_pct": round(roi_pct, 1),
    }


def get_pnl_timeseries() -> list:
    """Cumulative realized PnL over time, one point per resolved trade in
    chronological order — the data behind the dashboard's profit chart."""
    history = _load()
    resolved = [
        t for t in history
        if t.get("status") == "filled"
        and t.get("outcome") in ("win", "loss", "push")
        and t.get("resolved_at")
    ]
    resolved.sort(key=lambda t: t["resolved_at"])

    points = []
    cumulative = 0.0
    for t in resolved:
        cumulative += float(t.get("pnl_usd", 0) or 0)
        points.append({
            "date": t["resolved_at"],
            "cumulative_pnl": round(cumulative, 2),
            "question": t.get("question", ""),
        })
    return points


def get_scoreboard_line() -> str:
    """Return a one-line scoreboard string."""
    s = get_scoreboard_stats()
    pnl_str = f"+${s['total_pnl']:.2f}" if s['total_pnl'] >= 0 else f"-${abs(s['total_pnl']):.2f}"
    return (
        f"  │  Score   : {s['wins']}W - {s['losses']}L - {s['pushes']}P  "
        f"({s['win_pct']:.0f}% win rate)  P&L: {pnl_str}  [{s['pending']} pending]"
    )


def print_scoreboard() -> None:
    """Print a one-line scoreboard to the logs."""
    print(get_scoreboard_line())
