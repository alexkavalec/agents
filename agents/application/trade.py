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

    def get_valid_token_id(self, market):
        try:
            raw = market[0].dict()["metadata"]["clob_token_ids"]
            raw_str = str(raw).strip()
            if raw_str in ("0", "", "None", "null", "[]"):
                return None
            parsed = ast.literal_eval(raw_str)
            if isinstance(parsed, list):
                for t in parsed:
                    t_str = str(t).strip()
                    if t_str not in ("0", "", "None", "null") and len(t_str) > 5:
                        return t_str
                return None
            else:
                t_str = str(parsed).strip()
                if t_str not in ("0", "", "None", "null") and len(t_str) > 5:
                    return t_str
                return None
        except Exception:
            return None

    def is_market_liquid(self, market) -> bool:
        token_id = self.get_valid_token_id(market)
        if not token_id:
            return False
        try:
            ob = self.polymarket.get_orderbook(token_id)
            return ob is not None
        except Exception:
            return False

    def one_best_trade(self) -> None:
        events = []
        try:
            self.pre_trade_logic()
            events = self.polymarket.get_all_tradeable_events()
            print(f"1. FOUND {len(events)} EVENTS")
        except Exception as e:
            print(f"Error fetching events: {e}")
            return

        if len(events) == 0:
            print("No tradeable events found, exiting.")
            return

        try:
            filtered_events = self.agent.filter_events_with_rag(events)
            print(f"2. FILTERED {len(filtered_events)} EVENTS")
        except Exception as e:
            print(f"Error filtering events: {e}")
            return

        try:
            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            print(f"3. FOUND {len(markets)} MARKETS")
        except Exception as e:
            print(f"Error mapping markets: {e}")
            return

        if not markets:
            print("No markets found, exiting.")
            return

        print("Checking liquidity...")
        liquid_markets = []
        for m in markets:
            try:
                token_id = self.get_valid_token_id(m)
                question = ""
                try:
                    question = m[0].dict()["metadata"].get("question", "")[:50]
                except:
                    pass
                if not token_id:
                    print(f"  SKIP (no valid token): {question}")
                    continue
                liquid = self.is_market_liquid(m)
                print(f"  {'LIQUID' if liquid else 'dry'}: {question}")
                if liquid:
                    liquid_markets.append(m)
                if len(liquid_markets) >= 10:
                    break
            except Exception as e:
                print(f"  ERROR checking market: {e}")
                continue

        print(f"3b. FOUND {len(liquid_markets)} LIQUID MARKETS")

        if not liquid_markets:
            print("No liquid markets found, using fallback")
            for m in markets[:20]:
                try:
                    if self.get_valid_token_id(m):
                        liquid_markets.append(m)
                    if len(liquid_markets) >= 5:
                        break
                except:
                    continue

        if not liquid_markets:
            print("No valid markets at all, exiting.")
            return

        try:
            filtered_markets = self.agent.filter_markets(liquid_markets)
            print(f"4. FILTERED {len(filtered_markets)} MARKETS")
        except Exception as e:
            print(f"Error filtering markets: {e}")
            return

        if not filtered_markets:
            print("No filtered markets, exiting.")
            return

        try:
            market = filtered_markets[0]
            best_trade = self.agent.source_best_trade(market)
            print(f"5. CALCULATED TRADE {best_trade}")
        except Exception as e:
            print(f"Error calculating trade: {e}")
            return

        try:
            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            trade = self.polymarket.execute_market_order(market, amount)
            print(f"6. TRADED {trade}")
        except Exception as e:
            print(f"Error executing trade: {e}")

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
