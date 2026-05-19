from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket
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

    def one_best_trade(self) -> None:
        try:
            self.pre_trade_logic()

            # Get liquid sampling markets for token lookup
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

            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            print(f"Trade amount: {amount}")

            # Find liquid token from sampling markets
            token_id = self.get_liquid_token(question, sampling_data)

            trade_amount = max(float(amount) if amount else 0, 1.0)

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
                trade = self.polymarket.execute_market_order(market, trade_amount)
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
