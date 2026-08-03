# Polymarket Whale-Leaderboard Trading Bot

An autonomous trading bot for [Polymarket](https://polymarket.com) that trades **strictly off
the public leaderboard** — no AI, no market scanning, no news/RAG enrichment. It watches the
today / weekly / monthly / all-time top-profit traders, and enters a position only when 2+ of
them independently hold the same side of the same market.

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, guardrails, and change history.

## How it works

Each cycle, `Trader.one_best_trade()`:

1. Runs mechanical stop-loss (-60%) / take-profit (+60%) position management first
2. Checks daily loss/spend caps, max open positions, and trade cooldown
3. Scrapes the Polymarket leaderboard (today/weekly/monthly/all-time) and pulls open positions
   for every unique top trader
4. Builds a consensus signal wherever 2+ independent whales hold the same side of the same
   market, within 20% of their average entry price
5. Sizes the trade (3%–10% of balance) based on how many whales agree and their combined
   $ volume/profit, and buys the exact token they hold via a FOK market order

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
- `DISCORD_WEBHOOK_URL` — optional, notifications for fills, guardrail trips, and stop-loss/take-profit exits

Fund the wallet with USDC on Polygon, then run:

```
python cli.py run-loop --interval-minutes 60
```

## Deployment

Runs 24/7 on [Railway](https://railway.app) with the same start command above. The `Dockerfile`
installs `requirements.txt`; the actual run command is configured as the Railway service's start
command, not baked into the image.

## License

MIT — see [LICENSE.md](./LICENSE.md).

[Polymarket's Terms of Service](https://polymarket.com/tos) prohibit US persons and persons from
certain other jurisdictions from trading on Polymarket, including via bots/agents.
