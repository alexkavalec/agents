"""
WhaleTracker — discovers top Polymarket traders and surfaces their positions as
smart-money signals.

Two-stage approach:
  1. Pre-selection: tries /v1/leaderboard (ranked by all-time PnL) first; if blocked,
     falls back to /trades global feed scanning for largest recent dollar volumes.
     Fetches open positions for top traders → consensus signals used to boost
     whale-signalled markets and inject context into the superforecaster prompt.
  2. Post-selection: /holders?market=CONDITION_ID → direct top-holder snapshot for the
     specific market being analyzed, injected as a calibration block in the prompt.

All endpoints are public — no API key required.
  data-api.polymarket.com/v1/leaderboard — ranked leaderboard (profit/volume)
  data-api.polymarket.com/trades         — recent global trade feed (fallback)
  data-api.polymarket.com/positions      — open positions for a given address
  data-api.polymarket.com/holders        — top holders for a given market (conditionId)
"""

import requests
from collections import defaultdict

DATA_API = "https://data-api.polymarket.com"

# Minimum all-time profit to qualify as a true whale from the leaderboard ($50k+)
MIN_LEADERBOARD_PROFIT = 200_000.0

# Minimum dollar volume in recent trades to qualify as a whale (fallback path)
MIN_WHALE_VOLUME = 1_000.0

