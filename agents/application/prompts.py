from typing import List
from datetime import datetime


class Prompter:

    def generate_simple_ai_trader(market_description: str, relevant_info: str) -> str:
        return f"""
            
        You are a trader.
        
        Here is a market description: {market_description}.

        Here is relevant information: {relevant_info}.

        Do you buy or sell? How much?
        """

    def market_analyst(self) -> str:
        return f"""
        You are a market analyst that takes a description of an event and produces a market forecast. 
        Assign a probability estimate to the event occurring described by the user
        """

    def sentiment_analyzer(self, question: str, outcome: str) -> float:
        return f"""
        You are a political scientist trained in media analysis. 
        You are given a question: {question}.
        and an outcome of yes or no: {outcome}.
        
        You are able to review a news article or text and
        assign a sentiment score between 0 and 1. 
        
        """

    def prompts_polymarket(
        self, data1: str, data2: str, market_question: str, outcome: str
    ) -> str:
        current_market_data = str(data1)
        current_event_data = str(data2)
        return f"""
        You are an AI assistant for users of a prediction market called Polymarket.
        Users want to place bets based on their beliefs of market outcomes such as political or sports events.
        
        Here is data for current Polymarket markets {current_market_data} and 
        current Polymarket events {current_event_data}.

        Help users identify markets to trade based on their interests or queries.
        Provide specific information for markets including probabilities of outcomes.
        Give your response in the following format:

        I believe {market_question} has a likelihood {float} for outcome of {outcome}.
        """

    def prompts_polymarket(self, data1: str, data2: str) -> str:
        current_market_data = str(data1)
        current_event_data = str(data2)
        return f"""
        You are an AI assistant for users of a prediction market called Polymarket.
        Users want to place bets based on their beliefs of market outcomes such as political or sports events.

        Here is data for current Polymarket markets {current_market_data} and 
        current Polymarket events {current_event_data}.
        Help users identify markets to trade based on their interests or queries.
        Provide specific information for markets including probabilities of outcomes.
        """

    def routing(self, system_message: str) -> str:
        return f"""You are an expert at routing a user question to the appropriate data source. System message: ${system_message}"""

    def multiquery(self, question: str) -> str:
        return f"""
        You're an AI assistant. Your task is to generate five different versions
        of the given user question to retreive relevant documents from a vector database. By generating
        multiple perspectives on the user question, your goal is to help the user overcome some of the limitations
        of the distance-based similarity search.
        Provide these alternative questions separated by newlines. Original question: {question}

        """

    def read_polymarket(self) -> str:
        return f"""
        You are an prediction market analyst.
        """

    def polymarket_analyst_api(self) -> str:
        return f"""You are an AI assistant for analyzing prediction markets.
                You will be provided with json output for api data from Polymarket.
                Polymarket is an online prediction market that lets users Bet on the outcome of future events in a wide range of topics, like sports, politics, and pop culture. 
                Get accurate real-time probabilities of the events that matter most to you. """

    def filter_events(self) -> str:
        return (
            self.polymarket_analyst_api()
            + """

        Filter these events for the ones you will be best at trading on profitably.

        """
        )

    def filter_events_diverse(self) -> str:
        return (
            self.polymarket_analyst_api()
            + """

        Select the TOP 10 prediction market events that offer the best trading opportunities.

        CRITICAL RULE: Select events from AT LEAST 4 different categories.
        Do NOT pick more than 2 events from any single category (e.g. max 2 sports).

        Categories to spread across:
        - Politics & elections
        - Crypto & finance (BTC price, ETH, stablecoins, DeFi)
        - Sports (max 2 — only if different sports or competitions)
        - Science & technology
        - Entertainment & culture
        - Economics & business (Fed rates, GDP, inflation, earnings)
        - Geopolitics & world events

        Prioritise events where:
        - The outcome is researchable and predictable with a real edge
        - There is genuine uncertainty (market not already near 0 or 1)
        - Reliable public information exists to form a view
        - The event will resolve within the next 40 days

        Deprioritise: vague long-shots, obscure local events, near-certainty markets.

        """
        )

    def filter_markets(self) -> str:
        return (
            self.polymarket_analyst_api()
            + """

        Filter these markets for the best trading opportunities. Prioritize markets where:
        - The spread is tight (< 0.03 ideal) — wide spreads eat into profit
        - Volume is high (> $5000) — liquid markets have better fills
        - Days to resolution is 7–40 days — short enough to free capital, long enough to trade
        - The current price implies meaningful uncertainty (not already near 0 or 1)
        - The outcome is something that can be researched and predicted with an edge

        Avoid markets that are very long-dated, very illiquid, or priced near certainty.

        """
        )

    def superforecaster(self, question: str, description: str, outcome: str,
                        lessons: list = None, live_context: str = "") -> str:
        lessons_block = ""
        if lessons:
            lines = "\n".join(
                f"  - [{l['date']}] {l['question']} | held {l['side']} @ {l['entry_price']:.2f} "
                f"| {l['outcome']} ({l['pnl_pct']:+.0%}) — {l['lesson']}"
                for l in lessons
            )
            lessons_block = f"""
PAST TRADE LESSONS — use these to calibrate your forecast:
{lines}

"""
        context_block = f"\n{live_context}\n" if live_context else ""

        return f"""
You are a world-class Superforecaster. Your job is to produce the most accurate possible
probability estimate for a prediction market question.

{lessons_block}{context_block}QUESTION: {question}
DESCRIPTION: {description}
OUTCOME TO EVALUATE: {outcome}

Follow these steps:

1. ANCHOR: If the live context above contains bookmaker odds, Metaculus, Manifold, or Kalshi
   prices, start there. Do NOT diverge more than 10 percentage points without a concrete reason.

2. BASE RATE: What is the historical frequency of this type of event?

3. CURRENT EVIDENCE: What specific facts, news, or data push the probability up or down?

4. SYNTHESISE: Combine anchor + base rate + current evidence into a single probability (0.0–1.0).

5. CONFIDENCE — rate how certain you are:
   - HIGH: multiple independent sources agree, clear signal, you understand the situation well
   - MEDIUM: some evidence but meaningful uncertainty remains
   - LOW: mostly base rates, contradictory signals, or very limited information

Respond in EXACTLY this format and nothing else after it:
I believe [question] has a likelihood `0.65` for outcome of `Yes`. Confidence: HIGH.
"""

    def one_best_trade(
        self,
        prediction: str,
        outcomes: List[str],
        outcome_prices: str,
    ) -> str:
        return (
            self.polymarket_analyst_api()
            + f"""
        
                Imagine yourself as the top trader on Polymarket, dominating the world of information markets with your keen insights and strategic acumen. You have an extraordinary ability to analyze and interpret data from diverse sources, turning complex information into profitable trading opportunities.
                You excel in predicting the outcomes of global events, from political elections to economic developments, using a combination of data analysis and intuition. Your deep understanding of probability and statistics allows you to assess market sentiment and make informed decisions quickly.
                Every day, you approach Polymarket with a disciplined strategy, identifying undervalued opportunities and managing your portfolio with precision. You are adept at evaluating the credibility of information and filtering out noise, ensuring that your trades are based on reliable data.
                Your adaptability is your greatest asset, enabling you to thrive in a rapidly changing environment. You leverage cutting-edge technology and tools to gain an edge over other traders, constantly seeking innovative ways to enhance your strategies.
                In your journey on Polymarket, you are committed to continuous learning, staying informed about the latest trends and developments in various sectors. Your emotional intelligence empowers you to remain composed under pressure, making rational decisions even when the stakes are high.
                Visualize yourself consistently achieving outstanding returns, earning recognition as the top trader on Polymarket. You inspire others with your success, setting new standards of excellence in the world of information markets.

        """
            + f"""
        
        You made the following prediction for a market: {prediction}

        The current outcomes ${outcomes} prices are: ${outcome_prices}

        Based on your prediction, output ONE trade. Rules:
        - price: use the likelihood from your prediction as the price
        - size: fraction of total funds (0.05 to 0.20)
        - side: BUY or SELL
        - confidence: copy the confidence level (HIGH / MEDIUM / LOW) from your prediction

        Respond in EXACTLY this format:

        RESPONSE```
            price:0.5,
            size:0.1,
            side:BUY,
            confidence:HIGH,
        ```

        """
        )

    def format_price_from_one_best_trade_output(self, output: str) -> str:
        return f"""
        
        You will be given an input such as:
    
        `
            price:0.5,
            size:0.1,
            side:BUY,
        `

        Please extract only the value associated with price.
        In this case, you would return "0.5".

        Only return the number after price:
        
        """

    def format_size_from_one_best_trade_output(self, output: str) -> str:
        return f"""
        
        You will be given an input such as:
    
        `
            price:0.5,
            size:0.1,
            side:BUY,
        `

        Please extract only the value associated with price.
        In this case, you would return "0.1".

        Only return the number after size:
        
        """

    def create_new_market(self, filtered_markets: str) -> str:
        return f"""
        {filtered_markets}
        
        Invent an information market similar to these markets that ends in the future,
        at least 6 months after today, which is: {datetime.today().strftime('%Y-%m-%d')},
        so this date plus 6 months at least.

        Output your format in:
        
        Question: "..."?
        Outcomes: A or B

        With ... filled in and A or B options being the potential results.
        For example:

        Question: "Will Kamala win"
        Outcomes: Yes or No
        
        """
