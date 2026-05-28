from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
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
# =========================================================================
MIN_EDGE = 0.05              # strong mispricing: full-size trade
MIN_EDGE_CONVICTION = 0.03  # high-confidence trade even with smaller edge: half-size
MAX_TRADE_FRACTION = 0.10   # never stake more than 10% of current balance on one trade
CONVICTION_TRADE_FRACTION = 0.05  # half-size for conviction trades
MAX_OPEN_POSITIONS = 25    # don't open a new position if this many are already open
DAILY_SPEND_FRACTION = 0.30  # stop opening trades once 30% of starting daily balance spent
DAILY_LOSS_FRACTION = 0.15   # stop for the day if balance drops 15% below the day's start
ABSOLUTE_MIN_TRADE = 1.0   # Polymarket order minimum (~$1). Below this, skip.
TRADE_COOLDOWN_MINUTES = 55  # minimum gap between trades — blocks back-to-back redeploy trades
MAX_EDGE = 0.40              # reject trades where AI diverges > 40pp from market — likely hallucination
STATE_FILE = "trader_daily_state.json"  # project root survives Railway restarts better than /tmp

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
        self.gamma = Gamma()
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
                 "last_trade_time": None, "traded_tokens": []}
        self._save_state(state)
        return state

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

    def _parse_confidence(self, text: str) -> str:
        """Extract HIGH / MEDIUM / LOW from superforecaster or trade output."""
        try:
            m = re.search(r"[Cc]onfidence\s*[:=]\s*(HIGH|MEDIUM|LOW)", text)
            if m:
                return m.group(1).upper()
        except Exception:
            pass
        return "MEDIUM"

    def _resolve_trade(self, market, prob, market_price):
        """Return (token_id, side_label) for the correct side of the trade.

        Uses the token IDs already stored in the selected market's Chroma metadata.
        If bot estimate > market price: Yes is underpriced → buy YES (token index 0).
        If bot estimate < market price: Yes is overpriced → buy NO (token index 1).
        """
        try:
            meta = market[0].dict().get("metadata", {})
            clob_ids = self.agent._safe_parse_list(meta.get("clob_token_ids"))
            if not clob_ids:
                return None, None
            # index 0 = YES token, index 1 = NO token (Polymarket convention)
            if prob > market_price:
                token_id = str(clob_ids[0])
                side = "YES"
            else:
                token_id = str(clob_ids[1]) if len(clob_ids) > 1 else str(clob_ids[0])
                side = "NO"
            if not token_id or token_id in ("0", ""):
                return None, None
            return token_id, side
        except Exception as e:
            print(f"_resolve_trade error: {e}")
            return None, None

    def _parse_prob_and_price(self, best_trade, market):
        """prob = the bot's chosen price (its probability estimate, first number in
        the trade string). price = the market's current 'Yes' price, read from
        outcome_prices in metadata exactly like executor.source_best_trade does."""
        prob = price = None
        # bot's estimate: FIRST price-like number in the trade text
        try:
            text = str(best_trade)
            m = re.search(r"price\s*[:=]\s*([0-9]*\.?[0-9]+)", text)
            if m:
                prob = float(m.group(1))
        except Exception:
            pass
        # market price: from outcome_prices[0] in the market metadata
        try:
            meta = market[0].dict().get("metadata", {})
            op = self.agent._safe_parse_list(meta.get("outcome_prices"))
            if op:
                price = float(op[0])  # 'Yes' price
        except Exception:
            pass
        return prob, price

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

            # ── WHALE SCAN ───────────────────────────────────────────────────
            whale_signals = []
            try:
                from agents.connectors.whale_tracker import WhaleTracker
                whale_signals = WhaleTracker().get_whale_signals()
                if whale_signals:
                    sig_lines = [f"  ┌─ WHALE SIGNALS ── {len(whale_signals)} consensus position(s)"]
                    for s in whale_signals[:5]:
                        sig_lines.append(
                            f"  │  {s['whale_count']}x  {s['side'].upper():<20s}  "
                            f"entry {s['avg_entry']:.3f} → now {s['cur_price']:.3f}  "
                            f"({s['price_drift']:.0%} drift)  \"{s['title'][:40]}\""
                        )
                    if len(whale_signals) > 5:
                        sig_lines.append(f"  │  … +{len(whale_signals)-5} more")
                    sig_lines.append(f"  └───────────────────────────────────────────────────────")
                    print("\n".join(sig_lines))
                    print("")
            except Exception as e:
                print(f"  WhaleTracker error (non-fatal): {e}")

            events = self.polymarket.get_all_tradeable_events()
            if not events:
                print("  ✗ No events found. Skipping.")
                return
            print(f"  Scan: {len(events)} events", end="", flush=True)

            filtered_events = self.agent.filter_events_with_rag(events)
            print(f" → {len(filtered_events)} after RAG filter", end="", flush=True)

            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            if not markets:
                print(f" → 0 markets. Skipping.")
                return
            print(f" → {len(markets)} markets")

            # ── WHALE BOOST: promote whale-signalled markets to the top of the list ──
            # If a whale signal title matches a market question, move that market first
            # so the AI is more likely to select it.
            if whale_signals:
                whale_titles = [s["title"].lower() for s in whale_signals]

                def _whale_score(m: dict) -> int:
                    q = (m.get("question") or "").lower()
                    q_words = set(w for w in q.split() if len(w) > 3)
                    for wt in whale_titles:
                        wt_words = set(w for w in wt.split() if len(w) > 3)
                        if len(q_words & wt_words) >= 2:
                            return 1
                    return 0

                markets = sorted(markets, key=_whale_score, reverse=True)
                boosted = sum(1 for m in markets if _whale_score(m) > 0)
                if boosted:
                    print(f"  Whale boost: {boosted} market(s) moved to top")

            # Collect open position titles for correlation filtering (soft + hard)
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

            filtered_markets = self.agent.filter_markets(markets, open_positions=open_pos_questions)
            print(f"  → {len(filtered_markets)} candidate(s) after AI market selection")
            if not filtered_markets:
                print("  ✗ No markets passed AI filter. Skipping.")
                return

            market = None
            question = ""
            for candidate in filtered_markets:
                q = candidate[0].metadata.get("question", "")
                if not q:
                    continue
                correlated, matching = self._is_correlated_with_open(q, open_pos_questions)
                if correlated:
                    print(f"  Correlation skip: '{q[:55]}' overlaps with '{matching[:40]}'")
                    continue
                market = candidate
                question = q
                break
            if market is None:
                print("  ✗ All candidates correlated with open positions. Skipping.")
                return
            print(f"")
            print(f"  Selected: \"{question[:80]}\"")

            best_trade = self.agent.source_best_trade(market, whale_signals=whale_signals)
            if best_trade is None:
                print("  ✗ Could not generate trade signal. Skipping.")
                return

            prob, price = self._parse_prob_and_price(best_trade, market)
            if prob is None or price is None:
                print("  ✗ Could not determine market price. Skipping.")
                return

            edge = abs(prob - price)
            confidence = self._parse_confidence(best_trade)
            print(f"  Forecast: {prob:.3f}  Market: {price:.3f}  Edge: {edge:+.3f}  [{confidence}]")

            # Hard cap: an edge > 40pp almost always means the AI hallucinated a probability
            # that the live Polymarket crowd would never price. Reject immediately.
            if edge > MAX_EDGE:
                print(f"  ✗ Edge {edge:.3f} exceeds {MAX_EDGE} cap — likely AI hallucination. Skipping.")
                return

            # Two-tier system:
            #   Tier 1 — strong mispricing  (edge >= 5%): full-size trade
            #   Tier 2 — conviction trade   (edge >= 3% + HIGH confidence): half-size trade
            conviction_trade = False
            if edge >= MIN_EDGE:
                print(f"  → STRONG EDGE ({edge:.3f} ≥ {MIN_EDGE}) — full-size trade")
            elif edge >= MIN_EDGE_CONVICTION and confidence == "HIGH":
                conviction_trade = True
                print(f"  → CONVICTION ({edge:.3f} ≥ {MIN_EDGE_CONVICTION}, HIGH confidence) — half-size trade")
            else:
                reason = (f"edge {edge:.3f} < {MIN_EDGE_CONVICTION}" if edge < MIN_EDGE_CONVICTION
                          else f"edge {edge:.3f} < {MIN_EDGE} and confidence {confidence} ≠ HIGH")
                print(f"  ✗ No trade: {reason}. Skipping.")
                return

            amount = self.agent.format_trade_prompt_for_execution(best_trade)

            full_max    = balance * MAX_TRADE_FRACTION
            conv_max    = balance * CONVICTION_TRADE_FRACTION
            size_cap    = conv_max if conviction_trade else full_max
            remaining_daily = max(spend_cap - spent_today, 0)
            trade_amount = min(float(amount) if amount else size_cap, size_cap, remaining_daily)

            if trade_amount < ABSOLUTE_MIN_TRADE:
                # Bump to minimum if the wallet and daily budget both allow a $1 order
                if remaining_daily >= ABSOLUTE_MIN_TRADE and balance >= ABSOLUTE_MIN_TRADE:
                    print(f"  Size bumped ${trade_amount:.2f} → ${ABSOLUTE_MIN_TRADE:.2f} (minimum order)")
                    trade_amount = ABSOLUTE_MIN_TRADE
                else:
                    print(f"  ✗ Trade size ${trade_amount:.2f} below ${ABSOLUTE_MIN_TRADE} minimum — balance too low. Skipping.")
                    return
            print(f"  Size: ${trade_amount:.2f}  (cap ${size_cap:.2f}, daily room ${remaining_daily:.2f})")

            token_id, trade_side = self._resolve_trade(market, prob, price)

            # Dedup: check live positions first (survives redeploys), state file as fallback
            try:
                held = self.polymarket.get_held_token_ids()
                if token_id and token_id in held:
                    print(f"  ✗ Dedup: already holding token {token_id[:20]}... Skipping.")
                    return
            except Exception as e:
                print(f"  Dedup check failed: {e}")
            traded_tokens = state.get("traded_tokens", [])
            if token_id and token_id in traded_tokens:
                print(f"  ✗ Dedup: token {token_id[:20]}... already traded today. Skipping.")
                return

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
                    log_trade(question, token_id, trade_side, prob, price, edge,
                              trade_amount, "filled")
                    _discord(
                        f"✅ **TRADE FILLED** — {trade_side} ${trade_amount:.2f}\n"
                        f"> {question[:120]}\n"
                        f"> Forecast {prob:.3f} vs market {price:.3f}  edge {edge:+.3f}  [{confidence}]"
                    )
                except Exception as e:
                    err = str(e)
                    if "fully filled" in err or "FOK" in err.upper() or "killed" in err.lower():
                        print(f"  ✗ FOK killed — insufficient liquidity. Will retry next cycle.")
                        log_trade(question, token_id, trade_side, prob, price, edge,
                                  trade_amount, "fok_killed")
                        _discord(
                            f"⚡ **FOK KILLED** — no liquidity for {trade_side} ${trade_amount:.2f}\n"
                            f"> {question[:120]}"
                        )
                    else:
                        print(f"  ✗ Order error: {e}")
                        import traceback; traceback.print_exc()
                        log_trade(question, token_id, trade_side, prob, price, edge,
                                  trade_amount, "error")
            else:
                print("  ✗ Could not resolve token ID for market — skipping.")

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
