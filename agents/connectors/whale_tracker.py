"""
WhaleTracker — the bot's sole trade-signal source. Scrapes the top Polymarket
leaderboard traders (today/weekly/monthly/all-time) and buckets their open
positions into consensus signals: a (market, side) pair is a signal once
MIN_WHALES_AGREE independent whales hold it within MAX_PRICE_DRIFT of their
average entry.

Position tracking: whale positions are persisted to WHALE_STATE_FILE between cycles
so fresh entries (new this cycle) can be distinguished from ongoing holds.

All endpoints are public — no API key required.
  polymarket.com/leaderboard/overall/{window}/profit — SSR leaderboard pages (scraped)
  data-api.polymarket.com/positions                  — open positions for a given address
"""

import json
import re
import requests
from collections import defaultdict
from datetime import datetime, timezone

DATA_API   = "https://data-api.polymarket.com"
PM_WEB     = "https://polymarket.com"
PM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html",
}
WHALE_STATE_FILE = "whale_positions_state.json"

# Minimum current position value to count as meaningful smart-money signal
MIN_POSITION_VALUE = 50.0

# Price drift cap — skip if price already moved >20% from whale's avg entry
# (tightened from 40% now that whale consensus is the bot's sole trade signal —
# only trade signals that are still close to the whales' actual entry price)
MAX_PRICE_DRIFT = 0.20

