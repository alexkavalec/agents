from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket
import shutil
import os
import json
import re
import datetime

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
STATE_FILE = "/tmp/trader_daily_state.json"
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
        state = {"date": self._today(), "start_balance": current_balance, "spent": 0.0}
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

    def get_liquid_token(self, question, sampling_markets):
        if not question or not sampling_markets:
            return None
        q_lower = question.lower()[:30]
        for sm in sampling_markets:
            try:
                if isinstance(sm, dict):
                    sm_q = sm.get("question", "").lower()
                    tokens = sm.get("tokens", [])
                    if q_lower[:20] in sm_q or sm_q[:20] in q_lower:
                        if tokens:
                            t = tokens[1] if len(tokens) > 1 else tokens[0]
                            tid = t.get("token_id", "") if isinstance(t, dict) else str(t)
                            if tid and tid not in ("0", ""):
                                return tid
            except Exception:
                continue
        return None

    def _parse_prob_and_price(self, best_trade, market):
        prob = price = None
        try:
            text = str(best_trade)
            m = re.search(r"price\s*[:=]\s*([0-9]*\.?[0-9]+)", text)
            if m:
                prob = float(m.group(1))
        except Exception:
            pass
        try:
            meta = market[0].dict().get("metadata", {})
            tid = meta.get("clobTokenIds") or meta.get("token_id")
            if isinstance(tid, str) and tid:
                price = self.polymarket.get_orderbook_price(tid)
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

            print("Fetching liquid sampling markets...")
            try:
                raw = self.polymarket.client.get_sampling_simplified_markets()
                sampling_data = raw.get("data", raw) if isinstance(raw, dict) else raw
                print(f"1. FOUND {len(sampling_data)} LIQUID SAMPLING MARKETS")
            except Exception as e:
                print(f"Sampling error: {e}")
                sampling_data = []

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

            token_id = self.get_liquid_token(question, sampling_data)

            resp = None
            if token_id:
                print(f"Found liquid token: {token_id[:20]}...")
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
                print("No matching liquid token, trying direct execution...")
                resp = self.polymarket.execute_market_order(market, trade_amount)
                print(f"7. TRADED: {resp}")

            success = True
            if isinstance(resp, dict):
                success = resp.get("success", True)
            if success:
                state["spent"] = spent_today + trade_amount
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
