"""
WhaleTracker — follows top Polymarket traders and surfaces their positions
as a smart-money signal for the superforecaster.

Strategy: top traders often have superior research, connections, or information
that the broader market hasn't yet priced in. By detecting their fresh positions
before the price fully adjusts, we can ride the same information edge.

All endpoints are public — no API key required.
  data-api.polymarket.com/rankings   — leaderboard by profit/ROI
  data-api.polymarket.com/positions  — open positions for a given address
"""

import requests
from collections import defaultdict

DATA_API = "https://data-api.polymarket.com"

# Leaderboard quality filters — only follow consistently profitable traders
MIN_WHALE_PROFIT = 5_000    # $5k+ realised profit (not just lucky)
MIN_WHALE_ROI    = 0.05     # 5%+ ROI — filters out high-volume breakeven traders
MIN_WHALE_TRADES = 20       # at least 20 trades — not a one-shot wonder

# Price drift cap — if the market already priced in the whale's information, skip
# 0.40 = price moved 40% from whale's avg entry; beyond this we're chasing
MAX_PRICE_DRIFT = 0.40

# Consensus threshold — require multiple independent whales on the same side
# One whale might be wrong; 2+ whales agreeing independently is real signal
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


class WhaleTracker:

    def get_top_traders(self, n: int = 30) -> list:
        """
        Fetch the top n traders from the Polymarket leaderboard.
        Filters by minimum profit, ROI, and trade count so we follow
        skilled traders, not one-time lucky bettors.
        """
        for window in ("1M", "all"):
            data = _req(f"{DATA_API}/rankings", {"window": window, "limit": n})
            if not isinstance(data, list) or not data:
                continue
            traders = []
            for t in data:
                profit  = float(t.get("profit", 0) or 0)
                roi     = float(t.get("roi", 0) or 0)
                trades  = int(t.get("numTrades", t.get("num_trades", 0)) or 0)
                address = (
                    t.get("proxyWalletAddress")
                    or t.get("address")
                    or t.get("user", "")
                )
                if not address:
                    continue
                if profit >= MIN_WHALE_PROFIT and roi >= MIN_WHALE_ROI and trades >= MIN_WHALE_TRADES:
                    traders.append({
                        "address": address,
                        "profit":  profit,
                        "roi":     roi,
                        "trades":  trades,
                    })
            if traders:
                return traders[:n]
        return []

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
        Scan top traders' positions and return consensus signals:
        markets where ≥MIN_WHALES_AGREE independent whales are on the same side
        AND the price hasn't drifted more than MAX_PRICE_DRIFT from their avg entry
        (so there's still edge left to capture).

        Returns a list of dicts, sorted by (whale_count DESC, combined_profit DESC):
        {
          title, asset, side,
          avg_entry,        # avg price the whales paid
          cur_price,        # current market price
          price_drift,      # fraction price moved from entry (0.10 = 10%)
          whale_count,      # how many top traders hold this
          whale_profit_total,  # combined realised PnL of all agreeing whales ($)
        }
        """
        traders = self.get_top_traders(top_n)
        if not traders:
            print("  [WhaleTracker] No qualified traders found on leaderboard.")
            return []

        print(f"  [WhaleTracker] Scanning {len(traders)} top traders...")

        # key = (asset_token_id, side) → aggregated data
        buckets: dict = defaultdict(lambda: {
            "title":         "",
            "asset":         "",
            "side":          "",
            "entries":       [],
            "cur_prices":    [],
            "whale_profits": [],
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

                key = (asset, side)
                buckets[key]["title"]   = title
                buckets[key]["asset"]   = asset
                buckets[key]["side"]    = side
                buckets[key]["entries"].append(avg_price)
                buckets[key]["cur_prices"].append(cur_price)
                buckets[key]["whale_profits"].append(trader["profit"])

        signals = []
        for (asset, side), d in buckets.items():
            count = len(d["entries"])
            if count < MIN_WHALES_AGREE:
                continue

            avg_entry = sum(d["entries"]) / count
            avg_cur   = (sum(d["cur_prices"]) / len(d["cur_prices"])
                         if d["cur_prices"] else avg_entry)

            # How much has the market already moved from the whales' average entry?
            drift = abs(avg_cur - avg_entry) / avg_entry if avg_entry > 0 else 1.0
            if drift > MAX_PRICE_DRIFT:
                # Information already priced in — not worth chasing
                continue

            signals.append({
                "title":               d["title"],
                "asset":               asset,
                "side":                side,
                "avg_entry":           round(avg_entry, 4),
                "cur_price":           round(avg_cur, 4),
                "price_drift":         round(drift, 4),
                "whale_count":         count,
                "whale_profit_total":  int(sum(d["whale_profits"])),
            })

        signals.sort(key=lambda s: (s["whale_count"], s["whale_profit_total"]), reverse=True)
        print(f"  [WhaleTracker] {len(signals)} consensus whale signal(s) found.")
        return signals

    def format_whale_context(self, question: str, signals: list) -> str:
        """
        Match whale signals to the given market question by keyword overlap,
        and return a formatted block to inject into the superforecaster prompt.
        """
        if not signals:
            return ""

        q_words = set(
            w.lower() for w in question.split() if len(w) > 3
        )

        matched = []
        for s in signals:
            t_words = set(w.lower() for w in s["title"].split() if len(w) > 3)
            if len(q_words & t_words) >= 2:
                matched.append(s)

        if not matched:
            return ""

        lines = [
            "\nSMART-MONEY SIGNAL — top Polymarket traders currently hold:"
        ]
        for s in matched:
            drift_note = (
                f"{s['price_drift']:.0%} drift from entry"
                if s["price_drift"] > 0.02
                else "very fresh — price barely moved"
            )
            lines.append(
                f"  • {s['whale_count']} elite traders: {s['side'].upper()} "
                f"@ avg entry {s['avg_entry']:.3f}  (market now {s['cur_price']:.3f}, {drift_note})"
                f"  — combined profit of these traders: ${s['whale_profit_total']:,}"
            )
        lines += [
            "",
            "  IMPORTANT: these traders have a strong track record and may have",
            "  superior research or early information. Weight this signal HEAVILY.",
            "  Only diverge from their direction if you have specific, concrete,",
            "  recent evidence that contradicts their position.\n",
        ]
        return "\n".join(lines)