# Consensus threshold — require this many independent whales on the same side
MIN_WHALES_AGREE = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_whale_state() -> dict:
    try:
        with open(WHALE_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_updated": None, "whale_positions": {}}


def _save_whale_state(state: dict) -> None:
    try:
        with open(WHALE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


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

    # ------------------------------------------------------------------
    # Leaderboard helpers
    # ------------------------------------------------------------------

    def _scrape_leaderboard_page(self, window: str, n: int) -> list:
        """
        Scrape top-n traders from polymarket.com/leaderboard/overall/{window}/profit.
        The page embeds two JSON arrays: index 0 = by volume, index 1 = by profit.
        Returns trader dicts with address, volume (= PnL), name, rank, source.

        window: 'daily' | 'weekly' | 'all'
        """
        url = f"{PM_WEB}/leaderboard/overall/{window}/profit"
        try:
            r = requests.get(url, headers=PM_HEADERS, timeout=15)
            if r.status_code != 200:
                return []
            # Two JSON arrays embedded in SSR HTML; index 1 is sorted by profit
            arrays = re.findall(
                r'\[(\{"rank"[^\[\]]{20,}"proxyWallet"[^\[\]]*(?:\{[^\{\}]*\}[^\[\]]*)*)\]',
                r.text,
            )
            if len(arrays) < 2:
                return []
            entries = json.loads("[" + arrays[1] + "]")
        except Exception:
            return []

        traders = []
        seen: set = set()
        for e in entries[:n]:
            addr = e.get("proxyWallet") or ""
            if not addr or addr in seen:
                continue
            seen.add(addr)
            try:
                pnl = float(e.get("pnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            name = e.get("name") or e.get("pseudonym") or ""
            rank = int(e.get("rank") or 0)
            traders.append({"address": addr, "volume": pnl, "name": name,
                            "rank": rank, "source": window})
        return traders

    def get_top_traders_all_windows(self, n: int = 10) -> tuple:
        """
        Fetch top n traders from the today, weekly, monthly, and all-time windows
        by scraping polymarket.com/leaderboard/overall/{window}/profit (SSR page data).

        Polymarket exposes 3 windowed profit leaderboards:
          weekly  → 7d realized profit   (/overall/weekly/profit)
          monthly → 30d profit            (/overall/monthly/profit)
          all     → all-time profit       (/overall/all/profit)
        The 'today' slug is a separate snapshot of unrealized PnL (open positions),
        ranked differently from the profit windows above.

        Returns (today_list, weekly_list, monthly_list, alltime_list) each up to n entries.
        """
        today   = self._scrape_leaderboard_page("today",   n)
        weekly  = self._scrape_leaderboard_page("weekly",  n)
        monthly = self._scrape_leaderboard_page("monthly", n)
        alltime = self._scrape_leaderboard_page("all",     n)
        return today, weekly, monthly, alltime

    def _format_leaderboard(self, today: list, weekly: list, monthly: list, alltime: list) -> list:
        """Return compact leaderboard box (6 lines) to avoid Railway log-collector splitting."""
        def _top3(lst):
            parts = []
            for t in lst[:3]:
                name = t["name"] or t["address"][:10] + "..."
                v = t["volume"]
                val = (f"${v/1_000_000:.1f}M" if abs(v) >= 1_000_000
                       else f"${v/1_000:.0f}k" if abs(v) >= 1_000
                       else f"${v:.0f}")
                parts.append(f"{name} {val}")
            return "  |  ".join(parts) if parts else "(no data)"

        return [
            "  ┌─ WHALE LEADERBOARD ──────────────────────────────────────────────────────",
            f"  │  TODAY   (unrealized): {_top3(today)}",
            f"  │  WEEKLY  (7d profit):  {_top3(weekly)}",
            f"  │  MONTHLY (30d profit): {_top3(monthly)}",
            f"  │  ALL-TIME:             {_top3(alltime)}",
            "  └──────────────────────────────────────────────────────────────────────────",
        ]

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

    def get_whale_signals(self, top_n: int = 10) -> tuple:
        """
        Fetch top 10 traders from today, weekly, and all-time leaderboard windows,
        combine into a unique set, then scan their open positions for consensus signals.

        Returns (signals, leaderboard_text, signals_text) — two separate strings so
        the caller can print each with flush=True to avoid Railway log reordering.

        signals: list of dicts sorted by freshness, whale_count, volume
        leaderboard_text: leaderboard box only
        signals_text: scan summary + signals box
        """
        lb_log: list  = []  # leaderboard section — printed first, separately
        sig_log: list = []  # scan info + signals box — printed second, separately

        today, weekly, monthly, alltime = self.get_top_traders_all_windows(top_n)
        lb_log.extend(self._format_leaderboard(today, weekly, monthly, alltime))

        # Combine all four windows, deduplicate by address
        seen_addrs: set = set()
        traders: list = []
        for lst in (alltime, monthly, weekly, today):  # alltime first so rank ordering is preserved
            for t in lst:
                if t["address"] not in seen_addrs:
                    seen_addrs.add(t["address"])
                    traders.append(t)

        if not traders:
            sig_log.append("  [WhaleTracker] No traders found from any window.")
            return [], "\n".join(lb_log), "\n".join(sig_log)

        sig_log.append(
            f"  [WhaleTracker] Scanning {len(traders)} unique wallets "
            f"({len(alltime)} all-time + {len(monthly)} monthly + {len(weekly)} weekly + {len(today)} today)"
        )


        buckets: dict = defaultdict(lambda: {
            "title": "", "asset": "", "side": "",
            "entries": [], "cur_prices": [], "whale_volumes": [],
            "whale_addresses": [],  # track which whales are in each bucket
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
                buckets[key]["whale_addresses"].append(trader["address"])

        # Load previous state to detect new entries this cycle
        now = _now_iso()
        prev_state    = _load_whale_state()
        prev_pos      = prev_state.get("whale_positions", {})
        new_state_pos: dict = {}  # will replace prev_pos in the saved file

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
            pos_key   = f"{asset}_{side}"

            # Freshness: count which whales are new to this position this cycle
            new_whale_count = 0
            earliest_seen   = now
            for addr in d["whale_addresses"]:
                prev_first = prev_pos.get(addr, {}).get(pos_key, {}).get("first_seen")
                if prev_first is None:
                    new_whale_count += 1
                else:
                    if prev_first < earliest_seen:
                        earliest_seen = prev_first
                # Persist this whale's position with its original first_seen time
                if addr not in new_state_pos:
                    new_state_pos[addr] = {}
                new_state_pos[addr][pos_key] = {
                    "title":      d["title"],
                    "side":       side,
                    "first_seen": prev_first or now,
                }

            signals.append({
                "title":               d["title"],
                "asset":               asset,
                "side":                side,
                "avg_entry":           round(avg_entry, 4),
                "cur_price":           round(avg_cur, 4),
                "price_drift":         round(drift, 4),
                "whale_count":         count,
                "whale_volume_total":  total_vol,
                "new_whale_count":     new_whale_count,
                "is_fresh":            new_whale_count > 0,
                "first_seen":          earliest_seen,
            })

        _save_whale_state({"last_updated": now, "whale_positions": new_state_pos})

        # Sort: fresh first (any new whale entry), then by whale count and volume
        signals.sort(
            key=lambda s: (s["is_fresh"], s["whale_count"], s["whale_volume_total"]),
            reverse=True,
        )

        # Build signals box into sig_log lines
        if signals:
            fresh_n   = sum(1 for s in signals if s.get("is_fresh"))
            ongoing_n = len(signals) - fresh_n
            sig_log.append(
                f"  ┌─ WHALE SIGNALS ── {len(signals)} consensus  "
                f"({fresh_n} new  /  {ongoing_n} ongoing)"
            )
            for s in signals[:5]:
                tag       = "[NEW]" if s.get("is_fresh") else "     "
                new_label = (f"  +{s['new_whale_count']} new" if s.get("new_whale_count") else "")
                sig_log.append(
                    f"  │ {tag} {s['whale_count']}x  {s['side'].upper():<20s}  "
                    f"entry {s['avg_entry']:.3f} -> now {s['cur_price']:.3f}  "
                    f"({s['price_drift']:.0%} drift){new_label}  \"{s['title'][:35]}\""
                )
            if len(signals) > 5:
                sig_log.append(f"  │  ... +{len(signals)-5} more")
            sig_log.append(f"  └───────────────────────────────────────────────────────")

        return signals, "\n".join(lb_log), "\n".join(sig_log)
