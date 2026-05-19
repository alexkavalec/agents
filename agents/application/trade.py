from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket
from agents.utils.objects import SimpleMarket
import ast
import shutil


class Trader:
    def __init__(self):
        self.polymarket = Polymarket()
        self.gamma = Gamma()
        self.agent = Agent()

    def pre_trade_logic(self) -> None:
        self.clear_local_dbs()

    def clear_local_dbs(self) -> None:
        try:
            shutil.rmtree("local_db_events")
        except:
            pass
        try:
            shutil.rmtree("local_db_markets")
        except:
            pass

    def get_sampling_markets(self):
        try:
            raw = self.polymarket.client.get_sampling_simplified_markets()
            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            return data
        except Exception as e:
            print(f"Sampling markets error: {e}")
            return []

    def find_liquid_token(self, question, sampling_markets):
        if not question or not sampling_markets:
            return None
        q_lower = question.lower()[:30]
        for sm in sampling_markets:
            try:
                sm_q = ""
                if isinstance(sm, dict):
                    sm_q = sm.get("question", "").lower()
                    tokens = sm.get("tokens", [])
                elif hasattr(sm, "question"):
                    sm_q = (sm.question or "").lower()
                    tokens = sm.clob_token_ids if hasattr(sm, "clob_token_ids") else []
                else:
                    continue
                if q_lower[:20] in sm_q or sm_q[:20] in q_lower:
                    if isinstance(tokens, list) and tokens:
                        t = tokens[1] if len(tokens) > 1 else tokens[0]
                        tid = t.get("token_id", "") if isinstance(t, dict) else str(t)
                        if tid and tid not in ("0", ""):
                            return tid
            except Exception:
                continue
        return None

    def one_best_trade(self) -> None:
        try:
            self.pre_trade_logic()

            print("Fetching liquid sampling markets...")
            sampling_markets = self.get_sampling_markets()
            print(f"1. FOUND {len(sampling_markets)} LIQUID SAMPLING MARKETS")

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

            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            print(f"Trade amount: {amount}")

            # Find liquid token from sampling markets
            token_id = self.find_liquid_token(question, sampling_markets)

            if token_id:
                print(f"Found liquid token: {token_id[:20]}...")
                from py_clob_client.clob_types import MarketOrderArgs, OrderType
                from py_clob_client.order_builder.constants import BUY
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=max(amount, 1.0),
                    side=BUY,
                )
                signed_order = self.polymarket.client.create_market_order(order_args)
                print(f"Signed order created")
                resp = self.polymarket.client.post_order(signed_order, orderType=OrderType.FOK)
                print(f"7. TRADED: {resp}")
            else:
                print("No matching liquid token found in sampling markets")
                print("Attempting direct execution...")
                trade = self.polymarket.execute_market_order(market, max(amount, 1.0))
                print(f"7. TRADED: {trade}")

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
