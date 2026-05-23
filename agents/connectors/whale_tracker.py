"""
WhaleTracker — discovers active Polymarket traders dynamically from recent trade flow
and surfaces their positions as smart-money signals.

Two-stage approach:
  1. Pre-selection: /trades global feed → find most active recent traders → fetch their
     open positions → consensus signals used to boost whale-signalled markets in the
     candidate list and inject keyword-matched context into the superforecaster prompt.
  2. Post-selection: /holders?market=CONDITION_ID → direct top-holder snapshot for the
     specific market being analyzed, injected as a calibration block in the prompt.

All endpoints are public — no API key required.
  data-api.polymarket.com/trades    — recent global trade feed
  data-api.polymarket.com/positions — open positions for a given address
  data-api.polymarket.com/holders   — top holders for a given market (conditionId)
"""

import requests
from collections import defaultdict

DATA_API = "https://data-api.polymarket.com"

# Minimum dollar volume in recent trades to qualify as a whale
MIN_WHALE_VOLUME = 50.0

# Minimum current position value to count as meaningful smart-money signal
MIN_POSITION_VALUE = 10.0

# Price drift cap — skip if price already moved >40% from whale's avg entry
MAX_PRICE_DRIFT = 0.40

# Consensus threshold — require this many independent whales on the same side
MIN_WHALES_AGREE = 2