# Minimum current position value to count as meaningful smart-money signal
MIN_POSITION_VALUE = 50.0

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
    """Sort a holder dict into YES or NO bucket based on outcomeIndex or outcome field.
    Stores dollar value (amount × price) so near-zero-price tokens don't skew counts."""
    try:
        amount = float(holder.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount < 1.0:
        return

    # Weight by current token price so cheap YES/NO tokens don't inflate counts
    try:
        price = float(holder.get("price", 0) or holder.get("currentPrice", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    # If price unavailable, use raw token count (degrades gracefully)
    dollar_value = amount * price if price > 0 else amount

    outcome_idx = holder.get("outcomeIndex")
    outcome_str = (holder.get("outcome") or "").upper()

    if outcome_idx == 0 or outcome_str in ("YES", "0"):
        yes_list.append({"proxyWallet": holder.get("proxyWallet", ""), "amount": dollar_value})
    elif outcome_idx == 1 or outcome_str in ("NO", "1"):
        no_list.append({"proxyWallet": holder.get("proxyWallet", ""), "amount": dollar_value})


class WhaleTracker:

    def _fetch_leaderboard_window(self, window: str, top_n: int = 10) -> list:
        """
        Fetch top_n traders for a given time window from the leaderboard.
        window: "today" | "weekly" | "monthly" | "all"
        Returns list of raw entry dicts, empty on failure.
        """
        for params in [
            {"limit": top_n, "window": window, "sortBy": "pnl", "sortDir": "desc"},
            {"limit": top_n, "window": window, "sortBy": "profitAndLoss", "sortDir": "desc"},
            {"limit": top_n, "window": window},
        ]:
            data = _req(f"{DATA_API}/v1/leaderboard", params)
            if isinstance(data, list) and data:
                return data
        return []

    def get_top_traders_from_leaderboard(self, n: int = 50) -> list:
        """
        Fetch top 10 traders from each of the four leaderboard windows
        (today / weekly / monthly / all-time), deduplicate by wallet address.
        No profit floor — every slot from every window is included.
        """
        windows = ["today", "weekly", "monthly", "all"]
        window_labels = {"today": "T", "weekly": "W", "monthly": "M", "all": "A"}
        seen: dict = {}       # addr → trader dict
        appearances: dict = {}  # addr → list of window labels

        for window in windows:
            entries = self._fetch_leaderboard_window(window, top_n=10)
            tag = window_labels[window]
            for entry in entries:
                addr = (
                    entry.get("proxyWallet")
                    or entry.get("address")
                    or entry.get("user")
                    or ""
                )
                if not addr:
                    continue
                try:
                    profit = float(
                        entry.get("pnl")
                        or entry.get("profit")
                        or entry.get("profitAndLoss")
                        or 0
                    )
                except (TypeError, ValueError):
                    profit = 0.0

                appearances.setdefault(addr, [])
                appearances[addr].append(tag)

                if addr not in seen or profit > seen[addr]["volume"]:
                    name = (
                        entry.get("userName")
                        or entry.get("pseudonym")
                        or entry.get("name")
                        or entry.get("xUsername")
                        or ""
                    )
                    seen[addr] = {"address": addr, "volume": profit, "name": name, "source": "leaderboard"}

        traders = list(seen.values())
        traders.sort(key=lambda t: t["volume"], reverse=True)

        total_slots = sum(len(v) for v in appearances.values())
        duplicates  = sum(len(v) - 1 for v in appearances.values() if len(v) > 1)
        unique      = len(traders)
        print(f"  ┌─ WHALE LEADERBOARD ── {unique} unique traders ({total_slots} slots across 4 windows, {duplicates} cross-window dupes)")
        print(f"  │  T=Today  W=Weekly  M=Monthly  A=All-time")
        print(f"  │")
        for t in traders:
            tags = "+".join(appearances.get(t["address"], []))
            label = t["name"] or t["address"][:14] + "..."
            multi = " ◆" if len(appearances.get(t["address"], [])) > 1 else ""
            print(f"  │  [{tags:7s}]  ${t['volume']:>12,.0f}  {label}{multi}")
        print(f"  └─────────────────────────────────────────────────")
        return traders

    def get_top_traders_from_trades(self, n: int = 30) -> list:
        """
        Fallback: discover active traders from the recent global trade feed.
        Scans 2000 recent trades and returns highest USD-volume traders.
        """
        # Fetch a larger window to capture infrequent large traders
        data = _req(f"{DATA_API}/trades", {"limit": 2000, "takerOnly": "false"})
        if not isinstance(data, list) or not data:
            return []

        volume_by_wallet: dict = defaultdict(float)
        name_by_wallet:   dict = {}
        for trade in data:
            wallet = trade.get("proxyWallet", "")
            if not wallet:
                continue
            try:
                dollar_vol = float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            volume_by_wallet[wallet] += dollar_vol
            if wallet not in name_by_wallet:
                name = trade.get("pseudonym") or trade.get("name") or ""
                name_by_wallet[wallet] = name

        traders = [
            {"address": addr, "volume": vol, "name": name_by_wallet.get(addr, ""), "source": "trades"}
            for addr, vol in volume_by_wallet.items()
            if vol >= MIN_WHALE_VOLUME
        ]
        traders.sort(key=lambda t: t["volume"], reverse=True)

        if traders:
            top = traders[:n]
            print(f"  [WhaleTracker] Trade feed: {len(traders)} active traders, top {len(top)} (≥${MIN_WHALE_VOLUME:,.0f}):")
            for t in top[:10]:
                label = t["name"] or t["address"][:12] + "..."
                print(f"    ${t['volume']:>9,.0f} vol — {label}")
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

    def get_whale_signals(self, top_n: int = 50) -> list:
        """
        Scan top traders' positions and return consensus signals:
        markets where ≥MIN_WHALES_AGREE independent whales hold the same side
        AND the price hasn't drifted more than MAX_PRICE_DRIFT from their avg entry.

        Discovery order:
          1. /v1/leaderboard — true top traders by all-time profit (the real whales)
          2. /trades fallback — recent high-volume traders if leaderboard is blocked

        Returns list of dicts sorted by (whale_count DESC, combined_volume DESC):
        {
          title, asset, side,
          avg_entry, cur_price, price_drift,
          whale_count, whale_volume_total, whale_profit_total (= volume, for compat)
        }
        """
        # Try leaderboard first (real whales), fall back to trades
        traders = self.get_top_traders_from_leaderboard(top_n)
        source = "leaderboard"
        if not traders:
            print("  [WhaleTracker] Leaderboard unavailable — falling back to trade feed scan.")
            traders = self.get_top_traders_from_trades(top_n)
            source = "trades"
        if not traders:
            print("  [WhaleTracker] No traders found from either source.")
            return []


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
                f"~${yes_total:,.0f} in position value"
            )
        if no_holders:
            lines.append(
                f"  NO side:  {len(no_holders)} large holders, "
                f"~${no_total:,.0f} in position value"
            )

        dominant = "YES" if yes_total >= no_total else "NO"
        lines.append(
            f"  → Holder concentration leans {dominant} "
            f"({yes_total / grand * 100:.0f}% YES / {no_total / grand * 100:.0f}% NO by $ value)"
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

        lines = ["\nSMART-MONEY SIGNAL — top leaderboard whales currently hold:"]
        for s in matched:
            drift_note = (
                f"{s['price_drift']:.0%} drift from entry"
                if s["price_drift"] > 0.02
                else "very fresh — price barely moved"
            )
            lines.append(
                f"  • {s['whale_count']} whale(s): {s['side'].upper()} "
                f"@ avg entry {s['avg_entry']:.3f}  (market now {s['cur_price']:.3f}, {drift_note})"
                f"  — combined profit/volume: ${s['whale_volume_total']:,}"
            )
        lines += [
            "",
            "  NOTE: these are top-ranked Polymarket traders by all-time profit.",
            "  Weight this as a STRONG signal — these traders have demonstrated edge.",
            "  Cross-reference with the holder snapshot and other context below.\n",
        ]
        return "\n".join(lines)
