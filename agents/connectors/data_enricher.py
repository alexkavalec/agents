"""
DataEnricher — fetches live context for a market question from multiple sources
and returns a formatted block injected into the superforecaster prompt.

Sources:
  - Tavily           (requires TAVILY_API_KEY — real-time web search)
  - ESPN             (unofficial API, no key — comprehensive sports analytics)
  - NewsAPI          (requires NEWS_API_KEY)
  - Twitter/X        (requires TWITTER_BEARER_TOKEN)
  - Reddit           (public API, no key)
  - Wikipedia        (public API, no key)
  - Metaculus        (public API, no key)
  - Google Trends    (pytrends, no key — may be unreliable on cloud IPs)
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

ESPN_LEAGUES = {
    "champions league": "soccer/uefa.champions",
    "ucl": "soccer/uefa.champions",
    "uefa champions": "soccer/uefa.champions",
    "europa league": "soccer/uefa.europa",
    "premier league": "soccer/eng.1",
    "la liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie a": "soccer/ita.1",
    "ligue 1": "soccer/fra.1",
    "mls": "soccer/usa.1",
    "world cup": "soccer/fifa.world",
    "euros": "soccer/uefa.euro",
    "euro 2024": "soccer/uefa.euro",
    "nba": "basketball/nba",
    "nba finals": "basketball/nba",
    "nfl": "football/nfl",
    "super bowl": "football/nfl",
    "mlb": "baseball/mlb",
    "world series": "baseball/mlb",
    "nhl": "hockey/nhl",
    "stanley cup": "hockey/nhl",
    "ncaa": "basketball/mens-college-basketball",
    "march madness": "basketball/mens-college-basketball",
    "formula 1": "racing/f1",
    "f1": "racing/f1",
}


def _extract_keywords(question: str, max_words: int = 5) -> str:
    words = re.sub(r"[^\w\s]", "", question).split()
    keywords = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:max_words])


def _safe_get(url: str, params: dict = None, timeout: int = 8) -> dict:
    try:
        resp = requests.get(url, params=params, timeout=timeout,
                            headers={"User-Agent": "PolymarketTradingBot/1.0"})
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


class DataEnricher:

    def __init__(self):
        self.news_api_key = os.getenv("NEWS_API_KEY") or os.getenv("NEWSAPI_API_KEY")
        self.twitter_bearer = os.getenv("TWITTER_BEARER_TOKEN")
        self.tavily_key = os.getenv("TAVILY_API_KEY")

    # ══════════════════════════════════════════════════════════════════
    # ESPN — comprehensive sports analytics
    # ══════════════════════════════════════════════════════════════════

    def _detect_espn_league(self, question: str) -> str:
        q = question.lower()
        for keyword, league in sorted(ESPN_LEAGUES.items(), key=lambda x: -len(x[0])):
            if keyword in q:
                return league
        return ""

    def _espn_all_teams(self, base: str) -> list:
        """Fetch all teams in the league. Returns list of {id, name, abbreviation}."""
        data = _safe_get(f"{base}/teams", {"limit": 100})
        teams = []
        for group in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            t = group.get("team", {})
            teams.append({
                "id": t.get("id", ""),
                "name": t.get("displayName", ""),
                "short": t.get("shortDisplayName", ""),
                "abbr": t.get("abbreviation", ""),
            })
        # Also try direct teams array
        if not teams:
            for t in data.get("teams", []):
                teams.append({
                    "id": t.get("id", ""),
                    "name": t.get("displayName", t.get("name", "")),
                    "short": t.get("shortDisplayName", ""),
                    "abbr": t.get("abbreviation", ""),
                })
        return teams

    def _espn_detect_teams(self, question: str, all_teams: list) -> dict:
        """Find which teams from the league are mentioned in the question.
        Returns {team_id: team_name}."""
        q = question.lower()
        found = {}
        for t in all_teams:
            names = [t["name"].lower(), t["short"].lower(), t["abbr"].lower()]
            if any(n and n in q for n in names):
                found[t["id"]] = t["name"]
        return found

    def _espn_scoreboard(self, base: str) -> list:
        """Recent and upcoming games."""
        data = _safe_get(f"{base}/scoreboard")
        games = []
        for e in data.get("events", [])[:8]:
            comps = e.get("competitions", [{}])[0]
            competitors = comps.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            status = e.get("status", {}).get("type", {})
            games.append({
                "home": home.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", ""),
                "home_record": home.get("records", [{}])[0].get("summary", "") if home.get("records") else "",
                "away": away.get("team", {}).get("displayName", ""),
                "away_score": away.get("score", ""),
                "away_record": away.get("records", [{}])[0].get("summary", "") if away.get("records") else "",
                "status": status.get("description", ""),
                "completed": status.get("completed", False),
                "date": e.get("date", "")[:10],
                "venue": comps.get("venue", {}).get("fullName", ""),
                "notes": comps.get("notes", [{}])[0].get("headline", "") if comps.get("notes") else "",
            })
        return games

    def _espn_standings(self, base: str) -> list:
        """Full league standings."""
        data = _safe_get(f"{base}/standings")
        rows = []
        # Walk the standings structure (varies by sport)
        groups = data.get("standings", {}).get("entries", [])
        if not groups:
            for grp in data.get("children", []):
                groups += grp.get("standings", {}).get("entries", [])
        for entry in groups[:16]:
            team = entry.get("team", {}).get("displayName", "")
            stats = {s["name"]: s.get("displayValue", "") for s in entry.get("stats", [])}
            rows.append({"team": team, "stats": stats})
        return rows

    def _espn_league_news(self, base: str, max_articles: int = 5) -> list:
        """Top league news articles."""
        data = _safe_get(f"{base}/news", {"limit": max_articles})
        articles = []
        for a in data.get("articles", [])[:max_articles]:
            articles.append({
                "headline": a.get("headline", ""),
                "description": (a.get("description") or "")[:200],
                "published": (a.get("published") or "")[:10],
            })
        return articles

    def _espn_team_profile(self, base: str, team_id: str) -> dict:
        """Full team profile: record, home/away splits, streak."""
        data = _safe_get(f"{base}/teams/{team_id}")
        team = data.get("team", {})
        record_items = team.get("record", {}).get("items", [])
        records = {}
        for r in record_items:
            label = r.get("description", r.get("type", "overall"))
            records[label] = r.get("summary", "")
        return {
            "name": team.get("displayName", ""),
            "location": team.get("location", ""),
            "nickname": team.get("nickname", ""),
            "color": team.get("color", ""),
            "records": records,
            "standing_summary": team.get("standingSummary", ""),
        }

    def _espn_team_schedule(self, base: str, team_id: str, max_games: int = 10) -> list:
        """Recent + upcoming schedule for a team."""
        data = _safe_get(f"{base}/teams/{team_id}/schedule")
        events = data.get("events", [])
        results = []
        today = datetime.date.today().isoformat()
        # Sort: recent completed first, then upcoming
        completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed")]
        upcoming = [e for e in events if not e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed")]
        for e in (completed[-5:] + upcoming[:5]):
            comp = e.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            result_entry = {
                "date": e.get("date", "")[:10],
                "home": home.get("team", {}).get("displayName", ""),
                "away": away.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
                "completed": comp.get("status", {}).get("type", {}).get("completed", False),
                "result": "",
                "notes": comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else "",
            }
            # Determine W/L/D for this team
            if result_entry["completed"] and result_entry["home_score"] and result_entry["away_score"]:
                try:
                    hs, as_ = float(result_entry["home_score"]), float(result_entry["away_score"])
                    is_home = home.get("team", {}).get("id") == team_id
                    team_score = hs if is_home else as_
                    opp_score = as_ if is_home else hs
                    if team_score > opp_score:
                        result_entry["result"] = "W"
                    elif team_score < opp_score:
                        result_entry["result"] = "L"
                    else:
                        result_entry["result"] = "D"
                except Exception:
                    pass
            results.append(result_entry)
        return results

    def _espn_team_injuries(self, base: str, team_id: str) -> list:
        """Injury report for a team."""
        data = _safe_get(f"{base}/teams/{team_id}/injuries")
        injuries = []
        for item in data.get("injuries", [])[:15]:
            athlete = item.get("athlete", {})
            injuries.append({
                "player": athlete.get("displayName", ""),
                "position": athlete.get("position", {}).get("abbreviation", ""),
                "status": item.get("status", ""),
                "detail": item.get("shortComment", item.get("longComment", ""))[:120],
            })
        return injuries

    def _espn_team_roster(self, base: str, team_id: str) -> list:
        """Key players from the roster with positions."""
        data = _safe_get(f"{base}/teams/{team_id}/roster")
        players = []
        for group in data.get("athletes", []):
            # some sports return groups (offense/defense), others flat
            items = group.get("items", [group]) if isinstance(group, dict) and "items" in group else [group]
            for p in items:
                if isinstance(p, dict) and p.get("displayName"):
                    players.append({
                        "name": p.get("displayName", ""),
                        "position": p.get("position", {}).get("abbreviation", ""),
                        "jersey": p.get("jersey", ""),
                        "status": p.get("status", {}).get("type", "") if isinstance(p.get("status"), dict) else "",
                    })
        return players[:25]

    def _espn_team_stats(self, base: str, team_id: str) -> dict:
        """Season statistics for a team."""
        data = _safe_get(f"{base}/teams/{team_id}/statistics")
        stats = {}
        for category in data.get("results", {}).get("stats", {}).get("categories", []):
            cat_name = category.get("displayName", "")
            for stat in category.get("stats", [])[:6]:
                key = f"{cat_name}/{stat.get('displayName', '')}"
                stats[key] = stat.get("displayValue", "")
        # Also try splits
        if not stats:
            for split in data.get("splits", {}).get("categories", []):
                for stat in split.get("stats", [])[:4]:
                    stats[stat.get("displayName", stat.get("name", ""))] = stat.get("displayValue", "")
        return stats

    def _espn_team_news(self, base: str, team_id: str, max_articles: int = 4) -> list:
        """News articles for a specific team."""
        data = _safe_get(f"{base}/news", {"team": team_id, "limit": max_articles})
        articles = []
        for a in data.get("articles", [])[:max_articles]:
            articles.append({
                "headline": a.get("headline", ""),
                "description": (a.get("description") or "")[:200],
                "published": (a.get("published") or "")[:10],
            })
        return articles

    def _espn_head_to_head(self, base: str, team_id_1: str, team_id_2: str) -> list:
        """Recent head-to-head results between two teams from scoreboard history."""
        data = _safe_get(f"{base}/scoreboard", {"dates": "20240101-20261231", "limit": 100})
        h2h = []
        for e in data.get("events", []):
            comp = e.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            ids = {c.get("team", {}).get("id") for c in competitors}
            if team_id_1 in ids and team_id_2 in ids:
                home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
                h2h.append({
                    "date": e.get("date", "")[:10],
                    "home": home.get("team", {}).get("displayName", ""),
                    "home_score": home.get("score", ""),
                    "away": away.get("team", {}).get("displayName", ""),
                    "away_score": away.get("score", ""),
                    "completed": comp.get("status", {}).get("type", {}).get("completed", False),
                })
        return h2h[-5:]  # last 5 meetings

    def _get_espn(self, question: str) -> dict:
        league = self._detect_espn_league(question)
        if not league:
            return {}

        base = f"http://site.api.espn.com/apis/site/v2/sports/{league}"
        print(f"  [ESPN] Fetching data for league: {league}")

        result = {
            "league": league,
            "scoreboard": [],
            "standings": [],
            "league_news": [],
            "teams": {},
        }

        # League-level data (always fetch)
        result["scoreboard"] = self._espn_scoreboard(base)
        result["standings"] = self._espn_standings(base)
        result["league_news"] = self._espn_league_news(base)

        # Team-level data — only for teams mentioned in the question
        all_teams = self._espn_all_teams(base)
        mentioned = self._espn_detect_teams(question, all_teams)
        print(f"  [ESPN] Teams detected in question: {list(mentioned.values()) or 'none'}")

        team_ids = list(mentioned.keys())
        for team_id, team_name in mentioned.items():
            print(f"  [ESPN] Fetching full profile for {team_name}...")
            result["teams"][team_name] = {
                "id": team_id,
                "profile": self._espn_team_profile(base, team_id),
                "schedule": self._espn_team_schedule(base, team_id),
                "injuries": self._espn_team_injuries(base, team_id),
                "roster": self._espn_team_roster(base, team_id),
                "stats": self._espn_team_stats(base, team_id),
                "news": self._espn_team_news(base, team_id),
            }

        # Head-to-head if exactly two teams detected
        if len(team_ids) == 2:
            result["head_to_head"] = self._espn_head_to_head(base, team_ids[0], team_ids[1])
        else:
            result["head_to_head"] = []

        return result

    def _format_espn(self, espn: dict) -> list:
        """Format ESPN data into readable lines for the prompt."""
        lines = [f"\nESPN — {espn['league']}:"]

        # Scoreboard
        if espn.get("scoreboard"):
            lines.append("  Recent / upcoming games:")
            for g in espn["scoreboard"]:
                score = (f"{g['away_score']}-{g['home_score']}"
                         if g["completed"] else "vs")
                note = f" [{g['notes']}]" if g.get("notes") else ""
                venue = f" @ {g['venue']}" if g.get("venue") else ""
                lines.append(
                    f"    {g['date']}  {g['away']} ({g['away_record']}) {score} "
                    f"{g['home']} ({g['home_record']})  [{g['status']}]{venue}{note}"
                )

        # Standings (compact)
        if espn.get("standings"):
            lines.append("  Standings:")
            for s in espn["standings"][:10]:
                stat_str = "  ".join(f"{k}:{v}" for k, v in list(s["stats"].items())[:4])
                lines.append(f"    {s['team']:<28} {stat_str}")

        # Head-to-head
        if espn.get("head_to_head"):
            lines.append("  Head-to-head (recent):")
            for g in espn["head_to_head"]:
                lines.append(
                    f"    {g['date']}  {g['away']} {g['away_score']}-{g['home_score']} {g['home']}"
                )

        # League news
        if espn.get("league_news"):
            lines.append("  League news:")
            for a in espn["league_news"]:
                lines.append(f"    [{a['published']}] {a['headline']}")
                if a["description"]:
                    lines.append(f"      {a['description']}")

        # Per-team detail
        for team_name, td in espn.get("teams", {}).items():
            lines.append(f"\n  ── {team_name} ──")

            profile = td.get("profile", {})
            if profile.get("records"):
                rec_str = "  ".join(f"{k}: {v}" for k, v in profile["records"].items())
                lines.append(f"    Records: {rec_str}")
            if profile.get("standing_summary"):
                lines.append(f"    Standing: {profile['standing_summary']}")

            # Recent form
            schedule = td.get("schedule", [])
            completed = [g for g in schedule if g["completed"]]
            upcoming = [g for g in schedule if not g["completed"]]
            if completed:
                form_str = " ".join(g.get("result", "?") for g in completed[-5:])
                lines.append(f"    Recent form (last {len(completed[-5:])}): {form_str}")
                lines.append("    Recent results:")
                for g in completed[-5:]:
                    lines.append(
                        f"      {g['date']}  {g['away']} {g['away_score']}-{g['home_score']} {g['home']}"
                        + (f"  [{g['notes']}]" if g.get("notes") else "")
                    )
            if upcoming:
                lines.append("    Upcoming fixtures:")
                for g in upcoming[:3]:
                    lines.append(
                        f"      {g['date']}  {g['away']} vs {g['home']}"
                        + (f"  [{g['notes']}]" if g.get("notes") else "")
                    )

            # Injuries
            injuries = td.get("injuries", [])
            if injuries:
                lines.append(f"    Injuries ({len(injuries)}):")
                for inj in injuries:
                    lines.append(
                        f"      [{inj['status']}] {inj['player']} ({inj['position']}) — {inj['detail']}"
                    )
            else:
                lines.append("    Injuries: none reported")

            # Stats
            stats = td.get("stats", {})
            if stats:
                lines.append("    Season stats:")
                for k, v in list(stats.items())[:8]:
                    lines.append(f"      {k}: {v}")

            # Roster (key players only — first 15)
            roster = td.get("roster", [])
            if roster:
                active = [p for p in roster if p.get("status") != "injured"][:15]
                lines.append(f"    Key players ({len(active)} shown):")
                for p in active:
                    lines.append(f"      #{p['jersey']} {p['name']} ({p['position']})")

            # Team news
            team_news = td.get("news", [])
            if team_news:
                lines.append("    Latest news:")
                for a in team_news:
                    lines.append(f"      [{a['published']}] {a['headline']}")
                    if a["description"]:
                        lines.append(f"        {a['description']}")

        return lines

    # ══════════════════════════════════════════════════════════════════
    # Tavily — real-time AI web search
    # ══════════════════════════════════════════════════════════════════

    def _get_tavily(self, question: str, max_results: int = 5) -> list:
        if not self.tavily_key:
            return []
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_key)
            resp = client.search(question, max_results=max_results, search_depth="basic")
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": (r.get("content") or "")[:250],
                    "score": round(r.get("score", 0), 2),
                }
                for r in resp.get("results", []) if r.get("title")
            ]
        except Exception as e:
            print(f"  [DataEnricher] Tavily error: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # NewsAPI
    # ══════════════════════════════════════════════════════════════════

    def _get_news(self, query: str, max_articles: int = 5) -> list:
        if not self.news_api_key:
            return []
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query, "language": "en", "sortBy": "publishedAt",
                    "pageSize": max_articles,
                    "from": (datetime.date.today() - datetime.timedelta(days=7)).isoformat(),
                },
                headers={"X-Api-Key": self.news_api_key},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            return [
                {
                    "title": a.get("title", ""),
                    "snippet": (a.get("description") or "")[:200],
                    "published": (a.get("publishedAt") or "")[:10],
                }
                for a in resp.json().get("articles", []) if a.get("title")
            ]
        except Exception as e:
            print(f"  [DataEnricher] NewsAPI error: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # Twitter / X
    # ══════════════════════════════════════════════════════════════════

    def _get_tweets(self, query: str, max_tweets: int = 8) -> list:
        if not self.twitter_bearer:
            return []
        try:
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query": f"{query} -is:retweet -is:reply lang:en",
                    "max_results": max_tweets,
                    "tweet.fields": "created_at,public_metrics",
                },
                headers={"Authorization": f"Bearer {self.twitter_bearer}"},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            return [
                {
                    "text": t.get("text", "")[:280],
                    "engagement": (t.get("public_metrics", {}).get("like_count", 0)
                                  + t.get("public_metrics", {}).get("retweet_count", 0)),
                }
                for t in resp.json().get("data", [])
            ]
        except Exception as e:
            print(f"  [DataEnricher] Twitter error: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # Reddit
    # ══════════════════════════════════════════════════════════════════

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
            return [
                {
                    "subreddit": p["data"].get("subreddit", ""),
                    "title": p["data"].get("title", ""),
                    "score": p["data"].get("score", 0),
                    "comments": p["data"].get("num_comments", 0),
                }
                for p in resp.json().get("data", {}).get("children", [])
            ]
        except Exception as e:
            print(f"  [DataEnricher] Reddit error: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # Wikipedia Pageviews
    # ══════════════════════════════════════════════════════════════════

    def _get_wikipedia_pageviews(self, query: str) -> dict:
        try:
            search = _safe_get(
                "https://en.wikipedia.org/w/api.php",
                {"action": "opensearch", "search": query, "limit": 1, "format": "json"},
            )
            titles = search[1] if isinstance(search, list) and len(search) > 1 else []
            if not titles:
                return {}
            article = titles[0].replace(" ", "_")
            end = datetime.date.today()
            start = end - datetime.timedelta(days=30)
            pv = _safe_get(
                f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
                f"/en.wikipedia/all-access/all-agents/{requests.utils.quote(article)}"
                f"/daily/{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
            )
            items = pv.get("items", [])
            if not items:
                return {}
            total = sum(i.get("views", 0) for i in items)
            recent = sum(i.get("views", 0) for i in items[-7:])
            avg_daily = total // max(len(items), 1)
            return {
                "article": article.replace("_", " "),
                "views_last_7d": recent,
                "avg_daily_30d": avg_daily,
                "trending": recent > avg_daily * 7 * 1.5,
            }
        except Exception as e:
            print(f"  [DataEnricher] Wikipedia error: {e}")
            return {}

    # ══════════════════════════════════════════════════════════════════
    # Metaculus
    # ══════════════════════════════════════════════════════════════════

    def _get_metaculus(self, query: str, max_questions: int = 3) -> list:
        try:
            resp = requests.get(
                "https://www.metaculus.com/api2/questions/",
                params={"search": query, "status": "open", "limit": max_questions,
                        "order_by": "-activity"},
                headers={"Accept": "application/json"},
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            results = []
            for q in resp.json().get("results", []):
                pred = q.get("community_prediction", {})
                p_yes = (pred.get("full", {}) or {}).get("q2")
                if p_yes is not None:
                    results.append({
                        "title": q.get("title", ""),
                        "community_p_yes": round(p_yes, 3),
                        "forecasters": q.get("number_of_forecasters", 0),
                    })
            return results
        except Exception as e:
            print(f"  [DataEnricher] Metaculus error: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # Google Trends
    # ══════════════════════════════════════════════════════════════════

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
            return {
                "avg_interest_7d": int(series.mean()),
                "peak_interest_7d": int(series.max()),
                "current_interest": int(series.iloc[-1]),
                "trending": int(series.iloc[-1]) > int(series.mean()) * 1.3,
            }
        except Exception as e:
            print(f"  [DataEnricher] Google Trends error: {e}")
            return {}

    # ══════════════════════════════════════════════════════════════════
    # Main entry point
    # ══════════════════════════════════════════════════════════════════

    def get_context(self, question: str) -> str:
        keywords = _extract_keywords(question)
        if not keywords:
            return ""

        print(f"  [DataEnricher] Fetching live context for: {keywords!r}")

        tavily    = self._get_tavily(question)
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

        # Tavily — highest quality, goes first
        if tavily:
            lines.append(f"\nReal-time web search ({len(tavily)} results):")
            for r in tavily:
                lines.append(f"  [{r['score']}] {r['title']}")
                if r["snippet"]:
                    lines.append(f"    {r['snippet']}")

        # ESPN — full sports analytics block
        if espn:
            lines += self._format_espn(espn)

        # Attention signals
        if trends:
            note = " TRENDING" if trends.get("trending") else ""
            lines.append(
                f"\nGoogle Trends (7d): interest={trends['current_interest']}/100 "
                f"(avg {trends['avg_interest_7d']}, peak {trends['peak_interest_7d']}){note}"
            )
        if wiki:
            note = " — SPIKING" if wiki.get("trending") else ""
            lines.append(
                f"\nWikipedia '{wiki['article']}': {wiki['views_last_7d']:,} views last 7d "
                f"(avg daily {wiki['avg_daily_30d']:,}){note}"
            )

        # Prediction market cross-reference
        if metaculus:
            lines.append(f"\nMetaculus ({len(metaculus)} similar questions):")
            for m in metaculus:
                lines.append(
                    f"  [{m['forecasters']} forecasters] {m['title']} "
                    f"→ p(Yes)={m['community_p_yes']:.1%}"
                )

        # News
        if news:
            lines.append(f"\nNewsAPI ({len(news)} articles):")
            for a in news:
                lines.append(f"  [{a['published']}] {a['title']}")
                if a["snippet"]:
                    lines.append(f"    {a['snippet']}")

        # Social
        if tweets:
            lines.append(f"\nTwitter/X ({len(tweets)} tweets):")
            for t in tweets:
                lines.append(f"  [{t['engagement']} eng] {t['text'][:160]}")

        if reddit:
            lines.append(f"\nReddit ({len(reddit)} posts):")
            for r in reddit:
                lines.append(
                    f"  [r/{r['subreddit']} ↑{r['score']} {r['comments']}cmts] {r['title']}"
                )

        lines.append("")
        return "\n".join(lines)
