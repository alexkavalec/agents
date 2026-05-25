"""
Run this on Railway to verify all data sources are working:
  python test_sources.py
"""
import os
import sys
import datetime
import requests

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⏭  SKIP"

def test(name, fn, required_key=None):
    if required_key and not os.getenv(required_key):
        print(f"{SKIP}  {name} — {required_key} not set")
        return
    try:
        result = fn()
        if result:
            print(f"{PASS}  {name}")
            if isinstance(result, list) and result:
                print(f"         → {len(result)} result(s). First: {str(result[0])[:120]}")
            elif isinstance(result, dict):
                print(f"         → {str(result)[:120]}")
            else:
                print(f"         → {str(result)[:120]}")
        else:
            print(f"{FAIL}  {name} — returned empty (key set but no data returned)")
    except Exception as e:
        print(f"{FAIL}  {name} — {e}")


# ── No-key sources ────────────────────────────────────────────────────────

def check_metaculus():
    r = requests.get(
        "https://www.metaculus.com/api2/questions/",
        params={"search": "bitcoin", "status": "open", "limit": 2},
        headers={"Accept": "application/json"}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    results = r.json().get("results", [])
    return [{"title": q["title"],
             "p_yes": (q.get("community_prediction") or {}).get("full", {}).get("q2")}
            for q in results if q.get("title")]

def check_manifold():
    r = requests.get(
        "https://api.manifold.markets/v0/search-markets",
        params={"term": "bitcoin", "limit": 3, "filter": "open"},
        headers={"Accept": "application/json"}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    return [{"question": m["question"], "probability": round(float(m["probability"]), 3)}
            for m in r.json() if m.get("probability")]

def check_kalshi():
    r = requests.get(
        "https://trading-api.kalshi.com/trade-api/v2/markets",
        params={"limit": 5, "status": "open", "keyword": "bitcoin"},
        headers={"Accept": "application/json"}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    return [{"title": m.get("title", ""), "yes_ask": m.get("yes_ask")}
            for m in r.json().get("markets", [])[:3] if m.get("title")]

def check_reddit():
    r = requests.get(
        "https://www.reddit.com/search.json",
        params={"q": "bitcoin prediction", "sort": "new", "limit": 3, "t": "week"},
        headers={"User-Agent": "PolymarketTradingBot/1.0"}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    return [p["data"]["title"] for p in r.json()["data"]["children"]]

def check_wikipedia():
    r = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "opensearch", "search": "bitcoin", "limit": 1, "format": "json"},
        timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    title = r.json()[1][0].replace(" ", "_")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=7)
    pv = requests.get(
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
        f"/en.wikipedia/all-access/all-agents/{requests.utils.quote(title)}"
        f"/daily/{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}",
        timeout=10,
    )
    views = sum(i["views"] for i in pv.json().get("items", []))
    return {"article": title, "views_7d": views}

def check_espn():
    r = requests.get(
        "http://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    events = r.json().get("events", [])
    return [e["name"] for e in events[:3]] or ["no games today — API is live"]

def check_polymarket_trades():
    r = requests.get(
        "https://data-api.polymarket.com/trades",
        params={"limit": 3, "takerOnly": "false"},
        headers={"User-Agent": "PolymarketTradingBot/1.0"}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    data = r.json()
    assert isinstance(data, list) and data, "empty response"
    return [{"wallet": t.get("proxyWallet", "")[:12], "size": t.get("size")} for t in data[:3]]

def check_polymarket_holders():
    # fetch a live market conditionId first
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "limit": 1},
        headers={"User-Agent": "PolymarketTradingBot/1.0"}, timeout=10,
    )
    assert r.status_code == 200, f"Gamma HTTP {r.status_code}"
    markets = r.json()
    assert markets, "no markets returned"
    cid = markets[0].get("conditionId", "")
    assert cid, "no conditionId in first market"
    r2 = requests.get(
        "https://data-api.polymarket.com/holders",
        params={"market": cid, "limit": 5},
        headers={"User-Agent": "PolymarketTradingBot/1.0"}, timeout=10,
    )
    assert r2.status_code == 200, f"holders HTTP {r2.status_code}"
    return {"condition_id": cid[:20] + "...", "holders_sample": str(r2.json())[:100]}


# ── Key-required sources ──────────────────────────────────────────────────

def check_tavily():
    from tavily import TavilyClient
    client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    resp = client.search(
        "Will Bitcoin exceed 100k in 2025?", max_results=2,
        search_depth="basic", exclude_domains=["polymarket.com"],
    )
    results = resp.get("results", [])
    assert results, "no results"
    return [{"title": r["title"], "score": r.get("score")} for r in results]

def check_newsapi():
    key = os.getenv("NEWSAPI_API_KEY") or os.getenv("NEWS_API_KEY")
    r = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": "bitcoin prediction 2025", "language": "en",
            "sortBy": "publishedAt", "pageSize": 2,
            "from": (datetime.date.today() - datetime.timedelta(days=3)).isoformat(),
        },
        headers={"X-Api-Key": key}, timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:100]}"
    articles = r.json().get("articles", [])
    assert articles, "no articles returned"
    return [{"title": a["title"], "published": a.get("publishedAt", "")[:10]} for a in articles]

