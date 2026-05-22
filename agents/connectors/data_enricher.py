"""
DataEnricher — fetches live context (news + Twitter) for a given market question
and returns a formatted block that gets injected into the superforecaster prompt.

Plug-in architecture: add new sources by implementing a method that returns
a list of {"source", "title", "snippet", "url"} dicts and calling it in get_context().
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
    """Strip stop words from the question and return key terms for API queries."""
    words = re.sub(r"[^\w\s]", "", question).split()
    keywords = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:max_words])


class DataEnricher:
    def __init__(self):
        self.news_api_key = os.getenv("NEWS_API_KEY") or os.getenv("NEWSAPI_API_KEY")
        self.twitter_bearer = os.getenv("TWITTER_BEARER_TOKEN")

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
                for a in articles
                if a.get("title")
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
            # Exclude retweets and replies to get cleaner signal
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
                print(f"  [DataEnricher] Twitter API {resp.status_code}: {resp.text[:120]}")
                return []
            tweets = resp.json().get("data", [])
            return [
                {
                    "source": "Twitter/X",
                    "text": t.get("text", "")[:280],
                    "likes": t.get("public_metrics", {}).get("like_count", 0),
                    "retweets": t.get("public_metrics", {}).get("retweet_count", 0),
                }
                for t in tweets
            ]
        except Exception as e:
            print(f"  [DataEnricher] Twitter error: {e}")
            return []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def get_context(self, question: str) -> str:
        """Return a formatted live-data block for injection into the superforecaster prompt.
        Returns empty string if no data sources are configured or all fail."""
        keywords = _extract_keywords(question)
        if not keywords:
            return ""

        print(f"  [DataEnricher] Fetching live context for: {keywords!r}")

        news = self._get_news(keywords)
        tweets = self._get_tweets(keywords)

        if not news and not tweets:
            return ""

        lines = [f"LIVE CONTEXT (fetched now — use this to update your forecast):"]

        if news:
            lines.append(f"\nRecent news ({len(news)} articles):")
            for a in news:
                lines.append(f"  [{a['published']}] {a['title']}")
                if a["snippet"]:
                    lines.append(f"    {a['snippet']}")

        if tweets:
            lines.append(f"\nRecent tweets ({len(tweets)} tweets):")
            for t in tweets:
                engagement = t["likes"] + t["retweets"]
                lines.append(f"  [{engagement} engagements] {t['text'][:160]}")

        lines.append("")
        return "\n".join(lines)
