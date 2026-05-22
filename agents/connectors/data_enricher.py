"""
DataEnricher — fetches live context for a market question from multiple sources
and returns a formatted block injected into the superforecaster prompt.

Sources:
  - NewsAPI          (requires NEWS_API_KEY env var)
  - Twitter/X        (requires TWITTER_BEARER_TOKEN env var)
  - Tavily           (requires TAVILY_API_KEY env var — real-time web search)
  - ESPN             (unofficial API, no key needed — live sports scores/standings)
  - Reddit           (public JSON API, no key needed)
  - Wikipedia        (public REST API, no key needed)
  - Metaculus        (public API, no key needed)
  - Google Trends    (pytrends, no key needed — may be unreliable on cloud IPs)

Add new sources by implementing a _get_<source>() method and calling it in get_context().
"""

import os
import re
import datetime
import requests

STOP_WORDS = {
    "will", "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "would", "could",
    "should", "may", "might", "shall", "can", "this", "that", "these",
    "those", "what", "which", "who", "whom", "whose", "when", "where",
    "why", "how", "win", "lose", "happen", "occur", "become", "get",
    "in", "on", "at", "by", "for", "with", "about", "against", "between",
    "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "out", "off", "over", "under", "again", "than", "then",
    "once", "2025", "2026", "2027",
}


def _extract_keywords(question: str, max_words: int = 5) -> str:
    words = re.sub(r"[^\w\s]", "", question).split()
    keywords = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:max_words])


