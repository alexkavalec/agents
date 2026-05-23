import os
import json
import ast
import re
import logging
from typing import List, Dict, Any

import math

# Hard market filters — applied before the AI sees any market
MAX_SPREAD = 0.05        # skip markets with bid/ask spread > 5%
MIN_VOLUME = 500         # skip markets with < $500 lifetime volume (illiquid/dead)
MIN_DAYS = 1             # skip markets resolving today or already expired
MAX_DAYS = 40            # skip markets resolving > 40 days out (capital locked too long)

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.connectors.chroma import PolymarketRAG as Chroma
from agents.utils.objects import SimpleEvent, SimpleMarket
from agents.application.prompts import Prompter
from agents.polymarket.polymarket import Polymarket

def retain_keys(data, keys_to_retain):
    if isinstance(data, dict):
        return {
            key: retain_keys(value, keys_to_retain)
            for key, value in data.items()
            if key in keys_to_retain
        }
    elif isinstance(data, list):
        return [retain_keys(item, keys_to_retain) for item in data]
    else:
        return data

class Executor:
    def __init__(self, default_model='gpt-3.5-turbo-16k') -> None:
        load_dotenv()
        # Suppress verbose 404 errors from the CLOB client library
        logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)
        max_token_model = {'gpt-3.5-turbo-16k':15000, 'gpt-4-1106-preview':95000}
        self.token_limit = max_token_model.get(default_model)
        self.prompter = Prompter()
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.llm = ChatOpenAI(
            model=default_model, #gpt-3.5-turbo"
            temperature=0,
        )
        self.gamma = Gamma()
        self.chroma = Chroma()
        self.polymarket = Polymarket()

    def get_llm_response(self, user_input: str) -> str:
        system_message = SystemMessage(content=str(self.prompter.market_analyst()))
        human_message = HumanMessage(content=user_input)
        messages = [system_message, human_message]
        result = self.llm.invoke(messages)
        return result.content

    def get_superforecast(
        self, event_title: str, market_question: str, outcome: str
    ) -> str:
        messages = self.prompter.superforecaster(
            description=event_title, question=market_question, outcome=outcome
        )
        result = self.llm.invoke(messages)
        return result.content


    def estimate_tokens(self, text: str) -> int:
        # This is a rough estimate. For more accurate results, consider using a tokenizer.
        return len(text) // 4  # Assuming average of 4 characters per token

    def process_data_chunk(self, data1: List[Dict[Any, Any]], data2: List[Dict[Any, Any]], user_input: str) -> str:
        system_message = SystemMessage(
            content=str(self.prompter.prompts_polymarket(data1=data1, data2=data2))
        )
        human_message = HumanMessage(content=user_input)
        messages = [system_message, human_message]
        result = self.llm.invoke(messages)
        return result.content


    def divide_list(self, original_list, i):
        # Calculate the size of each sublist
        sublist_size = math.ceil(len(original_list) / i)
        
        # Use list comprehension to create sublists
        return [original_list[j:j+sublist_size] for j in range(0, len(original_list), sublist_size)]
    
    def get_polymarket_llm(self, user_input: str) -> str:
        data1 = self.gamma.get_current_events()
        data2 = self.gamma.get_current_markets()
        
        combined_data = str(self.prompter.prompts_polymarket(data1=data1, data2=data2))
        
        # Estimate total tokens
        total_tokens = self.estimate_tokens(combined_data)
        
        # Set a token limit (adjust as needed, leaving room for system and user messages)
        token_limit = self.token_limit
        if total_tokens <= token_limit:
            # If within limit, process normally
            return self.process_data_chunk(data1, data2, user_input)
        else:
            # If exceeding limit, process in chunks
            chunk_size = len(combined_data) // ((total_tokens // token_limit) + 1)
            print(f'total tokens {total_tokens} exceeding llm capacity, now will split and answer')
            group_size = (total_tokens // token_limit) + 1 # 3 is safe factor
            keys_no_meaning = ['image','pagerDutyNotificationEnabled','resolvedBy','endDate','clobTokenIds','negRiskMarketID','conditionId','updatedAt','startDate']
            useful_keys = ['id','questionID','description','liquidity','clobTokenIds','outcomes','outcomePrices','volume','startDate','endDate','question','questionID','events']
            data1 = retain_keys(data1, useful_keys)
            cut_1 = self.divide_list(data1, group_size)
            cut_2 = self.divide_list(data2, group_size)
            cut_data_12 = zip(cut_1, cut_2)

            results = []

            for cut_data in cut_data_12:
                sub_data1 = cut_data[0]
                sub_data2 = cut_data[1]
                sub_tokens = self.estimate_tokens(str(self.prompter.prompts_polymarket(data1=sub_data1, data2=sub_data2)))

                result = self.process_data_chunk(sub_data1, sub_data2, user_input)
                results.append(result)
            
            combined_result = " ".join(results)
            
        
            
            return combined_result
    def filter_events(self, events: "list[SimpleEvent]") -> str:
        prompt = self.prompter.filter_events(events)
        result = self.llm.invoke(prompt)
        return result.content

    def filter_events_with_rag(self, events: "list[SimpleEvent]") -> list:
        """Select diverse tradeable events via direct LLM call.
        Replaces Chroma RAG which returned only 4 results with a sports bias."""
        from langchain_core.documents import Document

        summaries = []
        for i, e in enumerate(events):
            try:
                d = e.dict() if hasattr(e, "dict") else {}
                summaries.append({
                    "index": i,
                    "title": d.get("title", ""),
                    "description": (d.get("description") or "")[:200],
                })
            except Exception:
                summaries.append({"index": i, "title": "", "description": ""})

        slim = json.dumps(summaries, indent=2)
        prompt = self.prompter.filter_events_diverse()
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=(
                f"Events:\n{slim}\n\n"
                "Return ONLY a JSON array of the top 10 event indexes, diverse across "
                "categories (max 2 from the same category). "
                "Example: [3, 12, 7, 22, 5, 8, 14, 2, 19, 0]"
            )),
        ]
        result = self.llm.invoke(messages)

        selected_indexes = []
        try:
            idxs_raw = re.search(r'\[[\d,\s]+\]', result.content)
            if idxs_raw:
                selected_indexes = json.loads(idxs_raw.group(0))
        except Exception:
            pass

        if not selected_indexes:
            selected_indexes = list(range(min(10, len(events))))

        selected = []
        for idx in selected_indexes[:10]:
            if 0 <= idx < len(events):
                e = events[idx]
                try:
                    d = e.dict() if hasattr(e, "dict") else {}
                    doc = Document(
                        page_content=d.get("description", "") or d.get("title", ""),
                        metadata={
                            "id": d.get("id", ""),
                            "markets": d.get("markets", ""),
                        },
                    )
                    selected.append((doc, 1.0))
                except Exception:
                    pass
        return selected

    def _safe_parse_list(self, value) -> list:
        """Robustly convert any Gamma/Chroma price/id field to a Python list."""
        if isinstance(value, list):
            return value
        if value is None:
            return []
        s = str(value).strip()
        if s in ('None', 'null', '', '[]'):
            return []
        # Try JSON first (Gamma returns JSON-encoded strings)
        try:
            result = json.loads(s)
            return result if isinstance(result, list) else []
        except Exception:
            pass
        # Fall back to ast.literal_eval for Python repr strings
        try:
            result = ast.literal_eval(s)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def _has_usable_prices(self, market_dict: dict) -> bool:
        op = self._safe_parse_list(market_dict.get("outcome_prices"))
        if not op:
            return False
        try:
            return not all(float(p) in (0.0, 1.0) for p in op)
        except Exception:
            return False

    def _enrich_with_live_prices(self, market_dict: dict) -> dict:
        """Replace Gamma placeholder outcome_prices with live CLOB prices."""
        try:
            op = self._safe_parse_list(market_dict.get("outcome_prices"))
            # Gamma returns ["0","1"] or null when there's no real AMM price data.
            # Treat any entry that is exactly 0.0 or 1.0 as a placeholder.
            if op and not any(float(p) in (0.0, 1.0) for p in op):
                return market_dict  # prices look real, skip enrichment

            clob_ids = self._safe_parse_list(market_dict.get("clob_token_ids"))
            if not clob_ids:
                return market_dict

            live_prices = []
            for token_id in clob_ids:
                try:
                    price = self.polymarket.get_midpoint_price(str(token_id))
                except Exception:
                    try:
                        price = self.polymarket.get_orderbook_price(str(token_id))
                    except Exception:
                        continue
                live_prices.append(str(round(price, 4)))

            if live_prices and len(live_prices) == len(clob_ids):
                market_dict["outcome_prices"] = str(live_prices)
                print(f"  Enriched outcome_prices from CLOB: {live_prices}")
            elif live_prices:
                # Partial enrichment — use what we got
                market_dict["outcome_prices"] = str(live_prices)
                print(f"  Partial CLOB enrichment: {live_prices} (got {len(live_prices)}/{len(clob_ids)})")
        except Exception as e:
            print(f"Live price enrichment failed: {e}")
        return market_dict

    def _passes_hard_filters(self, m: dict) -> tuple:
        """Return (passes: bool, reason: str). Checks spread, volume, days to resolution."""
        spread = m.get("spread", 0) or 0
        if spread > MAX_SPREAD:
            return False, f"spread {spread:.3f} > {MAX_SPREAD}"
        volume = m.get("volume", 0) or 0
        if volume < MIN_VOLUME:
            return False, f"volume ${volume:.0f} < ${MIN_VOLUME}"
        days = m.get("days_to_resolution")
        if days is not None:
            if days < MIN_DAYS:
                return False, f"resolves in {days}d (too soon)"
            if days > MAX_DAYS:
                return False, f"resolves in {days}d > {MAX_DAYS}d limit"
        # Skip markets where any outcome token is below CLOB minimum price (0.01)
        # — these are near-certain markets with no real edge and orders will be rejected
        op = self._safe_parse_list(m.get("outcome_prices"))
        if op:
            try:
                if any(float(p) < 0.01 or float(p) > 0.99 for p in op):
                    return False, f"token price out of CLOB range [0.01, 0.99]"
            except (ValueError, TypeError):
                pass
        return True, ""

    def map_filtered_events_to_markets(
        self, filtered_events: "list[SimpleEvent]"
    ) -> "list[SimpleMarket]":
        markets = []
        skipped_prices = 0
        skipped_filters: dict = {}
        for e in filtered_events:
            data = json.loads(e[0].json())
            market_ids = data["metadata"]["markets"].split(",")
            for market_id in market_ids:
                market_data = self.gamma.get_market(market_id)
                formatted_market_data = self.polymarket.map_api_to_market(market_data)
                formatted_market_data = self._enrich_with_live_prices(formatted_market_data)
                if not self._has_usable_prices(formatted_market_data):
                    skipped_prices += 1
                    continue
                if not formatted_market_data.get("question", ""):
                    skipped_filters["no question"] = skipped_filters.get("no question", 0) + 1
                    continue
                passes, reason = self._passes_hard_filters(formatted_market_data)
                if not passes:
                    skipped_filters[reason] = skipped_filters.get(reason, 0) + 1
                    continue
                markets.append(formatted_market_data)
        if skipped_prices:
            print(f"  Skipped {skipped_prices} markets with no usable prices")
        if skipped_filters:
            for reason, count in skipped_filters.items():
                print(f"  Skipped {count} markets: {reason}")
        print(f"  {len(markets)} markets with question + usable prices passed all filters")
        return markets

    def filter_markets(self, markets: list, open_positions: list = None) -> "list[tuple]":
        """Rank markets by passing structured data directly to the LLM.
        Bypasses Chroma to avoid metadata loss on retrieval."""
        from langchain_core.documents import Document
        prompt = self.prompter.filter_markets()

        slim = json.dumps([{
            "id": m.get("id"),
            "question": m.get("question", ""),
            "spread": m.get("spread"),
            "volume": m.get("volume"),
            "days_to_resolution": m.get("days_to_resolution"),
            "outcome_prices": m.get("outcome_prices"),
        } for m in markets], indent=2)

        holdings_block = ""
        if open_positions:
            holdings_block = (
                "\nIMPORTANT — you already hold positions in these markets. "
                "Do NOT select any market that is economically equivalent "
                "(e.g. the other side of the same event, or the same event with a different team):\n"
                + "".join(f"  - {q}\n" for q in open_positions if q)
                + "\n"
            )

        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=(
                f"Markets:\n{slim}\n\n"
                + holdings_block
                + "Return ONLY a JSON array of the top 4 market IDs ranked best first. "
                "Example: [12345, 67890]"
            )),
        ]
        result = self.llm.invoke(messages)

        selected_ids = []
        try:
            ids_raw = re.search(r'\[[\d,\s]+\]', result.content)
            if ids_raw:
                selected_ids = json.loads(ids_raw.group(0))
        except Exception:
            pass

        if not selected_ids:
            markets_sorted = sorted(markets, key=lambda m: m.get("volume", 0) or 0, reverse=True)
            selected_ids = [m.get("id") for m in markets_sorted[:4]]

        markets_by_id = {m.get("id"): m for m in markets}
        docs = []
        for mid in selected_ids:
            m = markets_by_id.get(mid)
            if m:
                doc = Document(
                    page_content=m.get("description", "") or m.get("question", ""),
                    metadata=m,
                )
                docs.append((doc, 1.0))
        return docs[:4]

    def source_best_trade(self, market_object, whale_signals: list = None) -> str:
        market_document = market_object[0].dict()
        market = market_document["metadata"]
        outcome_prices = self._safe_parse_list(market.get("outcome_prices"))
        outcomes = self._safe_parse_list(market.get("outcomes"))
        question = market.get("question", "")
        if not question:
            print("  Market metadata missing 'question' — skipping.")
            return None
        description = market_document["page_content"]

        from agents.memory.trade_log import get_recent_lessons
        from agents.connectors.data_enricher import DataEnricher
        from agents.connectors.whale_tracker import WhaleTracker
        lessons = get_recent_lessons(5)
        live_context = DataEnricher().get_context(question, whale_signals=whale_signals)

        # Append direct holder snapshot for this specific market (post-selection signal)
        condition_id = market.get("condition_id", "")
        if condition_id:
            try:
                holder_ctx = WhaleTracker().get_market_holders(condition_id)
                if holder_ctx:
                    live_context = (live_context or "") + holder_ctx
            except Exception as _wt_err:
                print(f"  [WhaleTracker] holders fetch failed (non-fatal): {_wt_err}")

        prompt = self.prompter.superforecaster(question, description, outcomes,
                                               lessons=lessons, live_context=live_context,
                                               market_prices=outcome_prices)
        print()
        print("... prompting ... ", prompt)
        print()
        result = self.llm.invoke(prompt)
        superforecaster_content = result.content

        print("result: ", superforecaster_content)
        print()
        prompt = self.prompter.one_best_trade(superforecaster_content, outcomes, outcome_prices)
        print("... prompting ... ", prompt)
        print()
        result = self.llm.invoke(prompt)
        trade_content = result.content

        print("result: ", trade_content)
        print()

        # If the LLM omitted price:, inject it from the superforecaster estimate so
        # the edge check in trade.py always has a probability to compare against.
        if not re.search(r"price\s*[:=]", trade_content):
            m = re.search(r"likelihood\s*`?([0-9]*\.?[0-9]+)", superforecaster_content)
            if m:
                trade_content = f"price:{m.group(1)}, {trade_content}"

        return trade_content

    def format_trade_prompt_for_execution(self, best_trade: str) -> float:
        data = best_trade.split(",")
        # price = re.findall("\d+\.\d+", data[0])[0]
        size = re.findall("\d+\.\d+", data[1])[0]
        usdc_balance = self.polymarket.get_usdc_balance()
        return float(size) * usdc_balance

    def source_best_market_to_create(self, filtered_markets) -> str:
        prompt = self.prompter.create_new_market(filtered_markets)
        print()
        print("... prompting ... ", prompt)
        print()
        result = self.llm.invoke(prompt)
        content = result.content
        return content
