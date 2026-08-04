from agents.polymarket.polymarket import Polymarket
from agents.memory.trade_log import log_trade, count_filled_today
from agents.application.overrides import load_overrides
import os
import json
import requests as _requests

# =========================================================================
# STRATEGY
#
# The bot trades ONLY on whale-leaderboard consensus (see WhaleTracker /
# get_whale_signals): every cycle it looks at the top 10 traders on the
# today / weekly / monthly / all-time profit leaderboards, and notes every
# position they're currently holding. A trade fires when MIN_WHALES_AGREE
# (in whale_tracker.py) independent whales hold the same side of the same
# market.
#
# There is no risk management beyond the rules below — no stop-loss,
# no take-profit, no daily loss/spend cap, no max open positions, no
# cooldown, no price-drift filter, no correlation filter. Positions are
# held forever once opened; the bot never sells.
#
# The dashboard's chat box (chat.py) can adjust three of these mechanical
# knobs for the rest of the current UTC day — see overrides.py — but it
# never picks markets or sizes trades on its own judgment; every override
# still flows through the same signal/eligibility pipeline below.
# =========================================================================
BET_FRACTION = 0.25   # the only sizing rule: 25% of current balance, every trade
ABSOLUTE_MIN_TRADE = 1.0   # Polymarket's own order minimum (~$1) — an exchange
                            # constraint, not a risk rule. Below this, skip.
STATE_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "trader_trade_history.json")
                                            # every (token_id, title, side) ever bought —
                                            # backs the two dedup rules below, as a
                                            # redundant check alongside the live positions API.
                                            # DATA_DIR should point at a mounted Railway Volume
                                            # so this survives redeploys — see CLAUDE.md.
HIGH_CONSENSUS_WHALES = 5  # bypass the "today's events only" filter (rule 6) when this
                            # many+ independent whales agree on the same (market, side) —
                            # strong enough to also catch whales positioned ahead of a
                            # market that resolves later than today
# =========================================================================


