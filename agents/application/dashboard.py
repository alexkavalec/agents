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
import re
import json
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents.polymarket.polymarket import Polymarket
from agents.memory.trade_log import get_stats, TRADE_HISTORY_FILE
from agents.memory.scoreboard import get_scoreboard_stats, get_pnl_timeseries
from agents.connectors.whale_tracker import WHALE_CACHE_FILE, WhaleTracker
from agents.application import chat, overrides, trade

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

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
                "avg_price": p.get("avgPrice"),
                "cur_price": p.get("curPrice"),
                "amount_traded": p.get("initialValue"),
                "current_value": p.get("currentValue"),
                # Net profit if this position resolves in our favor: each share pays out
                # $1, so payout is size * $1 — minus what we put in (initialValue).
                "to_win": (float(p["size"]) - float(p["initialValue"]))
                          if p.get("size") is not None and p.get("initialValue") is not None
                          else None,
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
        "active_overrides": overrides.load_overrides(),
    }


def _validate_proposal(data: dict):
    """Re-validate a chat-proposed override before writing it to disk on confirm —
    the frontend round-trips the proposal it was shown, but the server doesn't
    trust it blindly. Returns (clean_fields, error_message_or_None)."""
    if not isinstance(data, dict):
        return {}, "invalid payload"
    fields = {}

    ft = data.get("focus_trader")
    if ft is not None:
        if not isinstance(ft, dict) or not _ADDRESS_RE.match(str(ft.get("address", ""))):
            return {}, "invalid focus_trader"
        fields["focus_trader"] = {
            "address": ft["address"],
            "name": str(ft.get("name") or ft["address"][:10] + "...")[:60],
        }

    cap = data.get("trade_count_cap")
    if cap is not None:
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            return {}, "invalid trade_count_cap"
        if not (0 < cap <= 1000):
            return {}, "trade_count_cap out of range"
        fields["trade_count_cap"] = cap

    size = data.get("size_override_usd")
    if size is not None:
        try:
            size = float(size)
        except (TypeError, ValueError):
            return {}, "invalid size_override_usd"
        if not (0 < size <= 1_000_000):
            return {}, "size_override_usd out of range"
        fields["size_override_usd"] = round(size, 2)

    if not fields:
        return {}, "no valid override fields in payload"
    return fields, None


def _validate_manual_trade(data: dict):
    """Re-validate a chat-proposed manual trade before executing it — same
    round-trip-don't-trust principle as _validate_proposal above, just with
    the added weight that confirming this one places a real order immediately."""
    mt = data.get("manual_trade")
    if not isinstance(mt, dict):
        return {}, "invalid payload"
    token_id = str(mt.get("token_id", "")).strip()
    if not token_id:
        return {}, "missing token_id"
    try:
        amount = float(mt.get("amount_usd"))
    except (TypeError, ValueError):
        return {}, "invalid amount"
    if not (0 < amount <= 100_000):
        return {}, "amount out of range"
    market_title = str(mt.get("market_title", "")).strip()[:300]
    side = str(mt.get("side", "")).strip()[:100]
    if not market_title or not side:
        return {}, "missing market_title or side"
    return {"token_id": token_id, "amount_usd": round(amount, 2), "market_title": market_title, "side": side}, None


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
        elif path == "/api/trader-pnl":
            qs = parse_qs(urlparse(self.path).query)
            address = qs.get("address", [""])[0]
            window = qs.get("window", ["1W"])[0]
            if not _ADDRESS_RE.match(address):
                self._send(400, "application/json", json.dumps({"error": "invalid address"}).encode())
                return
            try:
                points = WhaleTracker().get_trader_pnl_history(address, window)
                self._send(200, "application/json", json.dumps(points).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        elif path == "/api/trader-positions":
            # Live per-trader refresh for the dashboard's open trader modal — a single
            # lightweight /positions call, not a full leaderboard rescan, so it's cheap
            # enough to poll every minute while the modal is open.
            qs = parse_qs(urlparse(self.path).query)
            address = qs.get("address", [""])[0]
            if not _ADDRESS_RE.match(address):
                self._send(400, "application/json", json.dumps({"error": "invalid address"}).encode())
                return
            try:
                positions = WhaleTracker().get_live_positions(address)
                self._send(200, "application/json", json.dumps(positions).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _INDEX_HTML)
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized():
            self._send(401, "text/plain; charset=utf-8", b"Unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
        except Exception:
            self._send(400, "application/json", json.dumps({"error": "invalid JSON body"}).encode())
            return

        if path == "/api/chat":
            message = str(data.get("message", ""))[:2000]
            history = data.get("history") if isinstance(data.get("history"), list) else []
            # Round-tripped from this same endpoint's previous response (see
            # chat.handle_message()'s docstring) — only used to resolve a follow-up
            # answer to a manual-trade disambiguation question without a fresh search,
            # and still goes through the same human-confirm step as any other proposal.
            pending_candidates = data.get("pending_candidates") if isinstance(data.get("pending_candidates"), list) else None
            try:
                # Reuse _build_stats() so the assistant sees exactly what the dashboard
                # itself shows — full leaderboards, every scanned whale's positions,
                # actual open positions (not just a count), and recent trades — instead
                # of the earlier hand-picked, much smaller subset.
                stats = _build_stats()
                whale_cache = _load_whale_cache()
                context = {
                    "balance": stats["balance"],
                    "open_positions": stats["open_positions"],
                    "scoreboard": stats["scoreboard"],
                    "active_overrides": stats["active_overrides"],
                    "whale_last_updated": stats["whale_last_updated"],
                    "whale_leaderboards": stats["whale_leaderboards"],
                    "whale_traders": stats["whale_traders"],
                    "whale_signals": whale_cache.get("signals", []),
                    "recent_trades": stats["recent_trades"][:15],
                }
                result = chat.handle_message(message, whale_cache.get("traders", []), context, history, pending_candidates)
                self._send(200, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        elif path == "/api/overrides/confirm":
            fields, err = _validate_proposal(data)
            if err:
                self._send(400, "application/json", json.dumps({"error": err}).encode())
                return
            self._send(200, "application/json", json.dumps(overrides.save_override(**fields)).encode())
        elif path == "/api/overrides/clear":
            self._send(200, "application/json", json.dumps(overrides.clear_overrides()).encode())
        elif path == "/api/manual-trade/confirm":
            fields, err = _validate_manual_trade(data)
            if err:
                self._send(400, "application/json", json.dumps({"error": err}).encode())
                return
            try:
                polymarket = Polymarket()
                result = trade.place_manual_trade(
                    polymarket, fields["market_title"], fields["side"], fields["token_id"], fields["amount_usd"]
                )
                self._send(200, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
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
