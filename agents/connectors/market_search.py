"""
Minimal Polymarket market search for the dashboard chat's manual-trade feature —
uses Gamma's public search endpoint (the same one polymarket.com's own search bar
calls), filtered down to markets that are actually open and accepting orders right
now. Read-only; never places an order. See chat.py for how a match becomes a trade
proposal, and trade.py's place_manual_trade() for the actual execution.

Query phrasing matters more than you'd expect: confirmed live that Gamma's search
ranks badly when the query text LEADS with a generic bet-type phrase or bare number
instead of the actual participant names — e.g. querying "Spread: Baltimore Orioles
(-1.5)" (mimicking Polymarket's own market-title format) returned a page of
"Spread: X (-1.5)" markets from entirely unrelated soccer games, not the real
Orioles/Angels one, while "Baltimore Orioles Los Angeles Angels spread -1.5" (team
names first, natural phrasing) found it immediately. is_relevant() correctly
rejects the wrong soccer matches (no team-name overlap), which is why this
surfaces as "couldn't find a market" rather than a wrong-market match — but the
right fix is a better query in the first place, not just filtering harder after
the fact. chat.py's system prompt now explicitly tells the model to always lead
market_query with who's playing.
"""

import re
import json
import requests

GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"


def search_markets(query: str, limit: int = 40, per_event_limit: int = 10) -> list:
    """Return up to `limit` open, order-accepting markets matching the query text,
    ranked by Gamma's own search relevance. Each result:
    {"question", "condition_id", "outcomes": [...], "outcome_prices": [...],
     "token_ids": [...], "volume": float}

    per_event_limit caps how many markets are taken from any single event before
    moving on — confirmed live this matters: a season-long prop event (e.g. "MLB:
    2026 Regular Season Win Totals", 30 markets, one per team) can rank ahead of
    the actual single-game event a query is about, and a flat overall cap alone
    (the old behavior) exhausts the whole budget on that one event's markets
    before ever reaching the real target further down the results. All of this
    data is already in the one search response Gamma returns — capping per event
    only changes what we keep from it, not how much we fetch, so there's no real
    cost to being generous with both numbers.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        r = requests.get(
            GAMMA_SEARCH_URL,
            params={"q": query, "limit_per_type": 15, "events_status": "active"},
            headers={"User-Agent": "PolymarketTradingBot/1.0"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    results = []
    seen_condition_ids = set()
    for event in data.get("events", []) or []:
        taken_this_event = 0
        for m in event.get("markets", []) or []:
            if taken_this_event >= per_event_limit:
                break
            if not m.get("active") or m.get("closed") or not m.get("acceptingOrders", True):
                continue
            condition_id = m.get("conditionId", "")
            if not condition_id or condition_id in seen_condition_ids:
                continue
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = json.loads(m.get("outcomePrices", "[]"))
                token_ids = json.loads(m.get("clobTokenIds", "[]"))
            except Exception:
                continue
            if not outcomes or not (len(outcomes) == len(prices) == len(token_ids)):
                continue
            seen_condition_ids.add(condition_id)
            results.append({
                "question": m.get("question", ""),
                "condition_id": condition_id,
                "outcomes": outcomes,
                "outcome_prices": [float(p) for p in prices],
                "token_ids": token_ids,
                "volume": float(m.get("volume") or 0),
            })
            taken_this_event += 1
            if len(results) >= limit:
                return results
    return results


def _extract_numbers(text: str) -> set:
    """Numbers mentioned in text, normalized so '100k' and '$100,000' compare equal."""
    nums = set()
    for raw in re.findall(r"\$?\d[\d,]*\.?\d*\s?[km]?\b", text.lower()):
        t = raw.replace("$", "").replace(",", "").strip()
        mult = 1.0
        if t.endswith("k"):
            mult, t = 1000.0, t[:-1].strip()
        elif t.endswith("m"):
            mult, t = 1_000_000.0, t[:-1].strip()
        try:
            nums.add(float(t) * mult)
        except ValueError:
            pass
    return nums


def is_relevant(question: str, query: str) -> bool:
    """Best-effort relevance gate against a specific, real failure mode: Gamma's
    search is semantic, not exact — querying "bitcoin above 100k" ranks a
    "Bitcoin above $54,000" market first (same topic, wrong number). If the query
    names a specific number, the candidate's question must mention a matching
    number too. Otherwise falls back to a loose word-overlap check. This is a
    quality filter for which candidates get shown, not the actual safety net —
    the caller must still show the exact question text for the user to verify."""
    query_nums = _extract_numbers(query)
    if query_nums:
        if not (query_nums & _extract_numbers(question)):
            return False
    query_words = set(re.findall(r"[a-z]{4,}", query.lower()))
    if query_words:
        hits = sum(1 for w in query_words if w in question.lower())
        # Short queries (1-2 salient words, e.g. a name) must match in full —
        # partial credit only kicks in for longer, more descriptive queries,
        # otherwise "leader out before 2027" swallows every leader, not just
        # the one actually named. For longer queries, a plain majority is
        # enough rather than "all but one" — confirmed live this matters for
        # per-side markets: a query naming BOTH teams (the phrasing that
        # actually ranks well with Gamma's own search, see search_markets())
        # can't reach "all but one" against a single-team market title like
        # "Spread: Baltimore Orioles (-1.5)", which only ever names the side
        # the line applies to, never both teams at once. The number check
        # above still has to pass independently whenever a number is named,
        # which is most of what carries the precision this filter cares about.
        required = len(query_words) if len(query_words) <= 2 else (len(query_words) + 1) // 2
        return hits >= required
    return True
