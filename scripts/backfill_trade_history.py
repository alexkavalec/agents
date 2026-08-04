"""
One-time recovery utility for when trade_history.json got wiped (e.g. a
Railway redeploy before DATA_DIR/a Volume was set up — see CLAUDE.md's
"Persistent state" notes) but the wallet still has real positions on
Polymarket. Reconstructs trade_history.json entries from what Polymarket's
public API can still show for the wallet, so the dashboard's Record/Trades
Filled/P&L stats aren't stuck at zero next to real open positions.

This is a BEST-EFFORT, PARTIAL recovery, not a full one:
  - Currently open positions recover cleanly (title, side, entry price, $ in).
  - Already-resolved LOSSES usually recover too — losing positions are
    worthless dust nobody bothers to formally redeem, so they linger
    indefinitely as `redeemable: true` entries.
  - Already-resolved WINS that were claimed do NOT recover — Polymarket's
    /positions API drops a position entirely once its payout is redeemed, so
    there's no trace left to reconstruct from. Any winning trade that
    resolved and got claimed before this ran is permanently gone from the
    record. (Same bias documented in CLAUDE.md's Task #33.)
  - Reconstructed entries can't know the bot's original bot_estimate/edge at
    trade time — those fields are set to the entry price as a placeholder.
    Every backfilled entry is tagged "backfilled": true so it's distinguishable
    from real-time logged trades if that ever matters.

Safe to re-run: entries are matched by token_id, so already-recovered or
already-logged positions are skipped, not duplicated.

Usage:
  python scripts/backfill_trade_history.py            # writes trade_history.json
  python scripts/backfill_trade_history.py --dry-run   # preview only, no write
  python scripts/backfill_trade_history.py <address>   # override the wallet address
"""
import sys
import os
import json
import datetime

import requests

DATA_API = "https://data-api.polymarket.com"
DATA_DIR = os.environ.get("DATA_DIR", ".")
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.json")
RESOLUTION_THRESHOLD = 0.90  # same threshold scoreboard.py uses


def _load_history() -> list:
    try:
        with open(TRADE_HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv[1:]
    address = args[0] if args else (os.getenv("POLYGON_FUNDER_ADDRESS") or os.getenv("POLYGON_ADDRESS"))

    if not address:
        print("No wallet address given and POLYGON_FUNDER_ADDRESS/POLYGON_ADDRESS aren't set.")
        print("Usage: python scripts/backfill_trade_history.py <address> [--dry-run]")
        sys.exit(1)

    print(f"Fetching positions for {address} ...")
    resp = requests.get(
        f"{DATA_API}/positions",
        params={"user": address, "limit": 500, "sizeThreshold": "0"},
        headers={"User-Agent": "PolymarketTradingBot/1.0"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code} fetching positions — aborting.")
        sys.exit(1)
    positions = resp.json()
    if not isinstance(positions, list):
        print("Unexpected response shape from /positions — aborting.")
        sys.exit(1)

    history = _load_history()
    known_tokens = {str(t.get("token_id", "")) for t in history}

    now = datetime.datetime.utcnow()
    added = []
    skipped_known = 0
    skipped_ambiguous = 0

    for pos in positions:
        token_id = str(pos.get("asset", ""))
        if not token_id or token_id in known_tokens:
            skipped_known += 1
            continue

        avg_price = float(pos.get("avgPrice", 0) or 0)
        if avg_price <= 0:
            continue
        size = float(pos.get("size", 0) or 0)
        amount_usd = float(pos.get("initialValue", 0) or (avg_price * size))
        if amount_usd <= 0:
            continue

        redeemable = pos.get("redeemable", False)
        # See scoreboard.py's resolve_completed() for why this isn't `x or -1` —
        # a resolved LOSS's curPrice is legitimately 0, which `or` would treat as
        # falsy and wrongly replace with the -1 sentinel.
        raw_cur = pos.get("curPrice")
        if raw_cur is None:
            raw_cur = pos.get("currentValue")
        cur_price = float(raw_cur) if raw_cur is not None else -1.0

        outcome = "pending"
        exit_price = None
        pnl_usd = None
        if redeemable:
            if cur_price >= RESOLUTION_THRESHOLD:
                outcome = "win"
            elif cur_price <= (1 - RESOLUTION_THRESHOLD):
                outcome = "loss"
            else:
                skipped_ambiguous += 1
                continue  # redeemable but price ambiguous — same rule scoreboard.py uses
            exit_price = round(cur_price, 4)
            pnl_usd = round(amount_usd * (1.0 / avg_price - 1), 2) if outcome == "win" else round(-amount_usd, 2)

        entry = {
            "timestamp": now.isoformat(),
            "date": now.date().isoformat(),
            "question": pos.get("title", token_id[:30]),
            "market_id": str(pos.get("conditionId", "")),
            "token_id": token_id,
            "side": pos.get("outcome", "") or pos.get("side", ""),
            "bot_estimate": round(avg_price, 4),   # placeholder — real value at trade time is lost
            "market_price": round(avg_price, 4),   # placeholder — same reason
            "edge": 0.0,                            # unknown, not recoverable
            "amount_usd": round(amount_usd, 2),
            "status": "filled",
            "outcome": outcome,
            "exit_price": exit_price,
            "pnl_usd": pnl_usd,
            "backfilled": True,
        }
        history.append(entry)
        added.append(entry)
        known_tokens.add(token_id)

    print(f"\n{len(added)} position(s) recoverable and not already in trade_history.json:")
    for e in added:
        tag = f"[{e['outcome'].upper()}]" if e["outcome"] != "pending" else "[OPEN]"
        print(f"  {tag:8s} ${e['amount_usd']:>10,.2f}  \"{e['question'][:55]}\"  ({e['side']})")
    if skipped_known:
        print(f"\n{skipped_known} position(s) already in trade_history.json — left untouched.")
    if skipped_ambiguous:
        print(f"{skipped_ambiguous} redeemable position(s) had an ambiguous price — skipped, not guessed.")

    if not added:
        print("\nNothing to backfill.")
        return

    if dry_run:
        print(f"\n--dry-run set — not writing to {TRADE_HISTORY_FILE}.")
        return

    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    print(f"\nWrote {len(history)} total entries to {TRADE_HISTORY_FILE}.")
    print(
        "\nReminder: any already-resolved WIN that was already claimed before this ran is not "
        "recoverable — Polymarket's API drops redeemed positions entirely. This backfill can only "
        "restore what's still visible: open positions and unclaimed losing dust."
    )


if __name__ == "__main__":
    main()
