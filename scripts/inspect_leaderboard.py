"""
Inspect top-10 traders from today, weekly, and all-time leaderboard windows.
Hits data-api.polymarket.com directly to verify what the API actually returns.

Usage:
    python scripts/inspect_leaderboard.py [--positions]

Options:
    --positions   Also fetch open positions for each wallet found
"""

import sys
import time
import requests
from collections import defaultdict

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
HEADERS   = {"User-Agent": "PolymarketTradingBot/1.0"}
SHOW_POSITIONS = "--positions" in sys.argv


def req(url, params=None):
    try:
        r = requests.get(url, params=params or {}, headers=HEADERS, timeout=15)
        print(f"  [{r.status_code}] {url}{'?' + '&'.join(f'{k}={v}' for k,v in (params or {}).items()) if params else ''}")
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  [ERR] {e}")
    return None


def parse_ts(val) -> float:
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fetch_leaderboard(n=10, window=None):
    params = {"limit": n, "sortBy": "pnl", "sortDir": "desc"}
    if window:
        params["window"] = window
    return req(DATA_API + "/v1/leaderboard", params) or []


def entries_to_rows(entries, n=10):
    rows = []
    for e in entries[:n]:
        addr  = e.get("proxyWallet") or e.get("address") or "?"
        name  = e.get("userName") or e.get("pseudonym") or e.get("name") or ""
        pnl   = float(e.get("pnl") or e.get("profit") or 0)
        rank  = e.get("rank") or "?"
        vol   = e.get("vol") or 0
        label = name or addr[:18] + "..."
        rows.append({"addr": addr, "name": label, "pnl": pnl, "rank": rank, "vol": vol})
    return rows


def print_table(title, rows):
    print(f"\n  {title}")
    print(f"  {'rank':<6}{'address':<22}{'name':<28}{'pnl':>14}{'vol':>10}")
    print("  " + "-" * 82)
    for i, r in enumerate(rows, 1):
        addr_short = r["addr"][:20]
        print(f"  {str(r['rank']):<6}{addr_short:<22}{r['name'][:26]:<28}${r['pnl']:>12,.0f}{r['vol']:>10}")


def top_from_trade_feed(n=10, since_ts=0, label=""):
    data = req(DATA_API + "/trades", {"limit": 10000, "takerOnly": "false"})
    if not isinstance(data, list):
        return []
    vol_by_wallet = defaultdict(float)
    name_by_wallet = {}
    for trade in data:
        ts = parse_ts(trade.get("timestamp") or trade.get("createdAt"))
        if ts < since_ts:
            continue
        wallet = trade.get("proxyWallet", "")
        if not wallet:
            continue
        try:
            dvol = float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0)
        except Exception:
            continue
        vol_by_wallet[wallet] += dvol
        if wallet not in name_by_wallet:
            name_by_wallet[wallet] = trade.get("pseudonym") or trade.get("name") or ""
    traders = sorted(
        [{"addr": a, "name": name_by_wallet.get(a, ""), "pnl": v, "rank": "-", "vol": 0}
         for a, v in vol_by_wallet.items() if v >= 500],
        key=lambda x: x["pnl"], reverse=True
    )
    return traders[:n]


# ─── API health check ─────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  API HEALTH CHECK")
print("=" * 70)
req(DATA_API  + "/v1/leaderboard", {"limit": 1})
req(GAMMA_API + "/markets",        {"limit": 1})
req(CLOB_API  + "/markets",        {"params": 1})

# ─── Leaderboard windows ──────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  LEADERBOARD — RAW WINDOW COMPARISON")
print("=" * 70)

alltime_raw = fetch_leaderboard(10, window=None)
today_raw   = fetch_leaderboard(10, window="1d")
weekly_raw  = fetch_leaderboard(10, window="1w")

alltime = entries_to_rows(alltime_raw)
today_lb = entries_to_rows(today_raw)
weekly_lb = entries_to_rows(weekly_raw)

top_all = alltime[0]["addr"]    if alltime    else ""
top_day = today_lb[0]["addr"]   if today_lb   else ""
top_wk  = weekly_lb[0]["addr"]  if weekly_lb  else ""

window_works = {
    "1d": bool(top_day and top_day != top_all),
    "1w": bool(top_wk  and top_wk  != top_all),
}
print(f"\n  window=1d respected: {window_works['1d']}")
print(f"  window=1w respected: {window_works['1w']}")

print_table("ALL-TIME top 10 (no window param):", alltime)
if window_works["1d"]:
    print_table("TODAY top 10 (window=1d):", today_lb)
else:
    print(f"\n  window=1d ignored — falling back to trade feed (last 24h)...")
    today_feed = top_from_trade_feed(10, since_ts=time.time() - 86_400, label="today")
    print_table("TODAY top 10 (from /trades last 24h, $500+ volume):", today_feed)

if window_works["1w"]:
    print_table("WEEKLY top 10 (window=1w):", weekly_lb)
else:
    print(f"\n  window=1w ignored — falling back to trade feed (last 7d)...")
    weekly_feed = top_from_trade_feed(10, since_ts=time.time() - 604_800, label="weekly")
    print_table("WEEKLY top 10 (from /trades last 7d, $500+ volume):", weekly_feed)

# ─── Combined unique wallet set ───────────────────────────────────────────────
seen = set()
combined = []
for lst in (alltime,
            today_lb  if window_works["1d"] else today_feed  if "today_feed"  in dir() else [],
            weekly_lb if window_works["1w"] else weekly_feed if "weekly_feed" in dir() else []):
    for t in lst:
        if t["addr"] not in seen:
            seen.add(t["addr"])
            combined.append(t)

print(f"\n  Total unique wallets to watch: {len(combined)}")

# ─── Open positions (optional) ────────────────────────────────────────────────
if SHOW_POSITIONS:
    print("\n" + "=" * 70)
    print("  OPEN POSITIONS PER WALLET")
    print("=" * 70)
    for t in combined:
        data = req(DATA_API + "/positions",
                   {"user": t["addr"], "limit": 100, "sizeThreshold": "0.01"})
        if not isinstance(data, list):
            continue
        open_pos = [p for p in data
                    if float(p.get("size", 0) or 0) > 0.01 and not p.get("redeemable")]
        label = t["name"] or t["addr"][:18] + "..."
        print(f"\n  {label}  ({len(open_pos)} open positions)")
        for p in sorted(open_pos,
                        key=lambda x: float(x.get("currentValue", 0) or 0),
                        reverse=True)[:5]:
            title   = (p.get("title") or "")[:55]
            outcome = p.get("outcome", "?")
            size    = float(p.get("size", 0) or 0)
            avg     = float(p.get("avgPrice", 0) or 0)
            cur     = float(p.get("curPrice", 0) or 0)
            val     = float(p.get("currentValue", 0) or size * cur)
            print(f"    {outcome:<5}  avg={avg:.3f} cur={cur:.3f}  ${val:>8.2f}  \"{title}\"")

print("\n" + "=" * 70 + "\n")
