from agents.application.executor import Executor as Agent
from agents.polymarket.polymarket import Polymarket
from agents.memory.trade_log import log_trade, log_lesson, get_recent_lessons
import shutil
import os
import json
import re
import datetime
import time
import requests as _requests

# =========================================================================
# GUARDRAILS CONFIG  -- tune these. All money limits are FRACTIONS of balance.
#
# Strategy: the bot trades ONLY on whale-leaderboard consensus (see WhaleTracker /
# get_whale_signals). There is no AI market scan, RAG filter, or superforecaster
# in the entry path anymore — a trade fires only when MIN_WHALES_AGREE (in
# whale_tracker.py) independent top-leaderboard traders (today/weekly/monthly/
# all-time) hold the same side of the same market.
# =========================================================================
MAX_TRADE_FRACTION = 0.10   # never stake more than 10% of current balance on one trade
MIN_TRADE_FRACTION = 0.03   # floor size for a bare-minimum qualifying signal
MAX_OPEN_POSITIONS = 25    # don't open a new position if this many are already open
DAILY_SPEND_FRACTION = 0.30  # stop opening trades once 30% of starting daily balance spent
DAILY_LOSS_FRACTION = 0.15   # stop for the day if balance drops 15% below the day's start
ABSOLUTE_MIN_TRADE = 1.0   # Polymarket order minimum (~$1). Below this, skip.
TRADE_COOLDOWN_MINUTES = 55  # minimum gap between trades — blocks back-to-back redeploy trades
STATE_FILE = "trader_daily_state.json"  # project root survives Railway restarts better than /tmp

# Position sizing scales with signal strength: how many whales agree, and how much
# combined profit/volume they represent. Both factors saturate at 1.0 and are
# averaged; a fresh signal (a whale newly opened this cycle) gets a bonus on top,
# capped at MAX_TRADE_FRACTION.
WHALE_COUNT_SATURATION = 5          # whale_count at/above this maxes out the count factor
WHALE_VOLUME_SATURATION = 2_000_000  # combined whale $ profit/volume that maxes out the volume factor
FRESH_SIGNAL_BONUS = 1.25           # multiplier applied to fresh (newly-entered) signals

_CORR_STOP = {
    "will", "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "do", "does", "did", "would", "could", "should", "may",
    "win", "lose", "this", "that", "in", "on", "at", "by", "for", "to",
    "of", "and", "or", "not", "yes", "no", "its", "their", "2025", "2026", "2027",
    # generic time/event words that span unrelated markets
    "meeting", "june", "july", "august", "september", "october", "november",
    "december", "january", "february", "march", "april",
    # generic political/outcome words that appear across unrelated elections
    "presidential", "candidate", "winner",
    # election/vote words appear in every election market across all countries
    "election", "elections", "elect", "elected", "vote", "votes", "voting",
}

# Position management — thresholds before ANY action is even considered.
# Small moves are completely ignored. AI re-evaluation is always required.
POSITION_REVIEW_MIN_MOVE    = 0.50   # ignore positions that moved < 50% — that's just noise
POSITION_TAKE_PROFIT_GAIN   = 0.60   # consider taking profit if up >= 60%
POSITION_STOP_LOSS_LOSS     = -0.60  # consider cutting if down >= 60%
POSITION_ACTION_EDGE_MAX    = 0.08   # only act if AI now shows edge < 8% (thesis gone)
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


