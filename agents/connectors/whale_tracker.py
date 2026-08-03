"""
WhaleTracker — the bot's sole trade-signal source. Every cycle, scrapes the top 10
traders on the Polymarket today/weekly/monthly/all-time profit leaderboards, notes
every position each one is currently holding, and buckets those positions into
consensus signals: a (market, side) pair is a signal once MIN_WHALES_AGREE
independent whales hold it. No other filtering is applied — this is a pure
observation of what the leaderboard is doing, not a risk-managed selection.

Position tracking: whale positions are persisted to WHALE_STATE_FILE between cycles
so fresh entries (new this cycle) can be distinguished from ongoing holds. Every scan
also writes WHALE_CACHE_FILE — leaderboards + each trader's current positions — so
the dashboard can display them without re-scraping on every page load.

All endpoints are public — no API key required.
  polymarket.com/leaderboard/overall/{window}/profit — SSR leaderboard pages (scraped)
  data-api.polymarket.com/positions                  — open positions for a given address
"""

import json
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
WHALE_CACHE_FILE = "whale_scan_cache.json"  # leaderboards + per-trader positions, for the dashboard

# Minimum current position value to count as meaningful smart-money signal
# (not a risk rule — pure data hygiene, filters out dust positions that would
# otherwise inflate the count of whales "agreeing" on a market)
MIN_POSITION_VALUE = 50.0

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


def _save_scan_cache(leaderboards: dict, traders: list, signals: list) -> None:
    """Persist the leaderboards + per-trader positions from this scan so the
    dashboard can read them instantly instead of re-scraping on every request —
    the dashboard refreshes every minute, far more often than this scan runs."""
    try:
        with open(WHALE_CACHE_FILE, "w") as f:
            json.dump({
                "last_updated": _now_iso(),
                "leaderboards": leaderboards,
                "traders": traders,
                "signals": signals,
            }, f, indent=2)
    except Exception as e:
        print(f"  [WhaleTracker] could not save scan cache: {e}")


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

        The page embeds a React Query "dehydrated state" cache inside a Next.js RSC
        flight chunk, as an escaped JSON string (literal `\"` two-char sequences, not
        real quote characters) — not a plain embedded array like the page used to
        serve. Each cached query in that array looks like:
          {"state":{"data":[...trader entries...]},"queryKey":["/leaderboard","profit",...]}
        Note "state" comes BEFORE "queryKey" in the object, so we locate the
        "profit"-sorted queryKey marker and search *backwards* for its own
        preceding "data" array (searching forward would find the next query
        object's data instead — e.g. the unrelated "biggestWins" widget).

        window: 'today' | 'weekly' | 'monthly' | 'all'
        """
        url = f"{PM_WEB}/leaderboard/overall/{window}/profit"
        try:
            r = requests.get(url, headers=PM_HEADERS, timeout=15)
            if r.status_code != 200:
                return []
            text = r.text
            key_marker = r'\"queryKey\":[\"/leaderboard\",\"profit\"'
            key_idx = text.find(key_marker)
            if key_idx == -1:
                return []
            data_marker = r'\"data\":['
            data_idx = text.rfind(data_marker, 0, key_idx)
            if data_idx == -1:
                return []
            arr_start = data_idx + len(data_marker) - 1
            depth = 0
            i = arr_start
            while i < len(text):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            raw = text[arr_start:i + 1].replace(r'\"', '"')
            entries = json.loads(raw)
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
        cache_leaderboards = {
            window: [
                {"name": t["name"] or t["address"][:10] + "...", "address": t["address"],
                 "profit": round(t["volume"], 2)}
                for t in lst
            ]
            for window, lst in (("today", today), ("weekly", weekly),
                                 ("monthly", monthly), ("all_time", alltime))
        }

        # Combine all four windows, deduplicate by address — track every window
        # a trader appears on AND their profit in each one (for the dashboard),
        # not just the first window found
        by_addr: dict = {}
        for window, lst in (("all_time", alltime), ("monthly", monthly),
                             ("weekly", weekly), ("today", today)):
            for t in lst:
                addr = t["address"]
                if addr not in by_addr:
                    by_addr[addr] = {**t, "windows": [window], "window_profit": {window: t["volume"]}}
                else:
                    by_addr[addr]["windows"].append(window)
                    by_addr[addr]["window_profit"][window] = t["volume"]
        traders: list = list(by_addr.values())

        if not traders:
            sig_log.append("  [WhaleTracker] No traders found from any window.")
            _save_scan_cache(cache_leaderboards, [], [])
            return [], "\n".join(lb_log), "\n".join(sig_log)

        sig_log.append(
            f"  [WhaleTracker] Scanning {len(traders)} unique wallets "
            f"({len(alltime)} all-time + {len(monthly)} monthly + {len(weekly)} weekly + {len(today)} today)"
        )


        buckets: dict = defaultdict(lambda: {
            "title": "", "asset": "", "side": "", "end_date": "",
            "entries": [], "cur_prices": [], "whale_volumes": [],
            "whale_addresses": [],  # track which whales are in each bucket
        })
        trader_records: list = []  # per-trader position lists, for the dashboard

        for trader in traders:
            try:
                positions = self.get_positions(trader["address"])
            except Exception:
                continue

            trader_positions = []
            for pos in positions:
                asset     = pos.get("asset", "")
                side      = pos.get("outcome", "") or pos.get("side", "")
                title     = pos.get("title", asset[:30])
                avg_price = float(pos.get("avgPrice", 0) or 0)
                cur_price = float(pos.get("curPrice", pos.get("currentValue", 0)) or 0)
                end_date  = pos.get("endDate", "") or ""

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
                buckets[key]["end_date"]      = end_date[:10]  # "YYYY-MM-DD", drop any time part
                buckets[key]["entries"].append(avg_price)
                buckets[key]["cur_prices"].append(cur_price)
                buckets[key]["whale_volumes"].append(trader["volume"])
                buckets[key]["whale_addresses"].append(trader["address"])

                # amount_traded (cost basis) and to_win (net profit if this resolves in
                # their favor: size * $1 payout - cost) — same fields the dashboard shows
                # for the bot's own Open Positions, now surfaced per whale position too
                amount_traded = float(pos.get("initialValue", 0) or 0)
                trader_positions.append({
                    "title": title, "side": side,
                    "avg_price": round(avg_price, 4), "cur_price": round(cur_price, 4),
                    "size": round(size, 2), "value": round(cur_val, 2),
                    "amount_traded": round(amount_traded, 2),
                    "to_win": round(size - amount_traded, 2),
                })

            if trader_positions:
                trader_records.append({
                    "name": trader["name"] or trader["address"][:10] + "...",
                    "address": trader["address"],
                    # profit per leaderboard window the trader appears on (today/weekly/
                    # monthly/all_time) — same source as the leaderboard columns, lets the
                    # dashboard show a trader's full record, not just one window
                    "window_profit": trader.get("window_profit", {}),
                    "positions": sorted(trader_positions, key=lambda p: p["value"], reverse=True),
                })

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
                # The market's own resolution date (Polymarket's "endDate", YYYY-MM-DD) —
                # distinct from first_seen/is_fresh, which are about when the BOT noticed
                # the position, not when the EVENT itself happens. "Trade today's events"
                # means this, not observation freshness.
                "end_date":            d.get("end_date", ""),
                "is_today_event":      d.get("end_date", "") == now[:10],
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

        _save_scan_cache(cache_leaderboards, trader_records, signals)

        return signals, "\n".join(lb_log), "\n".join(sig_log)
