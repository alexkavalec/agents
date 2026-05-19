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
            filtered_markets = self.agent.filter_markets(markets)
            print(f"4. FILTERED {len(filtered_markets)} MARKETS")
            market = None
            for m in filtered_markets:
                try:
                    token_id = ast.literal_eval(m[0].dict()["metadata"]["clob_token_ids"])[1]
                    self.polymarket.get_orderbook(token_id)
                    market = m
                    print(f"Found liquid market: {m[0].dict()['metadata'].get('question', 'unknown')[:50]}")
                    break
                except Exception as e:
                    print(f"Market not liquid, skipping: {e}")
                    continue
            if market is None:
                print("No liquid markets found, exiting.")
                return
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
