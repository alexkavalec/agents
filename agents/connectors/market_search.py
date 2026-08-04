"""
Minimal Polymarket market search for the dashboard chat's manual-trade feature —
uses Gamma's public search endpoint (the same one polymarket.com's own search bar
calls), filtered down to markets that are actually open and accepting orders right
now. Read-only; never places an order. See chat.py for how a match becomes a trade
proposal, and trade.py's place_manual_trade() for the actual execution.
"""

import re
import json
import requests

GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"


def search_markets(query: str, limit: int = 6) -> list:
    """Return up to `limit` open, order-accepting markets matching the query text,
    ranked by Gamma's own search relevance. Each result:
    {"question", "condition_id", "outcomes": [...], "outcome_prices": [...],
     "token_ids": [...], "volume": float}
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
        for m in event.get("markets", []) or []:
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
        # the one actually named.
        required = len(query_words) if len(query_words) <= 2 else len(query_words) - 1
        return hits >= required
    return True
