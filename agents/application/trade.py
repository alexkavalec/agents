from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket
import shutil
import os
import json
import re
import datetime
import time

# =========================================================================
# GUARDRAILS CONFIG  -- tune these. All money limits are FRACTIONS of balance.
# =========================================================================
MIN_EDGE = 0.10            # require >=10 percentage-point gap between the bot's
                           # estimated probability and the market price, or skip.
MAX_TRADE_FRACTION = 0.10  # never stake more than 10% of current balance on one trade
MAX_OPEN_POSITIONS = 5     # don't open a new position if this many are already open
DAILY_SPEND_FRACTION = 0.30  # stop opening trades once 30% of starting daily balance spent
DAILY_LOSS_FRACTION = 0.15   # stop for the day if balance drops 15% below the day's start
ABSOLUTE_MIN_TRADE = 1.0   # Polymarket order minimum (~$1). Below this, skip.
TRADE_COOLDOWN_MINUTES = 55  # minimum gap between trades — blocks back-to-back redeploy trades
STATE_FILE = "trader_daily_state.json"  # project root survives Railway restarts better than /tmp
# =========================================================================


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
            print(f"Balance: ${balance:.2f}")

            state = self._load_state(balance)
            start_bal = state["start_balance"]
            spent_today = state["spent"]

            loss_floor = start_bal * (1 - DAILY_LOSS_FRACTION)
            if balance <= loss_floor:
                print(f"DAILY LOSS LIMIT hit (balance ${balance:.2f} <= floor ${loss_floor:.2f}). Stopping for the day.")
                return

            spend_cap = start_bal * DAILY_SPEND_FRACTION
            if spent_today >= spend_cap:
                print(f"DAILY SPEND CAP hit (spent ${spent_today:.2f} >= cap ${spend_cap:.2f}). Stopping for the day.")
                return

            open_count = self._count_open_positions()
            if open_count >= 0:
                print(f"Open positions: {open_count}")
                if open_count >= MAX_OPEN_POSITIONS:
                    print(f"MAX OPEN POSITIONS reached ({open_count}/{MAX_OPEN_POSITIONS}). Skipping.")
                    return
            else:
                print("Open positions: unknown (could not fetch) - proceeding cautiously.")

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
            if elapsed is not None:
                if elapsed < TRADE_COOLDOWN_MINUTES:
                    print(f"COOLDOWN: last trade was {elapsed:.1f} min ago (need {TRADE_COOLDOWN_MINUTES} min). Skipping.")
                    return
                print(f"Cooldown OK: last trade was {elapsed:.1f} min ago.")

            events = self.polymarket.get_all_tradeable_events()
            print(f"2. FOUND {len(events)} EVENTS")
            if not events:
                print("No events found, exiting.")
                return

            filtered_events = self.agent.filter_events_with_rag(events)
            print(f"3. AI FILTERED TO {len(filtered_events)} EVENTS")

            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            print(f"4. MAPPED TO {len(markets)} MARKETS")
            if not markets:
                print("No markets, exiting.")
                return

            filtered_markets = self.agent.filter_markets(markets)
            print(f"5. AI FILTERED TO {len(filtered_markets)} MARKETS")
            if not filtered_markets:
                print("No filtered markets, exiting.")
                return

            market = filtered_markets[0]
            question = market[0].dict()["metadata"].get("question", "")
            print(f"Selected: {question[:80]}")

            best_trade = self.agent.source_best_trade(market)
            print(f"6. TRADE: {best_trade}")

            prob, price = self._parse_prob_and_price(best_trade, market)
            if prob is not None and price is not None:
                edge = abs(prob - price)
                print(f"Edge check: estimate={prob:.3f} market={price:.3f} edge={edge:.3f} (need >= {MIN_EDGE})")
                if edge < MIN_EDGE:
                    print(f"NO REAL EDGE ({edge:.3f} < {MIN_EDGE}). Skipping trade this hour.")
                    return
            else:
                print("Edge check: could not determine market price; skipping trade for safety.")
                return

            amount = self.agent.format_trade_prompt_for_execution(best_trade)

            max_trade = balance * MAX_TRADE_FRACTION
            remaining_daily = max(spend_cap - spent_today, 0)
            trade_amount = min(float(amount) if amount else max_trade, max_trade, remaining_daily)

            if trade_amount < ABSOLUTE_MIN_TRADE:
                print(f"Trade size ${trade_amount:.2f} below minimum ${ABSOLUTE_MIN_TRADE}. Skipping.")
                return
            print(f"Trade amount: ${trade_amount:.2f} (max/trade ${max_trade:.2f}, daily room ${remaining_daily:.2f})")

            token_id, trade_side = self._resolve_trade(market, prob, price)

            # Dedup: check live positions first (survives redeploys), state file as fallback
            try:
                held = self.polymarket.get_held_token_ids()
                if token_id and token_id in held:
                    print(f"DEDUP: already holding token {token_id[:20]}... Skipping.")
                    return
            except Exception as e:
                print(f"Position dedup check failed: {e}")
            traded_tokens = state.get("traded_tokens", [])
            if token_id and token_id in traded_tokens:
                print(f"DEDUP: token {token_id[:20]}... already traded today (state file). Skipping.")
                return

            resp = None
            if token_id:
                print(f"Placing BUY {trade_side} order, token: {token_id[:20]}...")
                from py_clob_client_v2 import MarketOrderArgs, OrderType, Side, PartialCreateOrderOptions
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=trade_amount,
                    side=Side.BUY,
                    order_type=OrderType.FOK,
                )
                resp = self.polymarket.client.create_and_post_market_order(
                    order_args=order_args,
                    options=PartialCreateOrderOptions(tick_size="0.01"),
                    order_type=OrderType.FOK,
                )
                print(f"7. TRADED: {resp}")
            else:
                print("Could not resolve token ID for selected market — skipping trade for safety.")

            success = True
            if isinstance(resp, dict):
                success = resp.get("success", True)
            if success:
                state["spent"] = spent_today + trade_amount
                state["last_trade_time"] = datetime.datetime.utcnow().isoformat()
                traded_tokens = state.get("traded_tokens", [])
                if token_id and token_id not in traded_tokens:
                    traded_tokens.append(token_id)
                state["traded_tokens"] = traded_tokens
                self._save_state(state)
                print(f"Daily spent now ${state['spent']:.2f} of ${spend_cap:.2f} cap.")

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