class Trader:
    def __init__(self):
        self.polymarket = Polymarket()
        self.agent = Agent()

    def _today(self) -> str:
        return datetime.date.today().isoformat()

    def _load_state(self, current_balance: float) -> dict:
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == self._today():
                return state
        except Exception:
            pass
        state = {"date": self._today(), "start_balance": current_balance, "spent": 0.0,
                 "last_trade_time": None, "traded_tokens": [], "recently_skipped": {}}
        self._save_state(state)
        return state

    def _record_skip(self, state: dict, question: str) -> None:
        """Record a market as recently evaluated-but-skipped (expires after 4h)."""
        if not question:
            return
        rs = state.get("recently_skipped", {})
        rs[question[:80]] = time.time()
        state["recently_skipped"] = rs
        self._save_state(state)

    def _save_state(self, state: dict) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"WARN: could not save daily state: {e}")

    def pre_trade_logic(self) -> None:
        self.clear_local_dbs()

    def clear_local_dbs(self) -> None:
        for d in ("local_db_events", "local_db_markets"):
            try:
                shutil.rmtree(d)
            except Exception:
                pass

    def _extract_keywords(self, text: str) -> set:
        words = re.findall(r"[a-z0-9]+", text.lower())
        return {w for w in words if len(w) > 3 and w not in _CORR_STOP}

    def _is_correlated_with_open(self, candidate_q: str, open_questions: list) -> tuple:
        """Returns (True, matching_question) if candidate shares ≥2 keywords with any open position."""
        c_kws = self._extract_keywords(candidate_q)
        for oq in open_questions:
            if not oq:
                continue
            overlap = c_kws & self._extract_keywords(oq)
            if len(overlap) >= 2:
                return True, oq
        return False, None

    def _count_open_positions(self) -> int:
        try:
            if hasattr(self.polymarket, "get_open_positions"):
                return len(self.polymarket.get_open_positions())
        except Exception:
            pass
        return -1

    def one_best_trade(self) -> None:
        try:
            self.pre_trade_logic()

            try:
                balance = float(self.polymarket.get_usdc_balance())
            except Exception as e:
                print(f"Could not read balance ({e}); aborting this run for safety.")
                return
            from agents.memory.trade_log import get_stats
            from agents.memory.scoreboard import resolve_completed, get_scoreboard_line
            resolve_completed(self.polymarket)

            state = self._load_state(balance)
            start_bal  = state["start_balance"]
            spent_today = state["spent"]
            spend_cap   = start_bal * DAILY_SPEND_FRACTION
            loss_floor  = start_bal * (1 - DAILY_LOSS_FRACTION)
            open_count  = self._count_open_positions()

            # ── CYCLE HEADER — printed as one call so Railway log collector keeps it intact ──
            stats = get_stats()
            print("\n".join([
                "",
                "  ┌─ CYCLE SUMMARY ───────────────────────────────────────",
                f"  │  Balance : ${balance:.2f}  (start ${start_bal:.2f})",
                f"  │  Spent   : ${spent_today:.2f} / ${spend_cap:.2f} daily cap",
                f"  │  Positions: {open_count if open_count >= 0 else '?'} / {MAX_OPEN_POSITIONS} max",
                get_scoreboard_line(),
                f"  │  Trades  : {stats['total_attempts']} attempts | {stats['filled']} filled | "
                f"{stats['fok_killed']} FOK killed | "
                f"{stats['closed_profit']}W {stats['closed_loss']}L",
                "  └───────────────────────────────────────────────────────",
                "",
            ]))

            if balance <= loss_floor:
                print(f"  ✗ DAILY LOSS LIMIT — balance ${balance:.2f} hit floor ${loss_floor:.2f}. Stopping.")
                _discord(f"🛑 **DAILY LOSS LIMIT** — balance ${balance:.2f} hit floor ${loss_floor:.2f}. Bot halted for today.")
                return
            if spent_today >= spend_cap:
                print(f"  ✗ DAILY SPEND CAP — spent ${spent_today:.2f} of ${spend_cap:.2f}. Stopping.")
                _discord(f"🛑 **DAILY SPEND CAP** — spent ${spent_today:.2f} of ${spend_cap:.2f} limit. Bot halted for today.")
                return
            if open_count >= MAX_OPEN_POSITIONS:
                print(f"  ✗ MAX POSITIONS — {open_count}/{MAX_OPEN_POSITIONS} open. Skipping.")
                _discord(f"⚠️ **MAX POSITIONS** — {open_count}/{MAX_OPEN_POSITIONS} open. Skipping this cycle.")
                return

            # Cooldown: API-first (survives redeploys), state file as fallback
            elapsed = None
            try:
                elapsed = self.polymarket.get_last_trade_minutes_ago()
            except Exception:
                pass
            if elapsed is None:
                ltt = state.get("last_trade_time")
                if ltt:
                    try:
                        elapsed = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(ltt)).total_seconds() / 60
                    except Exception:
                        pass
            if elapsed is not None and elapsed < TRADE_COOLDOWN_MINUTES:
                print(f"  ✗ COOLDOWN — last trade {elapsed:.0f} min ago (need {TRADE_COOLDOWN_MINUTES}). Skipping.")
                return

            # ── WHALE SCAN — the sole source of trade signals ──────────────────
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

            if not whale_signals:
                print("  ✗ No whale consensus signals this cycle. Skipping.")
                return

            # Pre-filter: skip signals evaluated but not traded in the last 4h.
            now_ts = time.time()
            recently_skipped = {k: v for k, v in state.get("recently_skipped", {}).items()
                                 if now_ts - v < 4 * 3600}
            state["recently_skipped"] = recently_skipped

            # Collect open position titles/tokens for correlation + dedup filtering
            open_pos_questions = []
            try:
                positions_data = self.polymarket.get_open_positions()
                open_pos_questions = [
                    p.get("title") or p.get("question", "")
                    for p in positions_data
                    if p.get("title") or p.get("question")
                ]
            except Exception as e:
                print(f"  Could not fetch open positions for correlation check: {e}")

            held_tokens = set()
            try:
                held_tokens = self.polymarket.get_held_token_ids()
            except Exception as e:
                print(f"  Could not fetch held token IDs for dedup check: {e}")
            traded_tokens_today = set(state.get("traded_tokens", []))

            # Signals arrive pre-sorted: fresh first, then whale_count, then whale_volume.
            # Walk them in order and take the first one that clears all guardrails.
            candidate = None
            for s in whale_signals:
                title = s["title"]
                token_id = s["asset"]
                if title[:80] in recently_skipped:
                    continue
                if token_id and (token_id in held_tokens or token_id in traded_tokens_today):
                    continue
                correlated, matching = self._is_correlated_with_open(title, open_pos_questions)
                if correlated:
                    print(f"  Correlation skip: '{title[:55]}' overlaps with '{matching[:40]}'")
                    continue
                candidate = s
                break

            if candidate is None:
                print("  ✗ No eligible whale signal — all correlated, held, or recently skipped. Skipping.")
                return

            question    = candidate["title"]
            token_id    = candidate["asset"]
            trade_side  = candidate["side"]
            whale_count = candidate["whale_count"]
            whale_vol   = candidate["whale_volume_total"]
            is_fresh    = candidate["is_fresh"]
            drift       = candidate["price_drift"]

            print(f"")
            print(f"  Selected: \"{question[:80]}\"")
            print(f"  Signal: {whale_count} whale(s) {'[FRESH]' if is_fresh else '[ongoing]'}  "
                  f"{trade_side.upper()}  entry {candidate['avg_entry']:.3f} -> now {candidate['cur_price']:.3f}  "
                  f"({drift:.0%} drift)  combined ${whale_vol:,}")

            # Sizing scales with signal strength: how many whales agree (count_factor)
            # and how much combined profit/volume they represent (volume_factor), each
            # saturating at 1.0. A fresh signal gets a bonus, capped at MAX_TRADE_FRACTION.
            count_factor  = min(whale_count / WHALE_COUNT_SATURATION, 1.0)
            volume_factor = min(whale_vol / WHALE_VOLUME_SATURATION, 1.0)
            size_fraction = MIN_TRADE_FRACTION + (MAX_TRADE_FRACTION - MIN_TRADE_FRACTION) * (
                (count_factor + volume_factor) / 2
            )
            if is_fresh:
                size_fraction *= FRESH_SIGNAL_BONUS
            size_fraction = min(size_fraction, MAX_TRADE_FRACTION)

            size_cap = balance * size_fraction
            remaining_daily = max(spend_cap - spent_today, 0)
            trade_amount = min(size_cap, remaining_daily)

            if trade_amount < ABSOLUTE_MIN_TRADE:
                # Bump to minimum if the wallet and daily budget both allow a $1 order
                if remaining_daily >= ABSOLUTE_MIN_TRADE and balance >= ABSOLUTE_MIN_TRADE:
                    print(f"  Size bumped ${trade_amount:.2f} → ${ABSOLUTE_MIN_TRADE:.2f} (minimum order)")
                    trade_amount = ABSOLUTE_MIN_TRADE
                else:
                    print(f"  ✗ Trade size ${trade_amount:.2f} below ${ABSOLUTE_MIN_TRADE} minimum — balance too low. Skipping.", flush=True)
                    self._record_skip(state, question)
                    return
            print(f"  Size: ${trade_amount:.2f}  (fraction {size_fraction:.1%}, cap ${size_cap:.2f}, daily room ${remaining_daily:.2f})")

            resp = None
            order_filled = False
            if token_id:
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
                    print(f"  ✓ FILLED  order {str(order_id)[:16] or '(no id)'}")
                    order_filled = True
                    log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                              drift, trade_amount, "filled")
                    _discord(
                        f"✅ **TRADE FILLED** — {trade_side} ${trade_amount:.2f}\n"
                        f"> {question[:120]}\n"
                        f"> {whale_count} whale(s) {'[FRESH]' if is_fresh else ''} — "
                        f"entry {candidate['avg_entry']:.3f} vs now {candidate['cur_price']:.3f} ({drift:.0%} drift)"
                    )
                except Exception as e:
                    err = str(e)
                    if "fully filled" in err or "FOK" in err.upper() or "killed" in err.lower():
                        print(f"  ✗ FOK killed — insufficient liquidity. Suppressing market for 4h.", flush=True)
                        self._record_skip(state, question)
                        log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                                  drift, trade_amount, "fok_killed")
                        _discord(
                            f"⚡ **FOK KILLED** — no liquidity for {trade_side} ${trade_amount:.2f}\n"
                            f"> {question[:120]}"
                        )
                    else:
                        print(f"  ✗ Order error: {e}")
                        import traceback; traceback.print_exc()
                        log_trade(question, token_id, trade_side, candidate["avg_entry"], candidate["cur_price"],
                                  drift, trade_amount, "error")
            else:
                print("  ✗ Signal had no token ID — skipping.")

            success = order_filled
            if order_filled and isinstance(resp, dict):
                success = resp.get("success", True)
            if success:
                state["spent"] = spent_today + trade_amount
                state["last_trade_time"] = datetime.datetime.utcnow().isoformat()
                traded_tokens = state.get("traded_tokens", [])
                if token_id and token_id not in traded_tokens:
                    traded_tokens.append(token_id)
                state["traded_tokens"] = traded_tokens
                self._save_state(state)
                print(f"  Spent today: ${state['spent']:.2f} / ${spend_cap:.2f} cap")

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    def _reeval_position_probability(self, question: str, description: str) -> float:
        """Re-run superforecaster on an existing position. Returns p(YES) or None."""
        try:
            prompt = self.agent.prompter.superforecaster(question, description, ["Yes", "No"])
            result = self.agent.llm.invoke(prompt)
            m = re.search(r"likelihood\s*`?([0-9]*\.?[0-9]+)", result.content)
            if m:
                p = float(m.group(1))
                print(f"  AI re-eval: p(Yes) = {p:.2f}")
                return p
        except Exception as e:
            print(f"  Re-eval error: {e}")
        return None

    def _close_position(self, token_id: str, size: float, reason: str, lesson_ctx: dict = None) -> None:
        """Sell the entire position via a market SELL order."""
        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, Side, PartialCreateOrderOptions
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size,
                side=Side.SELL,
                order_type=OrderType.FOK,
            )
            resp = self.polymarket.client.create_and_post_market_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.FOK,
            )
            print(f"  {reason} executed: {resp}")
            # Prevent re-entry on the same token this cycle.
            # Only update an existing state file — never create one with balance=0,
            # which would corrupt the daily spend cap calculation.
            try:
                if token_id and os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r") as f:
                        state = json.load(f)
                    if state.get("date") == self._today():
                        traded = state.get("traded_tokens", [])
                        if token_id not in traded:
                            traded.append(token_id)
                            state["traded_tokens"] = traded
                            self._save_state(state)
            except Exception:
                pass
            if lesson_ctx:
                outcome = "closed_profit" if lesson_ctx.get("pnl_pct", 0) > 0 else "closed_loss"
                lesson = (
                    f"{reason} after {lesson_ctx.get('pnl_pct', 0):+.1%} move. "
                    f"Held {lesson_ctx.get('side', '?')} at entry price {lesson_ctx.get('entry_price', 0):.3f}. "
                    f"AI edge had collapsed — thesis confirmed broken."
                )
                log_lesson(
                    question=lesson_ctx.get("question", ""),
                    side=lesson_ctx.get("side", ""),
                    entry_price=lesson_ctx.get("entry_price", 0),
                    pnl_pct=lesson_ctx.get("pnl_pct", 0),
                    outcome=outcome,
                    lesson=lesson,
                )
        except Exception as e:
            print(f"  {reason} sell failed: {e}")

    def maintain_positions(self) -> None:
        """Review open positions each cycle.
        Only exits when price has moved dramatically AND AI confirms the thesis is gone.
        A small dip is never sufficient reason to close — the threshold is intentionally high."""
        try:
            positions = self.polymarket.get_open_positions()
            if not positions:
                return
            print(f"Reviewing {len(positions)} open position(s)...")

            for p in positions:
                asset    = p.get("asset", "")
                outcome  = p.get("outcome", "")   # "Yes" or "No"
                title    = p.get("title", asset[:20])
                size     = float(p.get("size", 0) or 0)
                avg_price = float(p.get("avgPrice", 0) or 0)

                if not asset or size <= 0:
                    continue

                # Compute current value — use API fields if present, else CLOB price
                initial_value = float(p.get("initialValue", 0) or 0)
                current_value = float(p.get("currentValue", 0) or 0)
                if initial_value <= 0 and avg_price > 0:
                    initial_value = size * avg_price
                if current_value <= 0:
                    try:
                        current_value = size * self.polymarket.get_midpoint_price(asset)
                    except Exception:
                        continue
                if initial_value <= 0:
                    continue

                pnl_pct = (current_value - initial_value) / initial_value
                print(f"  {title[:45]} [{outcome}] P&L: {pnl_pct:+.1%}")

                # Gate 1: ignore anything under the minimum move threshold.
                # This is intentional — small dips are noise, not a reason to exit.
                if abs(pnl_pct) < POSITION_REVIEW_MIN_MOVE:
                    continue

                # STOP LOSS: close unconditionally — no AI re-eval.
                # The AI has already been shown to override -60% losses with optimistic forecasts.
                # At this loss level, the original thesis is broken regardless of AI opinion.
                if pnl_pct <= POSITION_STOP_LOSS_LOSS:
                    lesson_ctx = {
                        "question": title,
                        "side": outcome,
                        "entry_price": avg_price,
                        "pnl_pct": pnl_pct,
                    }
                    print(f"  STOP LOSS (unconditional): down {pnl_pct:+.1%} — closing without AI re-eval.")
                    _discord(
                        f"🔴 **STOP LOSS** — {outcome} {pnl_pct:+.1%}\n"
                        f"> {title[:120]}"
                    )
                    self._close_position(asset, size, "STOP LOSS", lesson_ctx)
                    continue

                # TAKE PROFIT: only close if AI confirms edge has collapsed.
                # Holding a winner that's still mispriced is fine.
                if pnl_pct < POSITION_TAKE_PROFIT_GAIN:
                    continue

                print(f"  Take-profit threshold reached ({pnl_pct:+.1%}) — running AI re-evaluation...")
                market_data = self.polymarket.get_market(asset)
                if not market_data:
                    print(f"  Could not fetch market data, holding.")
                    continue

                question    = market_data.get("question", "")
                description = market_data.get("description", "")
                if not question:
                    continue

                ai_p_yes = self._reeval_position_probability(question, description)
                if ai_p_yes is None:
                    print(f"  AI re-eval failed — holding.")
                    continue

                # Compute current edge for the side we hold
                current_token_price = current_value / size
                if outcome.lower() == "yes":
                    current_edge = ai_p_yes - current_token_price
                else:
                    current_edge = (1.0 - ai_p_yes) - current_token_price

                if abs(current_edge) >= POSITION_ACTION_EDGE_MAX:
                    print(f"  AI still sees edge {current_edge:+.2f} — holding despite {pnl_pct:+.1%} gain.")
                    continue

                lesson_ctx = {
                    "question": question,
                    "side": outcome,
                    "entry_price": avg_price,
                    "pnl_pct": pnl_pct,
                }
                print(f"  TAKE PROFIT: up {pnl_pct:+.1%}, AI edge = {current_edge:+.2f} (closed)")
                _discord(
                    f"🟢 **TAKE PROFIT** — {outcome} {pnl_pct:+.1%}, AI edge collapsed to {current_edge:+.2f}\n"
                    f"> {question[:120]}"
                )
                self._close_position(asset, size, "TAKE PROFIT", lesson_ctx)

        except Exception as e:
            print(f"maintain_positions error: {e}")
            import traceback
            traceback.print_exc()

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