def _discord(msg: str) -> None:
    """Post a notification to Discord if DISCORD_WEBHOOK_URL is set."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        _requests.post(url, json={"content": msg}, timeout=5)
    except Exception:
        pass


def _opposite_side(side: str) -> str:
    s = (side or "").strip().lower()
    if s == "yes":
        return "no"
    if s == "no":
        return "yes"
    return ""  # unrecognised side label — can't determine the opposite safely


class Trader:
    def __init__(self):
        self.polymarket = Polymarket()

    def _load_trade_history(self) -> list:
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_trade_history(self, history: list) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            print(f"WARN: could not save trade history: {e}")

    def _record_trade(self, token_id: str, title: str, side: str) -> None:
        history = self._load_trade_history()
        history.append({"token_id": token_id, "title": title, "side": side})
        self._save_trade_history(history)

    def one_best_trade(self) -> None:
        try:
            try:
                balance = float(self.polymarket.get_usdc_balance())
            except Exception as e:
                print(f"Could not read balance ({e}); aborting this run for safety.")
                return

            from agents.memory.trade_log import get_stats
            from agents.memory.scoreboard import resolve_completed, get_scoreboard_line
            resolve_completed(self.polymarket)

            open_positions = []
            try:
                open_positions = self.polymarket.get_open_positions()
            except Exception as e:
                print(f"  Could not fetch open positions: {e}")

            # ── CYCLE HEADER — printed as one call so Railway log collector keeps it intact ──
            stats = get_stats()
            print("\n".join([
                "",
                "  ┌─ CYCLE SUMMARY ───────────────────────────────────────",
                f"  │  Balance : ${balance:.2f}",
                f"  │  Positions: {len(open_positions)} open",
                get_scoreboard_line(),
                f"  │  Trades  : {stats['total_attempts']} attempts | {stats['filled']} filled | "
                f"{stats['fok_killed']} FOK killed | {stats['untradeable']} untradeable",
                "  └───────────────────────────────────────────────────────",
                "",
            ]))

            # ── CHAT OVERRIDES — see overrides.py. All three expire at UTC midnight
            # and never bypass rules 4/5 (dedup, no opposite side); they only adjust
            # which signals are considered and how big the resulting trade is. ──
            overrides = load_overrides()
            override_notes = []
            if overrides.get("focus_trader"):
                override_notes.append(f"focus={overrides['focus_trader']['name']}")
            if overrides.get("trade_count_cap") is not None:
                override_notes.append(f"cap={overrides['trade_count_cap']}/day")
            if overrides.get("size_override_usd") is not None:
                override_notes.append(f"size=${overrides['size_override_usd']:.2f}")
            if override_notes:
                print(f"  ⚙ Active chat override(s): {', '.join(override_notes)}", flush=True)

            if overrides.get("trade_count_cap") is not None:
                filled_today = count_filled_today()
                if filled_today >= overrides["trade_count_cap"]:
                    print(f"  ✗ Chat trade-count cap reached ({filled_today}/{overrides['trade_count_cap']} today). Skipping.", flush=True)
                    return

            # ── WHALE SCAN — the sole source of trade signals ──────────────────
            # Top 10 traders on the today/weekly/monthly/all-time leaderboards, their
            # weekly profit record, and every open position they hold, printed in full
            # every cycle (see WHALE LEADERBOARD / WHALE SIGNALS boxes below).
            try:
                from agents.connectors.whale_tracker import WhaleTracker
                whale_signals, whale_lb, whale_sig = WhaleTracker().get_whale_signals()
                # Two separate flush=True prints keep each section small enough that
                # Railway's log collector won't split and reorder them.
                print(whale_lb, flush=True)
                print(whale_sig, flush=True)
                # Discord alert for fresh signals (informational — independent of what we trade)
                fresh = [s for s in whale_signals if s.get("is_fresh")]
                if fresh:
                    alert_lines = ["**FRESH WHALE SIGNALS** — new positions opened this cycle:"]
                    for s in fresh[:3]:
                        alert_lines.append(
                            f"> [NEW] {s['new_whale_count']} whale(s) — {s['side'].upper()} | "
                            f"entry {s['avg_entry']:.3f}  now {s['cur_price']:.3f}\n"
                            f">    \"{s['title'][:100]}\""
                        )
                    _discord("\n".join(alert_lines))
            except Exception as e:
                print(f"  ✗ WhaleTracker error — no signal source available. Skipping. ({e})")
                return

            # Focus mode replaces the signal source entirely with just this one whale's
            # current positions — bypassing MIN_WHALES_AGREE and rule 6 (both are the
            # whole point of "copy only this guy today"), but not rules 4/5 below.
            if overrides.get("focus_trader"):
                focus_signals = self._focus_trader_signals(overrides["focus_trader"])
                print(f"  ⚙ Focus mode: {len(focus_signals)} position(s) found for "
                      f"{overrides['focus_trader']['name']} this cycle.", flush=True)
                whale_signals = focus_signals

            if not whale_signals:
                print("  ✗ No whale consensus signals this cycle. Skipping.")
                return

            # ── The only two eligibility rules: never bet the same exact thing twice,
            # and never bet the opposite outcome of a market already bet on. Checked
            # against live open positions AND a persistent local trade history (the
            # latter guards against positions-API indexer lag right after a fill). ──
            trade_history = self._load_trade_history()
            traded_pairs = {
                (h["title"].strip().lower(), h["side"].strip().lower())
                for h in trade_history if h.get("title") and h.get("side")
            }
            traded_token_ids = {h["token_id"] for h in trade_history if h.get("token_id")}

            open_pairs = {
                (p.get("title", "").strip().lower(), (p.get("outcome") or "").strip().lower())
                for p in open_positions if p.get("title")
            }
            held_tokens = {p.get("asset") for p in open_positions if p.get("asset")}

            # Signals arrive pre-sorted: fresh first, then whale_count, then whale_volume.
            # Filter down to every signal that clears both dedup rules AND the today's-
            # events-only timing rule (see rule 6 below), then try them IN ORDER until one
            # actually fills — a single signal that can't execute (FOK killed, or the
            # market's already moved outside Polymarket's tradeable [0.01, 0.99] price
            # range) shouldn't burn the whole 15-minute cycle when other valid consensus
            # signals are sitting right there unused.
            eligible = []
            skipped_not_today = 0
            for s in whale_signals:
                title, side, token_id = s["title"], s["side"], s["asset"]
                key = (title.strip().lower(), side.strip().lower())
                opp_key = (title.strip().lower(), _opposite_side(side))

                if token_id in held_tokens or token_id in traded_token_ids or key in traded_pairs or key in open_pairs:
                    continue  # same exact bet already made
                if opp_key in open_pairs or opp_key in traded_pairs:
                    continue  # already holding the opposite outcome of this market
                if not s.get("is_today_event", False) and s["whale_count"] < HIGH_CONSENSUS_WHALES:
                    skipped_not_today += 1
                    continue  # market doesn't resolve today, and consensus isn't overwhelming enough to override
                eligible.append(s)

            if not eligible:
                extra = f" ({skipped_not_today} skipped as not-today's-event / not enough consensus)" if skipped_not_today else ""
                print(f"  ✗ No eligible whale signal this cycle{extra}. Skipping.")
                return

            size_override = overrides.get("size_override_usd")
            if size_override is not None:
                trade_amount = size_override
                if trade_amount > balance:
                    print(f"  ⚠ Chat size override (${size_override:.2f}) exceeds balance (${balance:.2f}) — using full balance instead.", flush=True)
                    trade_amount = balance
            else:
                trade_amount = balance * BET_FRACTION
            if trade_amount < ABSOLUTE_MIN_TRADE:
                if balance >= ABSOLUTE_MIN_TRADE:
                    trade_amount = ABSOLUTE_MIN_TRADE
                else:
                    print(f"  ✗ Balance ${balance:.2f} too low to place the ${ABSOLUTE_MIN_TRADE} minimum order. Skipping.", flush=True)
                    return

            size_label = f"chat override, ${size_override:.2f}/trade" if size_override is not None else f"25% of ${balance:.2f} balance"

            filled = False
            for i, candidate in enumerate(eligible):
                if self._attempt_trade(candidate, trade_amount, size_label):
                    filled = True
                    break
                if i < len(eligible) - 1:
                    print(f"  → Trying next eligible signal ({len(eligible) - i - 1} more)...")

            if not filled:
                print(f"  ✗ None of {len(eligible)} eligible signal(s) filled this cycle.", flush=True)

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    def _focus_trader_signals(self, trader: dict) -> list:
        """Build ad-hoc single-whale signals from a chat-focused trader's current
        positions — same shape as WhaleTracker.get_whale_signals() output, but
        whale_count is always 1 (MIN_WHALES_AGREE doesn't apply here) and
        is_today_event is always True (rule 6 doesn't apply here either) since
        both are exactly what "copy only this guy today" is asking to bypass."""
        from agents.connectors.whale_tracker import WhaleTracker, MIN_POSITION_VALUE
        try:
            positions = WhaleTracker().get_positions(trader["address"])
        except Exception:
            return []

        signals = []
        for pos in positions:
            asset     = pos.get("asset", "")
            side      = pos.get("outcome", "") or pos.get("side", "")
            avg_price = float(pos.get("avgPrice", 0) or 0)
            cur_price = float(pos.get("curPrice", pos.get("currentValue", 0)) or 0)
            if not asset or avg_price <= 0:
                continue
            size = float(pos.get("size", 0) or 0)
            cur_val = float(pos.get("currentValue", 0) or 0) or (cur_price * size)
            if cur_val < MIN_POSITION_VALUE:
                continue
            signals.append({
                "title": pos.get("title", asset[:30]), "asset": asset, "side": side,
                "avg_entry": round(avg_price, 4), "cur_price": round(cur_price, 4),
                "price_drift": round(abs(cur_price - avg_price) / avg_price, 4) if avg_price else 0.0,
                "whale_count": 1, "whale_volume_total": 0,
                "new_whale_count": 0, "is_fresh": False, "first_seen": "",
                "end_date": (pos.get("endDate") or "")[:10],
                "is_today_event": True,
            })
        return signals

    def _attempt_trade(self, candidate: dict, trade_amount: float, size_label: str) -> bool:
        """Try to place a FOK BUY for one whale signal. Returns True on fill."""
        question    = candidate["title"]
        token_id    = candidate["asset"]
        trade_side  = candidate["side"]
        whale_count = candidate["whale_count"]
        whale_vol   = candidate["whale_volume_total"]
        is_fresh    = candidate["is_fresh"]
        drift       = candidate["price_drift"]
        end_date    = candidate.get("end_date") or "unknown"
        is_today    = candidate.get("is_today_event", False)

        print(f"")
        print(f"  Selected: \"{question[:80]}\"")
        print(f"  Signal: {whale_count} whale(s) {'[FRESH]' if is_fresh else '[ongoing]'}  "
              f"{trade_side.upper()}  entry {candidate['avg_entry']:.3f} -> now {candidate['cur_price']:.3f}  "
              f"({drift:.0%} drift)  combined ${whale_vol:,}")
        print(f"  Event date: {end_date}"
              f"{'  [TODAY]' if is_today else f'  [not today — allowed via {whale_count}-whale consensus override]'}")

        if not token_id:
            print("  ✗ Signal had no token ID — skipping.")
            return False

        print(f"  Size: ${trade_amount:.2f}  ({size_label})")
        print(f"  Placing BUY {trade_side} — ${trade_amount:.2f}  token: {token_id[:20]}...")
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side, PartialCreateOrderOptions
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=trade_amount,
            side=Side.BUY,
            order_type=OrderType.FOK,
        )
        try:
            resp = self.polymarket.client.create_and_post_market_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.FOK,
            )
            order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
            success = resp.get("success", True) if isinstance(resp, dict) else True
            if success:
                print(f"  ✓ FILLED  order {str(order_id)[:16] or '(no id)'}")
                log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                          drift, trade_amount, "filled")
                self._record_trade(token_id, question, trade_side)
                _discord(
                    f"✅ **TRADE FILLED** — {trade_side} ${trade_amount:.2f}\n"
                    f"> {question[:120]}\n"
                    f"> {whale_count} whale(s) {'[FRESH]' if is_fresh else ''} — "
                    f"entry {candidate['avg_entry']:.3f} vs now {candidate['cur_price']:.3f} ({drift:.0%} drift)"
                )
                return True
            print(f"  ✗ Order not successful: {resp}")
            log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                      drift, trade_amount, "error")
            return False
        except Exception as e:
            err = str(e)
            if "fully filled" in err or "FOK" in err.upper() or "killed" in err.lower():
                print(f"  ✗ FOK killed — insufficient liquidity.", flush=True)
                log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                          drift, trade_amount, "fok_killed")
                _discord(
                    f"⚡ **FOK KILLED** — no liquidity for {trade_side} ${trade_amount:.2f}\n"
                    f"> {question[:120]}"
                )
            elif "invalid price" in err.lower():
                print(f"  ✗ Market no longer tradeable (price outside [0.01, 0.99] — likely near-resolved): {e}", flush=True)
                log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                          drift, trade_amount, "untradeable")
            else:
                print(f"  ✗ Order error: {e}")
                import traceback; traceback.print_exc()
                log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                          drift, trade_amount, "error")
            return False


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
