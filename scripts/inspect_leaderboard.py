"""
Inspect Polymarket leaderboards by scraping polymarket.com/leaderboard/overall/{window}/profit.
The /v1/leaderboard API ignores window params — page scraping is the only way to get windowed data.

Usage:
    python scripts/inspect_leaderboard.py [--positions]
"""

import sys
import re
import json
import requests

PM_WEB  = "https://polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html",
}
SHOW_POSITIONS = "--positions" in sys.argv


def scrape_window(window: str, n: int = 20) -> tuple:
    """
    Returns (by_profit, by_volume) each a list of dicts for the given window.
    window: 'daily' | 'weekly' | 'all'
    """
    url = f"{PM_WEB}/leaderboard/overall/{window}/profit"
    r = requests.get(url, headers=HEADERS, timeout=15)
    print(f"  [{r.status_code}] {url}")
    if r.status_code != 200:
        return [], []
    arrays = re.findall(
        r'\[(\{"rank"[^\[\]]{20,}"proxyWallet"[^\[\]]*(?:\{[^\{\}]*\}[^\[\]]*)*)\]',
        r.text,
    )
    if len(arrays) < 2:
        print(f"  WARNING: only {len(arrays)} array(s) found in page")
        return [], []
    by_vol    = json.loads("[" + arrays[0] + "]")
    by_profit = json.loads("[" + arrays[1] + "]")
    return by_profit[:n], by_vol[:n]


def print_table(title: str, rows: list) -> None:
    print(f"\n  {title}")
    print(f"  {'#':<5} {'name':<26} {'pnl':>14}  {'volume':>14}")
    print("  " + "-" * 65)
    for e in rows:
        name = e.get("name") or e.get("pseudonym") or (e.get("proxyWallet", "")[:20] + "...")
        pnl  = float(e.get("pnl") or 0)
        vol  = float(e.get("volume") or 0)
        rank = e.get("rank", "?")
        print(f"  {str(rank):<5} {name:<26} ${pnl:>13,.0f}  ${vol:>13,.0f}")


# ─── Fetch all three windows ──────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  LEADERBOARD — polymarket.com page scrape (real windowed PnL)")
print("=" * 70)

daily_profit,   daily_vol   = scrape_window("daily",   20)
weekly_profit,  weekly_vol  = scrape_window("weekly",  20)
monthly_profit, monthly_vol = scrape_window("monthly", 20)
alltime_profit, alltime_vol = scrape_window("all",     20)

print_table("TODAY — top 20 by 24h profit:",    daily_profit)
print_table("WEEKLY — top 20 by 7d profit:",    weekly_profit)
print_table("MONTHLY — top 20 by 30d profit:",  monthly_profit)
print_table("ALL-TIME — top 20 by total profit:", alltime_profit)

# ─── Combined unique wallet set ───────────────────────────────────────────────
seen  = set()
combined = []
for lst in (alltime_profit, monthly_profit, weekly_profit, daily_profit):
    for e in lst:
        addr = e.get("proxyWallet", "")
        if addr and addr not in seen:
            seen.add(addr)
            combined.append(e)

print(f"\n  Total unique wallets to watch: {len(combined)}")

# ─── Open positions (optional) ────────────────────────────────────────────────
if SHOW_POSITIONS:
    print("\n" + "=" * 70)
    print("  OPEN POSITIONS PER WALLET")
    print("=" * 70)
    for e in combined:
        addr  = e.get("proxyWallet", "")
        label = e.get("name") or e.get("pseudonym") or addr[:18] + "..."
        r = requests.get(
            DATA_API + "/positions",
            params={"user": addr, "limit": 100, "sizeThreshold": "0.01"},
            headers={"User-Agent": "PolymarketTradingBot/1.0"},
            timeout=10,
        )
        data = r.json() if r.status_code == 200 else []
        if not isinstance(data, list):
            continue
        open_pos = [p for p in data
                    if float(p.get("size", 0) or 0) > 0.01 and not p.get("redeemable")]
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
