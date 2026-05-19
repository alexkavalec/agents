from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket
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

    def is_market_liquid(self, market) -> bool:
        try:
            token_ids = ast.literal_eval(market[0].dict()["metadata"]["clob_token_ids"])
            if not isinstance(token_ids, list):
                token_ids = [token_ids]
            for token_id in token_ids:
                try:
                    ob = self.polymarket.get_orderbook(str(token_id))
                    if ob:
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            print(f"Liquidity check error: {e}")
            return False

    def one_best_trade(self) -> None:
        try:
            self.pre_trade_logic()

            events = self.polymarket.get_all_tradeable_events()
            print(f"1. FOUND {len(events)} EVENTS")
            if len(events) == 0:
                print("No tradeable events found, exiting.")
                return

            filtered_events = self.agent.filter_events_with_rag(events)
            print(f"2. FILTERED {len(filtered_events)} EVENTS")

            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            print(f"3. FOUND {len(markets)} MARKETS")
            if not markets:
                print("No markets found, exiting.")
                return

            print("Checking liquidity on all markets...")
            liquid_markets = []
            for m in markets:
                liquid = self.is_market_liquid(m)
                question = m[0].dict()["metadata"].get("question", "unknown")[:50]
                print(f"  {'LIQUID' if liquid else 'dry'}: {question}")
                if liquid:
                    liquid_markets.append(m)
                if len(liquid_markets) >= 10:
                    break

            print(f"3b. FOUND {len(liquid_markets)} LIQUID MARKETS")
            if not liquid_markets:
                print("No liquid markets found — trying all markets directly")
                liquid_markets = markets[:5]

            filtered_markets = self.agent.filter_markets(liquid_markets)
            print(f"4. FILTERED {len(filtered_markets)} MARKETS")
            if not filtered_markets:
                print("No filtered markets, exiting.")
                return

            market = filtered_markets[0]
            best_trade = self.agent.source_best_trade(market)
            print(f"5. CALCULATED TRADE {best_trade}")

            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            trade = self.polymarket.execute_market_order(market, amount)
            print(f"6. TRADED {trade}")

        except Exception as e:
            print(f"Error: {e}")

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
