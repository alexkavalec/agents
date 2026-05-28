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

Position tracking: whale positions are persisted to WHALE_STATE_FILE between cycles
so fresh entries (new this cycle) can be distinguished from ongoing holds.

All endpoints are public — no API key required.
  data-api.polymarket.com/v1/leaderboard — ranked leaderboard (profit/volume)
  data-api.polymarket.com/trades         — recent global trade feed (fallback)
  data-api.polymarket.com/positions      — open positions for a given address
  data-api.polymarket.com/holders        — top holders for a given market (conditionId)
"""

import json
import requests
from collections import defaultdict
from datetime import datetime, timezone

DATA_API = "https://data-api.polymarket.com"
WHALE_STATE_FILE = "whale_positions_state.json"

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


def _parse_ts(val) -> float:
    """Parse a timestamp value (unix int/float or ISO string) to a float epoch."""
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


class WhaleTracker:

    # ------------------------------------------------------------------
    # Leaderboard helpers
    # ------------------------------------------------------------------

    def _fetch_leaderboard(self, n: int, window: str = None) -> list:
        """
        Try to fetch top n leaderboard entries for a given window param.
        Returns raw entry list (may be empty if unreachable).
        window values tried: '1d', '1w', None (= all-time).
        """
        param_sets = []
        if window:
            param_sets += [
                {"limit": n, "window": window, "sortBy": "pnl", "sortDir": "desc"},
                {"limit": n, "window": window},
            ]
        param_sets += [
            {"limit": n, "sortBy": "pnl", "sortDir": "desc"},
            {"limit": n},
        ]
        for params in param_sets:
            data = _req(f"{DATA_API}/v1/leaderboard", params)
            if isinstance(data, list) and data:
                return data[:n]
        return []

    def _entries_to_traders(self, entries: list, source: str) -> list:
        """Convert raw leaderboard entries to trader dicts, deduplicating by address."""
        seen: dict = {}
        for entry in entries:
            addr = (entry.get("proxyWallet") or entry.get("address") or entry.get("user") or "")
            if not addr:
                continue
            try:
                pnl = float(entry.get("pnl") or entry.get("profit") or entry.get("profitAndLoss") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            name = (entry.get("userName") or entry.get("pseudonym") or
                    entry.get("name") or entry.get("xUsername") or "")
            rank = int(entry.get("rank") or 0)
            if addr not in seen:
                seen[addr] = {"address": addr, "volume": pnl, "name": name,
                              "rank": rank, "source": source}
        return list(seen.values())

    def _top_traders_from_trade_feed(self, n: int, since_ts: float, label: str) -> list:
        """
        Derive top n traders by USD volume from the global trade feed since `since_ts`.
        Used when the leaderboard window param is ignored.
        """
        # Fetch a large batch — platform busy, 5000 trades may cover only a few hours
        data = _req(f"{DATA_API}/trades", {"limit": 10000, "takerOnly": "false"})
        if not isinstance(data, list) or not data:
            return []

        vol_by_wallet:  dict = defaultdict(float)
        name_by_wallet: dict = {}
        for trade in data:
            ts = _parse_ts(trade.get("timestamp") or trade.get("createdAt"))
            if ts < since_ts:
                continue
            wallet = trade.get("proxyWallet", "")
            if not wallet:
                continue
            try:
                dvol = float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            vol_by_wallet[wallet] += dvol
            if wallet not in name_by_wallet:
                name_by_wallet[wallet] = trade.get("pseudonym") or trade.get("name") or ""

        MIN_VOL = 500.0  # lower threshold for time-window feeds
        traders = [
            {"address": addr, "volume": vol, "name": name_by_wallet.get(addr, ""),
             "rank": 0, "source": label}
            for addr, vol in vol_by_wallet.items()
            if vol >= MIN_VOL
        ]
        traders.sort(key=lambda t: t["volume"], reverse=True)
        return traders[:n]

    def get_top_traders_all_windows(self, n: int = 10) -> tuple:
        """
        Fetch top n traders from the today, weekly, and all-time leaderboard windows.

        Strategy:
          1. Fetch all-time leaderboard (no window param) — always works.
          2. Try 'window=1d' and 'window=1w' leaderboard params.
             If the top address is the same as all-time, the API is ignoring the
             window param, so fall back to filtering the trade feed by timestamp.
          3. Return (today_list, weekly_list, alltime_list) each with up to n entries.
        """
        import time
        now = time.time()

        alltime_entries = self._fetch_leaderboard(n)
        today_entries   = self._fetch_leaderboard(n, window="1d")
        weekly_entries  = self._fetch_leaderboard(n, window="1w")

        alltime = self._entries_to_traders(alltime_entries, "alltime")

        # Detect if window params are being respected
        top_all = alltime[0]["address"] if alltime else ""
        top_day = (self._entries_to_traders(today_entries,  "today")[0]["address"]
                   if today_entries else "")
        top_wk  = (self._entries_to_traders(weekly_entries, "weekly")[0]["address"]
                   if weekly_entries else "")

        if top_day and top_day != top_all:
            today = self._entries_to_traders(today_entries, "today")
        else:
            # Window param ignored — derive from trade feed
            today = self._top_traders_from_trade_feed(n, now - 86_400, "today")

        if top_wk and top_wk != top_all:
            weekly = self._entries_to_traders(weekly_entries, "weekly")
        else:
            weekly = self._top_traders_from_trade_feed(n, now - 604_800, "weekly")

        return today, weekly, alltime

    def _format_leaderboard(self, today: list, weekly: list, alltime: list) -> list:
        """Return formatted leaderboard box as a list of lines (no print)."""
        def _rows(lst, n=10):
            rows = []
            for i, t in enumerate(lst[:n], 1):
                label = t["name"] or t["address"][:14] + "..."
                rows.append(f"  │  {i:2d}.  ${t['volume']:>12,.0f}  {label}  [{t['source']}]")
            return rows

        lines = ["  ┌─ WHALE LEADERBOARD ── today / weekly / all-time top 10"]
        lines.append("  │")
        lines.append("  │  TODAY (by 24h volume):")
        lines += _rows(today) or ["  │    (no data)"]
        lines.append("  │")
        lines.append("  │  WEEKLY (by 7d volume):")
        lines += _rows(weekly) or ["  │    (no data)"]
        lines.append("  │")
        lines.append("  │  ALL-TIME (by unrealized PnL):")
        lines += _rows(alltime) or ["  │    (no data)"]
        lines.append("  └─────────────────────────────────────────────────")
        return lines

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

        Returns (signals, log_text) so the caller can print everything as one batched
        call and avoid Railway log reordering.

        signals: list of dicts sorted by freshness, whale_count, volume
        log_text: pre-formatted string covering leaderboard + scan summary + signals box
        """
        log: list = []  # accumulate all lines; caller prints at once

        today, weekly, alltime = self.get_top_traders_all_windows(top_n)
        log.extend(self._format_leaderboard(today, weekly, alltime))

        # Combine all three windows, deduplicate by address
        seen_addrs: set = set()
        traders: list = []
        for lst in (alltime, weekly, today):  # alltime first so rank ordering is preserved
            for t in lst:
                if t["address"] not in seen_addrs:
                    seen_addrs.add(t["address"])
                    traders.append(t)

        if not traders:
            log.append("  [WhaleTracker] No traders found from any window.")
            return [], "\n".join(log)

        log.append(
            f"  [WhaleTracker] Scanning {len(traders)} unique wallets "
            f"({len(alltime)} all-time + {len(weekly)} weekly + {len(today)} today)"
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
                "whale_profit_total":  total_vol,  # backward compat alias
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

        # Build signals box into log lines
        if signals:
            fresh_n   = sum(1 for s in signals if s.get("is_fresh"))
            ongoing_n = len(signals) - fresh_n
            log.append(
                f"  ┌─ WHALE SIGNALS ── {len(signals)} consensus  "
                f"({fresh_n} new  /  {ongoing_n} ongoing)"
            )
            for s in signals[:5]:
                tag       = "[NEW]" if s.get("is_fresh") else "     "
                new_label = (f"  +{s['new_whale_count']} new" if s.get("new_whale_count") else "")
                log.append(
                    f"  │ {tag} {s['whale_count']}x  {s['side'].upper():<20s}  "
                    f"entry {s['avg_entry']:.3f} -> now {s['cur_price']:.3f}  "
                    f"({s['price_drift']:.0%} drift){new_label}  \"{s['title'][:35]}\""
                )
            if len(signals) > 5:
                log.append(f"  │  ... +{len(signals)-5} more")
            log.append(f"  └───────────────────────────────────────────────────────")

        return signals, "\n".join(log)

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