class DataEnricher:
    # ESPN league slugs keyed by sport keywords
    ESPN_LEAGUES = {
        "champions league": "soccer/uefa.champions",
        "ucl": "soccer/uefa.champions",
        "uefa": "soccer/uefa.champions",
        "premier league": "soccer/eng.1",
        "la liga": "soccer/esp.1",
        "bundesliga": "soccer/ger.1",
        "serie a": "soccer/ita.1",
        "ligue 1": "soccer/fra.1",
        "nba": "basketball/nba",
        "nfl": "football/nfl",
        "super bowl": "football/nfl",
        "mlb": "baseball/mlb",
        "nhl": "hockey/nhl",
        "world cup": "soccer/fifa.world",
        "euro": "soccer/uefa.euro",
    }

    def __init__(self):
        self.news_api_key = os.getenv("NEWS_API_KEY") or os.getenv("NEWSAPI_API_KEY")
        self.twitter_bearer = os.getenv("TWITTER_BEARER_TOKEN")
        self.tavily_key = os.getenv("TAVILY_API_KEY")

    # ------------------------------------------------------------------
    # NewsAPI
    # ------------------------------------------------------------------

    def _get_news(self, query: str, max_articles: int = 5) -> list:
        if not self.news_api_key:
            return []
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": max_articles,
                    "from": (datetime.date.today() - datetime.timedelta(days=7)).isoformat(),
                },
                headers={"X-Api-Key": self.news_api_key},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            articles = resp.json().get("articles", [])
            return [
                {
                    "source": "NewsAPI",
                    "title": a.get("title", ""),
                    "snippet": (a.get("description") or "")[:200],
                    "published": (a.get("publishedAt") or "")[:10],
                }
                for a in articles if a.get("title")
            ]
        except Exception as e:
            print(f"  [DataEnricher] NewsAPI error: {e}")
            return []

    # ------------------------------------------------------------------
    # Twitter / X
    # ------------------------------------------------------------------

    def _get_tweets(self, query: str, max_tweets: int = 8) -> list:
        if not self.twitter_bearer:
            return []
        try:
            search_query = f"{query} -is:retweet -is:reply lang:en"
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query": search_query,
                    "max_results": max_tweets,
                    "tweet.fields": "created_at,public_metrics",
                },
                headers={"Authorization": f"Bearer {self.twitter_bearer}"},
                timeout=8,
            )
            if resp.status_code != 200:
                print(f"  [DataEnricher] Twitter {resp.status_code}: {resp.text[:120]}")
                return []
            return [
                {
                    "source": "Twitter/X",
                    "text": t.get("text", "")[:280],
                    "engagement": (t.get("public_metrics", {}).get("like_count", 0)
                                  + t.get("public_metrics", {}).get("retweet_count", 0)),
                }
                for t in resp.json().get("data", [])
            ]
        except Exception as e:
            print(f"  [DataEnricher] Twitter error: {e}")
            return []

    # ------------------------------------------------------------------
    # Reddit (public JSON API — no key required)
    # ------------------------------------------------------------------

    def _get_reddit(self, query: str, max_posts: int = 5) -> list:
        try:
            resp = requests.get(
                "https://www.reddit.com/search.json",
                params={"q": query, "sort": "new", "limit": max_posts, "t": "week"},
                headers={"User-Agent": "PolymarketTradingBot/1.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            posts = resp.json().get("data", {}).get("children", [])
            results = []
            for p in posts:
                d = p.get("data", {})
                results.append({
                    "source": "Reddit",
                    "subreddit": d.get("subreddit", ""),
                    "title": d.get("title", ""),
                    "score": d.get("score", 0),
                    "comments": d.get("num_comments", 0),
                })
            return results
        except Exception as e:
            print(f"  [DataEnricher] Reddit error: {e}")
            return []

    # ------------------------------------------------------------------
    # Wikipedia Pageviews (public REST API — no key required)
    # ------------------------------------------------------------------

    def _get_wikipedia_pageviews(self, query: str) -> dict:
        try:
            # Step 1: find the best matching article title
            search_resp = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": 1,
                    "format": "json",
                },
                timeout=8,
            )
            if search_resp.status_code != 200:
                return {}
            results = search_resp.json()
            if not results or not results[1]:
                return {}
            article = results[1][0].replace(" ", "_")

            # Step 2: fetch pageviews for the last 30 days
            end = datetime.date.today()
            start = end - datetime.timedelta(days=30)
            pv_resp = requests.get(
                f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
                f"/en.wikipedia/all-access/all-agents/{requests.utils.quote(article)}"
                f"/daily/{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}",
                headers={"User-Agent": "PolymarketTradingBot/1.0"},
                timeout=8,
            )
            if pv_resp.status_code != 200:
                return {}
            items = pv_resp.json().get("items", [])
            if not items:
                return {}
            total_views = sum(i.get("views", 0) for i in items)
            recent_views = sum(i.get("views", 0) for i in items[-7:])
            avg_daily = total_views // max(len(items), 1)
            return {
                "article": article.replace("_", " "),
                "views_last_7d": recent_views,
                "avg_daily_30d": avg_daily,
                "trending": recent_views > avg_daily * 7 * 1.5,
            }
        except Exception as e:
            print(f"  [DataEnricher] Wikipedia error: {e}")
            return {}

    # ------------------------------------------------------------------
    # Metaculus (public API — no key required)
    # ------------------------------------------------------------------

    def _get_metaculus(self, query: str, max_questions: int = 3) -> list:
        try:
            resp = requests.get(
                "https://www.metaculus.com/api2/questions/",
                params={
                    "search": query,
                    "status": "open",
                    "limit": max_questions,
                    "order_by": "-activity",
                },
                headers={"Accept": "application/json"},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            questions = resp.json().get("results", [])
            results = []
            for q in questions:
                pred = q.get("community_prediction", {})
                p_yes = (pred.get("full", {}) or {}).get("q2")
                if p_yes is not None:
                    results.append({
                        "source": "Metaculus",
                        "title": q.get("title", ""),
                        "community_p_yes": round(p_yes, 3),
                        "forecasters": q.get("number_of_forecasters", 0),
                    })
            return results
        except Exception as e:
            print(f"  [DataEnricher] Metaculus error: {e}")
            return []

    # ------------------------------------------------------------------
    # Tavily — real-time AI web search (requires TAVILY_API_KEY)
    # ------------------------------------------------------------------

    def _get_tavily(self, question: str, max_results: int = 5) -> list:
        if not self.tavily_key:
            return []
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_key)
            resp = client.search(question, max_results=max_results, search_depth="basic")
            results = resp.get("results", [])
            return [
                {
                    "source": "Tavily",
                    "title": r.get("title", ""),
                    "snippet": (r.get("content") or "")[:250],
                    "url": r.get("url", ""),
                    "score": round(r.get("score", 0), 2),
                }
                for r in results if r.get("title")
            ]
        except Exception as e:
            print(f"  [DataEnricher] Tavily error: {e}")
            return []

    # ------------------------------------------------------------------
    # ESPN unofficial API — live sports scores, standings, injuries
    # ------------------------------------------------------------------

    def _detect_espn_league(self, question: str) -> str:
        q = question.lower()
        for keyword, league in self.ESPN_LEAGUES.items():
            if keyword in q:
                return league
        return ""

    def _get_espn(self, question: str) -> dict:
        league = self._detect_espn_league(question)
        if not league:
            return {}
        try:
            sport, slug = league.split("/", 1)
            base = f"http://site.api.espn.com/apis/site/v2/sports/{league}"

            # Scoreboard — live/recent games
            sb_resp = requests.get(f"{base}/scoreboard", timeout=8)
            games = []
            if sb_resp.status_code == 200:
                events = sb_resp.json().get("events", [])
                for e in events[:5]:
                    comps = e.get("competitions", [{}])[0]
                    competitors = comps.get("competitors", [])
                    if len(competitors) >= 2:
                        home = competitors[0]
                        away = competitors[1]
                        status = e.get("status", {}).get("type", {}).get("description", "")
                        games.append({
                            "home": home.get("team", {}).get("displayName", ""),
                            "home_score": home.get("score", ""),
                            "away": away.get("team", {}).get("displayName", ""),
                            "away_score": away.get("score", ""),
                            "status": status,
                            "date": e.get("date", "")[:10],
                        })

            # Standings
            standings = []
            st_resp = requests.get(f"{base}/standings", timeout=8)
            if st_resp.status_code == 200:
                entries = st_resp.json().get("standings", {}).get("entries", [])
                for entry in entries[:8]:
                    team = entry.get("team", {}).get("displayName", "")
                    stats = {s["name"]: s.get("displayValue", "") for s in entry.get("stats", [])}
                    standings.append({"team": team, "stats": stats})

            return {"league": league, "games": games, "standings": standings}
        except Exception as e:
            print(f"  [DataEnricher] ESPN error: {e}")
            return {}

    # ------------------------------------------------------------------
    # Google Trends (pytrends — may be blocked on cloud IPs)
    # ------------------------------------------------------------------

    def _get_google_trends(self, query: str) -> dict:
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="en-US", tz=0, timeout=(8, 8))
            pt.build_payload([query[:100]], timeframe="now 7-d")
            df = pt.interest_over_time()
            if df.empty:
                return {}
            col = [c for c in df.columns if c != "isPartial"]
            if not col:
                return {}
            series = df[col[0]]
            avg = int(series.mean())
            peak = int(series.max())
            recent = int(series.iloc[-1]) if len(series) else avg
            return {
                "avg_interest_7d": avg,
                "peak_interest_7d": peak,
                "current_interest": recent,
                "trending": recent > avg * 1.3,
            }
        except Exception as e:
            print(f"  [DataEnricher] Google Trends error: {e}")
            return {}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def get_context(self, question: str) -> str:
        keywords = _extract_keywords(question)
        if not keywords:
            return ""

        print(f"  [DataEnricher] Fetching live context for: {keywords!r}")

        tavily    = self._get_tavily(question)       # full question for best results
        espn      = self._get_espn(question)
        news      = self._get_news(keywords)
        tweets    = self._get_tweets(keywords)
        reddit    = self._get_reddit(keywords)
        wiki      = self._get_wikipedia_pageviews(keywords)
        metaculus = self._get_metaculus(keywords)
        trends    = self._get_google_trends(keywords)

        if not any([tavily, espn, news, tweets, reddit, wiki, metaculus, trends]):
            return ""

        lines = ["LIVE CONTEXT (fetched now — use this to update your forecast):"]

        # Tavily first — highest-quality real-time answer
        if tavily:
            lines.append(f"\nWeb search results ({len(tavily)}):")
            for r in tavily:
                lines.append(f"  [{r['score']}] {r['title']}")
                if r["snippet"]:
                    lines.append(f"    {r['snippet']}")

        # ESPN sports data
        if espn:
            lines.append(f"\nESPN — {espn['league']}:")
            if espn.get("games"):
                lines.append("  Recent/live games:")
                for g in espn["games"]:
                    lines.append(
                        f"    {g['away']} {g['away_score']} @ {g['home']} {g['home_score']}"
                        f"  [{g['status']}  {g['date']}]"
                    )
            if espn.get("standings"):
                lines.append("  Current standings (top 8):")
                for s in espn["standings"]:
                    stat_str = "  ".join(f"{k}:{v}" for k, v in list(s["stats"].items())[:3])
                    lines.append(f"    {s['team']}: {stat_str}")

        if trends:
            trend_note = " TRENDING" if trends.get("trending") else ""
            lines.append(
                f"\nGoogle Trends (last 7d): interest={trends['current_interest']}/100 "
                f"(avg {trends['avg_interest_7d']}, peak {trends['peak_interest_7d']}){trend_note}"
            )

        if wiki:
            trend_note = " — SPIKING" if wiki.get("trending") else ""
            lines.append(
                f"\nWikipedia '{wiki['article']}': {wiki['views_last_7d']:,} views last 7d "
                f"(avg daily: {wiki['avg_daily_30d']:,}){trend_note}"
            )

        if metaculus:
            lines.append(f"\nMetaculus similar questions ({len(metaculus)} found):")
            for m in metaculus:
                lines.append(
                    f"  [{m['forecasters']} forecasters] {m['title']} "
                    f"→ community p(Yes)={m['community_p_yes']:.1%}"
                )

        if news:
            lines.append(f"\nRecent news ({len(news)} articles):")
            for a in news:
                lines.append(f"  [{a['published']}] {a['title']}")
                if a["snippet"]:
                    lines.append(f"    {a['snippet']}")

        if tweets:
            lines.append(f"\nRecent tweets ({len(tweets)}):")
            for t in tweets:
                lines.append(f"  [{t['engagement']} engagements] {t['text'][:160]}")

        if reddit:
            lines.append(f"\nReddit posts ({len(reddit)}):")
            for r in reddit:
                lines.append(
                    f"  [r/{r['subreddit']} | ↑{r['score']} | {r['comments']} comments] {r['title']}"
                )

        lines.append("")
        return "\n".join(lines)
