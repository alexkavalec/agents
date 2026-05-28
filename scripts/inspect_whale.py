"""
Usage: python scripts/inspect_whale.py [address]
Default: bossoskil1 (0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a)
"""
import sys
import requests

DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "PolymarketTradingBot/1.0"}

def req(url, params=None):
    r = requests.get(url, params=params or {}, headers=HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"  HTTP {r.status_code} — {r.text[:100]}")
    return None

address = sys.argv[1] if len(sys.argv) > 1 else "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a"

print(f"\n{'='*65}")
print(f"  WHALE PROFILE: {address}")
print(f"{'='*65}")

# ── Leaderboard entry ──────────────────────────────────────────────
lb = req(f"{DATA_API}/v1/leaderboard", {"limit": 200})
entry = None
if isinstance(lb, list):
    for e in lb:
        if (e.get("proxyWallet") or e.get("address") or "").lower() == address.lower():
            entry = e
            break
if entry:
    print(f"\n  LEADERBOARD ENTRY (raw):")
    for k, v in entry.items():
        if k != "profileImage":
            print(f"    {k:20s} = {v}")
else:
    print(f"\n  Not found in top-200 leaderboard.")

# ── Open positions ─────────────────────────────────────────────────
positions = req(f"{DATA_API}/positions", {"user": address, "limit": 500, "sizeThreshold": "0"})
if isinstance(positions, list):
    open_pos = [p for p in positions if float(p.get("size", 0) or 0) > 0.01 and not p.get("redeemable")]
    closed   = [p for p in positions if p.get("redeemable")]
    print(f"\n  OPEN POSITIONS ({len(open_pos)}):")
    total_val = 0
    for p in sorted(open_pos, key=lambda x: float(x.get("size",0) or 0) * float(x.get("curPrice",0) or 0), reverse=True):
        title   = (p.get("title") or p.get("question") or "")[:55]
        outcome = (p.get("outcome") or p.get("side") or "?")[:8]
        size    = float(p.get("size", 0) or 0)
        avg     = float(p.get("avgPrice", 0) or 0)
        cur     = float(p.get("curPrice", 0) or 0)
        val     = size * cur
        pnl     = (cur - avg) * size
        total_val += val
        print(f"    {outcome:<8}  size={size:>8.0f}  avg={avg:.3f}→{cur:.3f}  val=${val:>8.2f}  pnl=${pnl:>+8.2f}  \"{title}\"")
    print(f"    {'─'*60}")
    print(f"    Total open position value: ${total_val:,.2f}")
    print(f"    Redeemable (resolved): {len(closed)}")

# ── Recent trades ──────────────────────────────────────────────────
trades = req(f"{DATA_API}/trades", {"maker": address, "limit": 20})
if not isinstance(trades, list) or not trades:
    trades = req(f"{DATA_API}/activity", {"user": address, "limit": 20})
if isinstance(trades, list) and trades:
    print(f"\n  RECENT TRADES ({len(trades)} shown):")
    for t in trades[:20]:
        side  = t.get("side") or t.get("outcome") or "?"
        price = float(t.get("price", 0) or 0)
        size  = float(t.get("size", 0) or t.get("amount", 0) or 0)
        title = (t.get("title") or t.get("market") or "")[:50]
        ts    = (t.get("timestamp") or t.get("createdAt") or "")[:16]
        print(f"    {ts}  {side:<5}  ${size*price:>8.2f}  @{price:.3f}  \"{title}\"")
else:
    print(f"\n  No recent trades found (endpoint may require auth).")

print(f"\n{'='*65}\n")
