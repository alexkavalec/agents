# Polymarket Whale-Leaderboard Trading Bot

An autonomous trading bot for [Polymarket](https://polymarket.com) that trades **strictly off
the public leaderboard** — no AI, no market scanning, no news/RAG enrichment, and no risk
management beyond 6 explicit rules. It watches the top 10 traders on the today / weekly /
monthly / all-time leaderboards and copies their consensus moves.

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture and change history.

## How it works

Every 15 minutes, `Trader.one_best_trade()`:

1. Fetches the top 10 traders on the today/weekly/monthly/all-time profit leaderboards and
   every open position each one currently holds
2. Builds a consensus signal wherever 2+ independent whales hold the same side of the same
   market
3. Keeps only signals for markets resolving today, unless 5+ whales agree (see rule 6 below)
4. Bets a flat 25% of current balance, buying the exact token the whales hold, via a FOK
   market order

## The only rules it follows

1. Bet exactly 25% of current balance on every trade
2. No take-profit, no stop-loss, no cash-out — once opened, a position is held to resolution
   no matter what, even if it's down
3. No daily loss limit, no daily spend cap
4. Never bet the exact same (market, side) twice
5. Never bet the opposite outcome of a market it already has a position in
6. Only trade markets that resolve today (UTC), unless 5+ independent whales agree on the
   same (market, side) — then it's eligible regardless of when the market resolves

That's it. There's no cooldown, no max open positions, no correlation filter, no signal-quality
filtering beyond rule 6 above.

## Setup

Requires Python 3.9.

```
git clone <this-repo>
cd agents
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `POLYGON_WALLET_PRIVATE_KEY` — required, your trading wallet's private key
- `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE` — optional, derived automatically if unset
- `POLYGON_FUNDER_ADDRESS` / `POLYGON_RPC_URL` — optional
- `DISCORD_WEBHOOK_URL` — optional, notifications for fills and FOK kills
- `DASHBOARD_TOKEN` — optional but recommended, protects the stats dashboard (see below)
- `DATA_DIR` — optional but recommended on Railway, points state files at a mounted Volume so
  trade history survives redeploys (see Deployment below)

Fund the wallet with USDC on Polygon, then run:

```
python cli.py run-loop --interval-minutes 15
```

**Warning:** this bot has no downside protection whatsoever. It bets 25% of its balance on every
signal and never sells a losing position. Only fund it with money you're fully prepared to lose.

## Stats dashboard

`run-loop` also starts a tiny read-only web dashboard (stdlib `http.server`, no extra
dependencies) alongside the trading loop. It listens on `$PORT` (defaults to 8080
locally) and refreshes itself every 60 seconds. Visit `/`, or `/?key=...` if
`DASHBOARD_TOKEN` is set — without a token the dashboard is publicly readable to
anyone with the URL, so set one before exposing it. Data also available as JSON at
`/api/stats`.

Shows:
- Balance, open positions, win/loss record, ROI%, and a P&L chart (all live/updated
  every poll)
- The 4 whale leaderboards (today/weekly/monthly/all-time) — click any trader to open a
  detail modal with their weekly P&L and every open position they currently hold. This
  part only refreshes as often as the bot's own 15-minute cycle runs (it reads a cache
  the bot writes; the dashboard polling faster than that doesn't trigger extra scraping)

## Deployment

Runs 24/7 on [Railway](https://railway.app) with the same start command above. The `Dockerfile`
installs `requirements.txt`; the actual run command is configured as the Railway service's start
command, not baked into the image. Enable "Public Networking" on the Railway service to get a
URL for the dashboard.

**Persisting state across deploys:** Railway's container filesystem is ephemeral by default —
every redeploy resets trade history, the dedup journal, and whale tracking unless you attach a
Volume:
1. Railway dashboard → your service → **Settings → Volumes** → **Add Volume**, mount it at e.g. `/data`
2. Service → **Variables** → add `DATA_DIR=/data`
3. Redeploy — state now survives future deploys

## License

MIT — see [LICENSE.md](./LICENSE.md).

[Polymarket's Terms of Service](https://polymarket.com/tos) prohibit US persons and persons from
certain other jurisdictions from trading on Polymarket, including via bots/agents.