def check_twitter():
    r = requests.get(
        "https://api.twitter.com/2/tweets/search/recent",
        params={
            "query": "bitcoin prediction -is:retweet -is:reply lang:en",
            "max_results": 5,
            "tweet.fields": "public_metrics",
        },
        headers={"Authorization": f"Bearer {os.getenv('TWITTER_BEARER_TOKEN')}"},
        timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json().get("data", [])
    assert data, "no tweets returned"
    return [{"text": t["text"][:80], "likes": t.get("public_metrics", {}).get("like_count", 0)}
            for t in data[:3]]

def check_api_sports():
    # Test football (Premier League) standings — basic API health check
    r = requests.get(
        "https://v3.football.api-sports.io/standings",
        params={"league": 39, "season": 2024},  # Premier League 2024/25
        headers={"x-apisports-key": os.getenv("API_SPORTS_KEY")},
        timeout=10,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    data = r.json()
    errors = data.get("errors", {})
    assert not errors, f"API errors: {errors}"
    standings = data.get("response", [])
    assert standings, "no standings data"
    entries = standings[0].get("league", {}).get("standings", [[]])[0]
    return [{"team": e.get("team", {}).get("name"), "pts": e.get("points")} for e in entries[:5]]


# ── Google Trends (unreliable on cloud) ───────────────────────────────────

def check_google_trends():
    from pytrends.request import TrendReq
    pt = TrendReq(hl="en-US", tz=0, timeout=(8, 8))
    pt.build_payload(["bitcoin"], timeframe="now 7-d")
    df = pt.interest_over_time()
    assert not df.empty, "empty dataframe"
    return {"avg_interest": int(df["bitcoin"].mean()), "current": int(df["bitcoin"].iloc[-1])}


# ── Run all ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 65)
    print("POLYMARKET BOT — DATA SOURCE LIVE TEST")
    print(f"Running on: Railway / {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 65)

    print("\n── No-key sources ──────────────────────────────────────────")
    test("Metaculus",             check_metaculus)
    test("Manifold",              check_manifold)
    test("Kalshi (no key)",       check_kalshi)
    test("Reddit",                check_reddit)
    test("Wikipedia pageviews",   check_wikipedia)
    test("ESPN",                  check_espn)
    test("Polymarket /trades",    check_polymarket_trades)
    test("Polymarket /holders",   check_polymarket_holders)

    print("\n── API-key sources ─────────────────────────────────────────")
    test("Tavily",       check_tavily,     required_key="TAVILY_API_KEY")
    test("NewsAPI",      check_newsapi,    required_key="NEWSAPI_API_KEY")
    test("Twitter/X",    check_twitter,    required_key="TWITTER_BEARER_TOKEN")
    test("API-Sports",   check_api_sports, required_key="API_SPORTS_KEY")

    print("\n── Unreliable on cloud ──────────────────────────────────────")
    test("Google Trends", check_google_trends)

    print()
