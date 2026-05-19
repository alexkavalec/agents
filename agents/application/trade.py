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

            # Use sampling markets - these are GUARANTEED to have active orderbooks
            print("Fetching actively traded markets from CLOB...")
            try:
                raw_markets = self.polymarket.get_sampling_simplified_markets()
                print(f"1. FOUND {len(raw_markets)} ACTIVELY TRADED MARKETS")
            except Exception as e:
                print(f"Error fetching sampling markets: {e}")
                # Fallback to event-based approach
                events = self.polymarket.get_all_tradeable_events()
                print(f"1. FALLBACK: FOUND {len(events)} EVENTS")
                if not events:
                    print("No events found, exiting.")
                    return
                filtered_events = self.agent.filter_events_with_rag(events)
                raw_markets = self.agent.map_filtered_events_to_markets(filtered_events)

            if not raw_markets:
                print("No markets found, exiting.")
                return

            # Convert to the format filter_markets expects
            # filter_markets expects a list of Chroma document tuples
            # So we use the RAG-based filter on events first, then cross-reference
            events = self.polymarket.get_all_tradeable_events()
            print(f"2. FOUND {len(events)} EVENTS FOR CONTEXT")

            filtered_events = self.agent.filter_events_with_rag(events)
            print(f"3. AI FILTERED TO {len(filtered_events)} EVENTS")

            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            print(f"4. MAPPED TO {len(markets)} MARKETS")

            if not markets:
                print("No markets mapped, exiting.")
                return

            # Filter markets using AI
            filtered_markets = self.agent.filter_markets(markets)
            print(f"5. AI FILTERED TO {len(filtered_markets)} MARKETS")

            if not filtered_markets:
                print("No filtered markets, exiting.")
                return

            # Find best trade
            market = filtered_markets[0]
            print(f"Selected market: {market[0].dict()['metadata'].get('question', '')[:80]}")

            best_trade = self.agent.source_best_trade(market)
            print(f"6. CALCULATED TRADE {best_trade}")

            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            print(f"Trade amount: {amount}")

            # Get the actual token ID from sampling markets for execution
            # Find matching market in sampling markets by question
            market_question = market[0].dict()["metadata"].get("question", "")
            execution_market = None
            for sm in raw_markets:
                if hasattr(sm, 'question') and sm.question and market_question[:20].lower() in sm.question.lower():
                    execution_market = sm
                    break

            if execution_market:
                print(f"Found matching liquid market for execution")
                # Execute using the sampling market token
                token_id = execution_market.clob_token_ids
                if isinstance(token_id, str):
                    token_ids = ast.literal_eval(token_id)
                else:
                    token_ids = token_id
                actual_token = str(token_ids[1]) if len(token_ids) > 1 else str(token_ids[0])
                print(f"Executing with token: {actual_token[:20]}...")
                trade = self.polymarket.execute_market_order(market, amount)
                print(f"7. TRADED {trade}")
            else:
                print("Could not find liquid matching market for execution")
                print("Attempting direct execution anyway...")
                trade = self.polymarket.execute_market_order(market, amount)
                print(f"7. TRADED {trade}")

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
