# Polymarket Trading Bot — Project Notes

## Repo & Deploy
- **Repo:** github.com/alexkavalec/agents (forked from Polymarket/agents)
- **Host:** Railway, runs 24/7. Start command: `python cli.py run-loop --interval-minutes 15`
- **Stack:** Python 3.9, py-clob-client-v2, web3.py. **No LLM/AI dependency** — the bot is
  purely mechanical, driven entirely by Polymarket leaderboard data.
- **Branch for new work:** `claude/polymarket-trading-bot-*` → PR → squash merge to `main`

## Key Files (the entire trading pipeline — nothing else remains)
- `cli.py` — `run_loop` / `run_autonomous_trader` entry points only
- `agents/application/trade.py` — `Trader`: orchestration, sizing, order placement, the
  two dedup rules. No position management of any kind — positions are never sold by the bot.
- `agents/connectors/whale_tracker.py` — leaderboard scraping + consensus signal generation
  (the bot's SOLE trade signal source)
- `agents/polymarket/polymarket.py` — CLOB client wrapper: auth, balance, positions, orders
- `agents/memory/trade_log.py` / `agents/memory/scoreboard.py` — trade history + win/loss tracking

Everything else from the original fork (Gamma market scanner, Chroma RAG, OpenAI-based
superforecaster/trade-constructor/market-selector, news/Twitter/Reddit/Wikipedia/Tavily
enrichment, the `create_market`/`Creator` market-creation feature, and their CLI commands)
has been **deleted**, not just disconnected — see git history if any of it needs resurrecting.
The bot only ever calls Polymarket's own `data-api.polymarket.com` and `clob.polymarket.com`
endpoints, plus a Discord webhook for notifications.

## Current Architecture — pure leaderboard-following, zero risk management (as of 2026-08-03)
The bot has no opinion of its own about any market. It watches what the top leaderboard
traders are doing and copies consensus moves, full stop. There is no AI, no market scan, no
signal filtering beyond "did enough whales agree," no stop-loss, no take-profit, no daily
caps — the only guardrails are the five rules listed below.

`Trader.one_best_trade()` runs every 15 minutes:

1. **Whale scan** — `WhaleTracker.get_whale_signals()` fetches the top 10 traders on the
   today / weekly / monthly / all-time profit leaderboards (`polymarket.com/leaderboard/overall/
   {window}/profit`), pulls every open position each of those traders currently holds, and logs
   both boxes in full every cycle (leaderboard = each whale's record for that window, e.g. weekly
   = 7-day profit; signals = the positions found)
2. **Consensus signal** — a `(market, side)` becomes a candidate signal once `MIN_WHALES_AGREE`
   (2+, in `whale_tracker.py`) independent whales hold it. Nothing else gates a signal — no price
   drift cap, no minimum whale count beyond 2, no filtering by market category or liquidity
3. **Signal selection** — signals are pre-sorted (fresh first, then whale count, then combined
   $ volume/profit); the bot walks the list and picks the first one that clears the two rules below
4. **Sizing** — flat `BET_FRACTION` (25%) of current balance, every single trade. No scaling by
   signal strength, whale count, or conviction of any kind
5. **Order executor** — buys the exact token the whales hold (no YES/NO inference needed — the
   whale's position tells us the side directly) via FOK market order

The bot **never sells**. Once a position is opened it is held until Polymarket resolves the
market on its own — no stop-loss, no take-profit, no manual exit under any circumstance,
regardless of how far the position moves against it.

## The only 5 rules the bot follows (all in `trade.py`)
1. `BET_FRACTION = 0.25` — bet exactly 25% of current account balance on every trade
2. No take-profit, no stop-loss, no cash-out — a position is held to resolution no matter what
3. No daily loss limit, no daily spend cap — the bot will keep betting 25% of whatever balance
   remains, cycle after cycle, for as long as consensus signals keep appearing
4. **Never make the exact same bet twice** — if the bot already holds (or has ever bought) a
   given side of a given market, a repeat signal for that same (market, side) is skipped
5. **Never bet the opposite outcome of a market already bet on** — if the bot holds YES on a
   market, a consensus signal for NO on that same market is skipped (and vice versa)

Rules 4 and 5 are checked against both the live Polymarket positions API and a local
persistent journal (`trader_trade_history.json`, see below) — the journal exists purely as a
redundant check in case the positions API hasn't caught up yet in the seconds right after a fill.

`ABSOLUTE_MIN_TRADE = $1` also exists, but it isn't a risk rule — it's Polymarket's own order
minimum. If 25% of balance is below $1, the bot bumps up to $1 (if the balance can cover it) or
skips (if it can't). This is an exchange constraint, not a choice.

**Everything that used to be here is gone**: no `MAX_OPEN_POSITIONS`, no `DAILY_SPEND_FRACTION`,
no `DAILY_LOSS_FRACTION`, no `TRADE_COOLDOWN_MINUTES`, no keyword correlation filter, no price
drift cap on signals, no scaled position sizing, no `maintain_positions()` / stop-loss / take-
profit of any kind. Do not resurrect any of it without an explicit new instruction — the whole
point of this iteration was to strip every rule down to the five above.

---

## Memory: Two Different Kinds

### 1. Dev-time memory (this file)
`CLAUDE.md` is read by **Claude Code** (the coding tool) at the start of each dev session.
It is NOT read by the running trading bot. It's documentation for the developer.

### 2. Runtime memory (shared agent state)
Running agents share state via files or in-memory data structures — not via CLAUDE.md.
`trader_trade_history.json` (project root) is a flat, ever-growing list of every trade the bot
has ever filled — used only to back the two dedup rules above:
```json
[
  {"token_id": "17522237181479319953...", "title": "Will X happen?", "side": "Yes"}
]
```
There's no daily reset and no expiry — an entry stays relevant for as long as the market it
refers to remains open (once a market resolves it can't be traded again anyway, so old entries
are harmless dead weight, not a correctness risk).

`whale_positions_state.json` persists each whale's positions between cycles so the bot can tell
a freshly-opened whale position (this cycle) from one they've held for a while.

---

## Future: Multi-Agent Architecture — SUPERSEDED, do not build

An earlier version of this doc proposed adding Scanner/Analyst/Forecaster/Risk/Post-mortem
LLM agents on top of the trading loop. As of 2026-08-03 the direction reversed twice: first to
**strictly leaderboard-driven with zero AI**, then further to **zero risk management beyond 5
explicit rules** — see Architecture above. All AI infrastructure (`executor.py`, `prompts.py`,
Chroma, news/enrichment connectors) and all scaled/adaptive risk logic (stop-loss, take-profit,
daily caps, cooldowns, correlation filtering, drift caps) have been deleted outright, not just
disconnected. Do not resurrect either plan without an explicit new decision from the user.

## Known Issues / To-Do
- [x] **Task #3** — Trade side logic fixed (`_resolve_trade` reads token IDs from selected market)
- [x] **Task #4** — Auth errors silenced (`derive_api_key()` + optional `CLOB_API_*` env vars)
- [x] **Task #7** — Log cleanup: batched prints, removed redundant lines, compact pipeline summary
- [x] **Task #8** — Trade size fix: bot was blocked at $9.40 balance (10% = $0.94 < $1 min); now checks `balance >= ABSOLUTE_MIN_TRADE` instead of `full_max >= ABSOLUTE_MIN_TRADE`
- [x] **Task #9** — Scoreboard premature LOSS fix: only resolve win/loss when `redeemable=True` (not just low price on open market)
- [x] **Task #11** — Whale leaderboard fixed: API ignores `window` param, removed multi-window fetching, now fetches once; numbered display with box formatting
- [x] **Task #12** — Removed debug print from `whale_tracker.py` `get_top_traders_from_leaderboard()`
- [x] **Task #13** — Updated display label from "all-time PnL" to "unrealized PnL"; updated docstring. Confirmed `pnl` = unrealized. All-time profit field still unknown — revisit if API changes.
- [x] **Task #5** — Discord webhook notifications added (`DISCORD_WEBHOOK_URL` env var on Railway); fires on trade filled / FOK killed / fresh whale signals
- [x] **Task #15** — Whale position tracking: positions persisted to `whale_positions_state.json` between cycles; fresh entries (new this cycle) flagged as NEW in logs; Discord alert when any whale opens a fresh position
- [x] **Task #16** — Three-window whale leaderboard: fetch top 10 from today, weekly, and all-time; auto-detect if `window` param works; fall back to trade-feed time filtering. Up to 30 unique wallets watched per cycle. Leaderboard box shows all three windows.
- [x] **Task #17** — Scrapped the AI market-scan pipeline from the entry path — bot began trading strictly off whale-leaderboard consensus. (Superseded by Task #19 below, which removed the risk-management layer this task still had.)
- [x] **Task #18** — Full repo strip-down to leaderboard-only, zero unused code (deleted `executor.py`, `prompts.py`, `creator.py`, `cron.py`, `chroma.py`, `news.py`, `data_enricher.py`, `search.py`, `gamma.py`, `agents/utils/`, tests; trimmed `polymarket.py`/`whale_tracker.py`; made position management mechanical). Also stripped repo-level scaffolding — `.github/` CI/templates, `CONTRIBUTING.md`, `.pre-commit-config.yaml`, `docs/`, unused `scripts/`, pruned `requirements.txt` 172→67 packages, rewrote `README.md`.
- [x] **Task #19** — Removed ALL remaining risk management. Deleted `maintain_positions`/`_close_position`/stop-loss/take-profit entirely — the bot now never sells a position under any circumstance. Deleted daily loss floor, daily spend cap, max open positions, trade cooldown, and the keyword correlation filter. Sizing changed from scaled (3%–10% by whale count/volume) to a flat 25% of balance on every trade. Replaced the correlation filter with two narrow, explicit rules: never bet the exact same (market, side) twice, never bet the opposite side of a market already held — both checked against live positions + a new persistent `trader_trade_history.json` journal. Removed the price-drift cap from `whale_tracker.py` (still computed and displayed, just no longer gates a signal). Interval changed from 60min/30min-default to 15min. Removed now-dead `Polymarket.get_last_trade_minutes_ago`/`get_midpoint_price`/`get_held_token_ids`, and `trade_log.log_lesson`/`get_recent_lessons`/`FORECASTER_LOG_FILE` (fed by the now-deleted `maintain_positions`). State file renamed `trader_daily_state.json` → `trader_trade_history.json` (no more daily reset — it's a permanent trade journal now, not a per-day budget tracker). Verified the new dedup/opposite-outcome logic against 7 scripted scenarios (clean trade, same-bet-blocked, opposite-blocked, journal-fallback-blocked, second-signal-picked-when-first-blocked, balance-too-low, bumped-to-$1-minimum) before shipping.
- [ ] **Task #6** — Multi-agent architecture — SUPERSEDED, see note above. Do not build without an explicit new decision to reintroduce AI.

---

## Whale API Notes (important for Task #12/#13)

### `/v1/leaderboard` field meanings
- `pnl` — **unrealized PnL on current open positions** (NOT all-time profit — fluctuates daily)
  - LaBradfordSmith22: $651k → $622k → $566k within same day → confirmed volatile
  - Real top traders (surfandturf ~$3M all-time, bossoskil1 ~$2.87M) show only $100k–$600k here
- `vol` — **always 0** (confirmed from raw dump 2026-05-28). Field is present but unpopulated. Useless.
- `profileImage` — excluded from display (too long)
- Raw dump (2026-05-28 #1 entry): `{'rank': '1', 'proxyWallet': '0x4924...', 'vol': 0, 'pnl': 278004.60}`
  - All-time profit field remains unidentified — `pnl` (unrealized) is the best available sort key.

### `/v1/leaderboard` API quirks
- `window` param (`1d`, `1w`, etc.): may be silently ignored — code auto-detects by comparing top-1 address. If ignored, falls back to filtering `/trades` feed by timestamp for daily/weekly top traders.
- `sortBy` param: `pnl` works; `profitAndLoss` also accepted but returns same unrealized field
- No API key required; `User-Agent` header helps avoid 403s
- `/trades?limit=5000` fetches global trade feed; filtered by timestamp for daily/weekly active traders when leaderboard window param is non-functional

### Inspect script
`scripts/inspect_whale.py` — one-shot profiler for any Polymarket address:
```
python scripts/inspect_whale.py 0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a
```
Shows: raw leaderboard entry (all fields), open positions sorted by value, recent trades.
Default address: bossoskil1 (`0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a`).

---

## Environment / Railway Notes

### Network policy (Cloud environment)
Claude Code sessions on code.claude.com run in isolated containers. By default, outbound
network is **None** (all blocked). To allow Polymarket API access:
1. Go to code.claude.com → Environment settings → Network policy
2. Set to **Custom** and add: `*.polymarket.com`, `data-api.polymarket.com`, `clob.polymarket.com`
3. **Takes effect on next session start** — not current session
4. Without this, all `requests.get(...)` to Polymarket return 403

### Railway deployment
- **Update the Railway start command to `--interval-minutes 15`** — this repo change updated
  `cli.py`'s default, but Railway's configured start command overrides the default and needs to
  be updated manually in the Railway dashboard.
- Container restarts don't lose state: `trader_trade_history.json` persists on Railway volume
- Logs are collected by Railway's log aggregator — rapid-fire `print()` calls get reordered
  - **Fix**: batch entire log sections into single `print("\n".join([...]))` call

---

## Log Format Reference (current)

There is no market-scan pipeline flow line anymore — the whale leaderboard/signals boxes below
(printed every cycle) are the entire signal-discovery log output, followed directly by
`Selected: "..."` / `Signal: N whale(s) ...` for the chosen trade, if any.

Per-cycle summary box (single batched print):
```
  ┌─ CYCLE SUMMARY ───────────────────────────────────────
  │  Balance : $9.40
  │  Positions: 3 open
  │  Score   : 0W - 0L - 0P  (0% win rate)  P&L: +$0.00  [0 pending]
  │  Trades  : 1 attempts | 0 filled | 1 FOK killed
  └───────────────────────────────────────────────────────
```

Whale signals box (single batched print):
```
  ┌─ WHALE SIGNALS ── N consensus signal(s) from leaderboard scan
  │  ...
  └─────────────────────────────────────────────────────
```

Whale leaderboard box (single batched print):
```
  ┌─ WHALE LEADERBOARD ──────────────────────────────────────────────────────
  │  TODAY   (unrealized): ...
  │  WEEKLY  (7d profit):  ...
  │  MONTHLY (30d profit): ...
  │  ALL-TIME:             ...
  └──────────────────────────────────────────────────────────────────────────
```
