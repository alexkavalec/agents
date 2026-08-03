"""
Minimal read-only stats dashboard. Serves the bot's balance, open positions,
and trade history as a single HTML page + a small JSON API.

Deliberately stdlib-only (http.server) — no new dependencies for something
this small. Runs in a background thread alongside the trading loop; see
cli.py's run_loop, which starts it once before entering the loop.

If DASHBOARD_TOKEN is set, requests must include a matching ?key= query
param — this is your balance and trade history, don't leave it open on a
public URL without one.
"""

import os
import json
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents.polymarket.polymarket import Polymarket
from agents.memory.trade_log import get_stats, TRADE_HISTORY_FILE
from agents.memory.scoreboard import get_scoreboard_stats, get_pnl_timeseries
from agents.connectors.whale_tracker import WHALE_CACHE_FILE

DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()
_INDEX_HTML = (Path(__file__).parent / "dashboard_static" / "index.html").read_bytes()


def _load_trade_history() -> list:
    try:
        with open(TRADE_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _load_whale_cache() -> dict:
    try:
        with open(WHALE_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_updated": None, "leaderboards": {}, "traders": [], "signals": []}


def _build_stats() -> dict:
    polymarket = Polymarket()
    try:
        balance = float(polymarket.get_usdc_balance())
    except Exception as e:
        balance = None
        print(f"  [Dashboard] balance fetch failed: {e}")
    try:
        positions = polymarket.get_open_positions()
    except Exception as e:
        positions = []
        print(f"  [Dashboard] positions fetch failed: {e}")

    history = sorted(_load_trade_history(), key=lambda t: t.get("timestamp", ""), reverse=True)
    whale_cache = _load_whale_cache()

    return {
        "balance": balance,
        "open_positions": [
            {
                "title": p.get("title", ""),
                "outcome": p.get("outcome", ""),
                "size": p.get("size"),
                "avg_price": p.get("avgPrice"),
                "cur_price": p.get("curPrice"),
                "current_value": p.get("currentValue"),
            }
            for p in positions
        ],
        "scoreboard": get_scoreboard_stats(),
        "trade_stats": get_stats(),
        "recent_trades": history[:50],
        "pnl_history": get_pnl_timeseries(),
        "whale_last_updated": whale_cache.get("last_updated"),
        "whale_leaderboards": whale_cache.get("leaderboards", {}),
        "whale_traders": whale_cache.get("traders", []),
    }


class _Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        if not DASHBOARD_TOKEN:
            return True
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("key", [""])[0] == DASHBOARD_TOKEN

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized():
            self._send(401, "text/plain; charset=utf-8", b"Unauthorized")
            return
        if path == "/api/stats":
            try:
                body = json.dumps(_build_stats()).encode()
                self._send(200, "application/json", body)
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _INDEX_HTML)
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, format, *args) -> None:
        pass  # keep the trading loop's logs clean — Railway logs every request otherwise


def start_dashboard(port: int = None) -> None:
    port = port or int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    auth_note = "token-protected" if DASHBOARD_TOKEN else "WARNING: no DASHBOARD_TOKEN set — publicly readable"
    print(f"Dashboard listening on :{port} ({auth_note})", flush=True)