def _req(url: str, params: dict = None) -> object:
    try:
        r = requests.get(
            url,
            params=params or {},
            timeout=10,
            headers={"User-Agent": "PolymarketTradingBot/1.0"},
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _categorise(holder: dict, yes_list: list, no_list: list) -> None:
    """Sort a holder dict into YES or NO bucket based on outcomeIndex or outcome field."""
    try:
        amount = float(holder.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount < 1.0:
        return

    outcome_idx = holder.get("outcomeIndex")
    outcome_str = (holder.get("outcome") or "").upper()

    if outcome_idx == 0 or outcome_str in ("YES", "0"):
        yes_list.append({"proxyWallet": holder.get("proxyWallet", ""), "amount": amount})
    elif outcome_idx == 1 or outcome_str in ("NO", "1"):
        no_list.append({"proxyWallet": holder.get("proxyWallet", ""), "amount": amount})


class WhaleTracker:

    def get_top_traders_from_trades(self, n: int = 30) -> list:
        """
        Discover active traders dynamically from the recent global trade feed.
        No leaderboard endpoint needed — self-updating each cycle.
        Returns top N traders sorted by total recent dollar volume.
        """
        data = _req(f"{DATA_API}/trades", {"limit": 500, "takerOnly": "false"})
        if not isinstance(data, list) or not data:
            return []

        volume_by_wallet: dict = defaultdict(float)
        for trade in data:
            wallet = trade.get("proxyWallet", "")
            if not wallet:
                continue
            try:
                dollar_vol = float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            volume_by_wallet[wallet] += dollar_vol

        traders = [
            {"address": addr, "volume": vol}
            for addr, vol in volume_by_wallet.items()
            if vol >= MIN_WHALE_VOLUME
        ]
        traders.sort(key=lambda t: t["volume"], reverse=True)

        if traders:
            print(f"  [WhaleTracker] {len(traders)} active traders found in recent trade feed (using top {min(n, len(traders))})")
        return traders[:n]

    def get_positions(self, address: str) -> list:
        """Open positions for one trader — skip resolved markets."""
        data = _req(
            f"{DATA_API}/positions",
            {"user": address, "limit": 500, "sizeThreshold": "0.01"},
        )
        if not isinstance(data, list):
            return []
        return [
            p for p in data
            if float(p.get("size", 0) or 0) > 0.01
            and not p.get("redeemable", False)
        ]

    def get_whale_signals(self, top_n: int = 25) -> list:
        """
        Scan top recent traders' positions and return consensus signals:
        markets where ≥MIN_WHALES_AGREE independent active traders hold the same side
        AND the price hasn't drifted more than MAX_PRICE_DRIFT from their avg entry.

        Returns list of dicts sorted by (whale_count DESC, combined_volume DESC):
        {
          title, asset, side,
          avg_entry, cur_price, price_drift,
          whale_count, whale_volume_total, whale_profit_total (= volume, for compat)
        }
        """
        traders = self.get_top_traders_from_trades(top_n)
        if not traders:
            print("  [WhaleTracker] No active traders found in recent trade feed.")
            return []

        print(f"  [WhaleTracker] Scanning {len(traders)} top traders for open positions...")

        buckets: dict = defaultdict(lambda: {
            "title": "", "asset": "", "side": "",
            "entries": [], "cur_prices": [], "whale_volumes": [],
        })

        for trader in traders:
            try:
                positions = self.get_positions(trader["address"])
            except Exception:
                continue
            for pos in positions:
                asset     = pos.get("asset", "")
                side      = pos.get("outcome", "") or pos.get("side", "")
                title     = pos.get("title", asset[:30])
                avg_price = float(pos.get("avgPrice", 0) or 0)
                cur_price = float(pos.get("curPrice", pos.get("currentValue", 0)) or 0)

                if not asset or avg_price <= 0:
                    continue

                # Only count positions with meaningful dollar value
                size = float(pos.get("size", 0) or 0)
                cur_val = float(pos.get("currentValue", 0) or 0) or (cur_price * size)
                if cur_val < MIN_POSITION_VALUE:
                    continue

                key = (asset, side)
                buckets[key]["title"]         = title
                buckets[key]["asset"]         = asset
                buckets[key]["side"]          = side
                buckets[key]["entries"].append(avg_price)
                buckets[key]["cur_prices"].append(cur_price)
                buckets[key]["whale_volumes"].append(trader["volume"])

        signals = []
        for (asset, side), d in buckets.items():
            count = len(d["entries"])
            if count < MIN_WHALES_AGREE:
                continue

            avg_entry = sum(d["entries"]) / count
            avg_cur   = (sum(d["cur_prices"]) / len(d["cur_prices"])
                         if d["cur_prices"] else avg_entry)
            drift = abs(avg_cur - avg_entry) / avg_entry if avg_entry > 0 else 1.0
            if drift > MAX_PRICE_DRIFT:
                continue

            total_vol = int(sum(d["whale_volumes"]))
            signals.append({
                "title":               d["title"],
                "asset":               asset,
                "side":                side,
                "avg_entry":           round(avg_entry, 4),
                "cur_price":           round(avg_cur, 4),
                "price_drift":         round(drift, 4),
                "whale_count":         count,
                "whale_volume_total":  total_vol,
                "whale_profit_total":  total_vol,  # backward compat alias
            })

        signals.sort(key=lambda s: (s["whale_count"], s["whale_volume_total"]), reverse=True)
        print(f"  [WhaleTracker] {len(signals)} consensus signal(s) found.")
        return signals

    def get_market_holders(self, condition_id: str) -> str:
        """
        Fetch top holders for a specific market and return a formatted context block.
        Called AFTER market selection — gives direct, accurate smart-money context
        without needing a leaderboard or global scan.

        Uses /holders?market=CONDITION_ID (limit 50).
        Returns empty string if the endpoint is unavailable or data is thin.
        """
        if not condition_id:
            return ""

        data = _req(f"{DATA_API}/holders", {"market": condition_id, "limit": 50})
        if not data:
            return ""

        yes_holders = []
        no_holders  = []

        entries = data if isinstance(data, list) else []
        for item in entries:
            if isinstance(item, dict) and "holders" in item:
                # Nested shape: {token: "...", holders: [{proxyWallet, amount, outcomeIndex, ...}]}
                for h in item.get("holders", []):
                    _categorise(h, yes_holders, no_holders)
            elif isinstance(item, dict) and "proxyWallet" in item:
                # Flat shape: each item is a holder object
                _categorise(item, yes_holders, no_holders)

        if not yes_holders and not no_holders:
            return ""

        yes_total = sum(h["amount"] for h in yes_holders)
        no_total  = sum(h["amount"] for h in no_holders)
        grand     = yes_total + no_total or 1

        lines = ["\nCURRENT MARKET HOLDER SNAPSHOT (largest position-holders right now):"]
        if yes_holders:
            lines.append(
                f"  YES side: {len(yes_holders)} large holders, "
                f"{yes_total:,.0f} tokens held"
            )
        if no_holders:
            lines.append(
                f"  NO side:  {len(no_holders)} large holders, "
                f"{no_total:,.0f} tokens held"
            )

        dominant = "YES" if yes_total >= no_total else "NO"
        lines.append(
            f"  → Holder concentration leans {dominant} "
            f"({yes_total / grand * 100:.0f}% YES / {no_total / grand * 100:.0f}% NO by tokens)"
        )
        lines.append(
            "  Treat this as a secondary calibration signal — concentrated large holders"
            " often have an information edge on the outcome.\n"
        )
        return "\n".join(lines)

    def format_whale_context(self, question: str, signals: list) -> str:
        """
        Match pre-selection whale signals to the given market question by keyword overlap,
        and return a formatted block to inject into the superforecaster prompt.
        """
        if not signals:
            return ""

        q_words = set(w.lower() for w in question.split() if len(w) > 3)

        matched = []
        for s in signals:
            t_words = set(w.lower() for w in s["title"].split() if len(w) > 3)
            if len(q_words & t_words) >= 2:
                matched.append(s)

        if not matched:
            return ""

        lines = ["\nSMART-MONEY SIGNAL — top active traders currently hold:"]
        for s in matched:
            drift_note = (
                f"{s['price_drift']:.0%} drift from entry"
                if s["price_drift"] > 0.02
                else "very fresh — price barely moved"
            )
            lines.append(
                f"  • {s['whale_count']} active traders: {s['side'].upper()} "
                f"@ avg entry {s['avg_entry']:.3f}  (market now {s['cur_price']:.3f}, {drift_note})"
                f"  — combined recent volume: ${s['whale_volume_total']:,}"
            )
        lines += [
            "",
            "  NOTE: these traders have been recently active with significant volume.",
            "  Weight this as a moderate signal — active traders may have better research.",
            "  Cross-reference with the holder snapshot and other context below.\n",
        ]
        return "\n".join(lines)
