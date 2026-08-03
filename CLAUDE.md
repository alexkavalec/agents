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
- `agents/application/dashboard.py` + `agents/application/dashboard_static/index.html` — read-only
  stats web dashboard (balance, open positions, trade history, P&L chart, whale leaderboards —
  click a trader to see their open positions in a modal), started by `run_loop` in a background
  thread. Stdlib `http.server` only, no new
  dependencies. Polls every 60s client-side; balance/positions/trades are live per request, whale
  leaderboard/position data comes from `whale_scan_cache.json` (written once per 15-min bot cycle,
  not re-scraped per dashboard request). Optional `DASHBOARD_TOKEN` gates it via `?key=` query
  param — see Environment/Railway Notes below.

Everything else from the original fork (Gamma market scanner, Chroma RAG, OpenAI-based
superforecaster/trade-constructor/market-selector, news/Twitter/Reddit/Wikipedia/Tavily
enrichment, the `create_market`/`Creator` market-creation feature, and their CLI commands)
has been **deleted**, not just disconnected — see git history if any of it needs resurrecting.
The bot only ever calls Polymarket's own `data-api.polymarket.com` and `clob.polymarket.com`
endpoints, plus a Discord webhook for notifications.

## Current Architecture — pure leaderboard-following, zero risk management (as of 2026-08-03)
The bot has no opinion of its own about any market. It watches what the top leaderboard
traders are doing and copies consensus moves, full stop. There is no AI, no market scan, no
stop-loss, no take-profit, no daily caps — the only guardrails are the six rules listed below,
including a signal-timing filter (rule 6) on top of the "did enough whales agree" consensus check.

`Trader.one_best_trade()` runs every 15 minutes:

1. **Whale scan** — `WhaleTracker.get_whale_signals()` fetches the top 10 traders on the
   today / weekly / monthly / all-time profit leaderboards (`polymarket.com/leaderboard/overall/
   {window}/profit`), pulls every open position each of those traders currently holds, and logs
   both boxes in full every cycle (leaderboard = each whale's record for that window, e.g. weekly
   = 7-day profit; signals = the positions found)
2. **Consensus signal** — a `(market, side)` becomes a candidate signal once `MIN_WHALES_AGREE`
   (2+, in `whale_tracker.py`) independent whales hold it. Nothing else gates a signal at this
   stage — no price drift cap, no minimum whale count beyond 2, no filtering by market category
   or liquidity. Each signal also carries `end_date` — the market's own resolution date, straight
   from Polymarket's `endDate` field on `/positions` (e.g. `"2026-08-17"`) — and `is_today_event`,
   whether that date is today (UTC). This is about when the *event* happens, not when the bot
   first noticed the position (that's `first_seen`/`is_fresh`, a separate, unrelated concept still
   used only for the `[NEW]` freshness tag in logs/Discord)
3. **Signal selection** — signals are pre-sorted (fresh first, then whale count, then combined
   $ volume/profit); the bot walks the list and picks the first one that clears rules 4-6 below
4. **Sizing** — flat `BET_FRACTION` (25%) of current balance, every single trade. No scaling by
   signal strength, whale count, or conviction of any kind
5. **Order executor** — buys the exact token the whales hold (no YES/NO inference needed — the
   whale's position tells us the side directly) via FOK market order

The bot **never sells**. Once a position is opened it is held until Polymarket resolves the
market on its own — no stop-loss, no take-profit, no manual exit under any circumstance,
regardless of how far the position moves against it.

## The only 6 rules the bot follows (all in `trade.py`)
1. `BET_FRACTION = 0.25` — bet exactly 25% of current account balance on every trade
2. No take-profit, no stop-loss, no cash-out — a position is held to resolution no matter what
3. No daily loss limit, no daily spend cap — the bot will keep betting 25% of whatever balance
   remains, cycle after cycle, for as long as consensus signals keep appearing
4. **Never make the exact same bet twice** — if the bot already holds (or has ever bought) a
   given side of a given market, a repeat signal for that same (market, side) is skipped
5. **Never bet the opposite outcome of a market already bet on** — if the bot holds YES on a
   market, a consensus signal for NO on that same market is skipped (and vice versa)
6. **Today's events only, unless overwhelming consensus** — a signal is only eligible if
   `is_today_event` is true (the market's own `end_date` is today, UTC — e.g. if it's August 17,
   only markets resolving August 17), *unless* `whale_count >= HIGH_CONSENSUS_WHALES` (5, in
   `trade.py`), in which case it's eligible regardless of when the market resolves. This keeps the
   bot focused on same-day events by default (daily sports games, same-day crypto-price markets,
   etc.) while still catching genuinely strong, overwhelming consensus on a market that resolves
   further out — e.g. 5+ independent whales already positioned ahead of a later event

Rules 4 and 5 are checked against both the live Polymarket positions API and a local
persistent journal (`trader_trade_history.json`, see below) — the journal exists purely as a
redundant check in case the positions API hasn't caught up yet in the seconds right after a fill.
Rule 6 is a signal-timing/eligibility filter, not risk management — it doesn't cap losses, it
just changes which signals are considered at all.

`ABSOLUTE_MIN_TRADE = $1` also exists, but it isn't a risk rule — it's Polymarket's own order
minimum. If 25% of balance is below $1, the bot bumps up to $1 (if the balance can cover it) or
skips (if it can't). This is an exchange constraint, not a choice.

**Everything that used to be here is gone**: no `MAX_OPEN_POSITIONS`, no `DAILY_SPEND_FRACTION`,
no `DAILY_LOSS_FRACTION`, no `TRADE_COOLDOWN_MINUTES`, no keyword correlation filter, no price
drift cap on signals, no scaled position sizing, no `maintain_positions()` / stop-loss / take-
profit of any kind. Do not resurrect any of it without an explicit new instruction — the whole
point of this iteration was to strip every risk-management rule down to rules 1-5 above (rule 6,
added later, is a signal-timing filter, not risk management — see note above).

---

## Memory: Two Different Kinds

### 1. Dev-time memory (this file)
`CLAUDE.md` is read by **Claude Code** (the coding tool) at the start of each dev session.
It is NOT read by the running trading bot. It's documentation for the developer.

### 2. Runtime memory (shared agent state)
Running agents share state via files or in-memory data structures — not via CLAUDE.md.
All state file paths below live under `DATA_DIR` (default `.`, the working directory) — see
Task #35 in the changelog and "Persistent state" under Railway Notes for why this matters and
how to actually make it survive redeploys (it doesn't, by default).

`trader_trade_history.json` is a flat, ever-growing list of every trade the bot
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
a freshly-opened whale position (this cycle) from one they've held for a while, via `first_seen`
per (whale, market, side) — used only for the `[NEW]`/`is_fresh` freshness tag in logs and
Discord alerts. It does **not** back rule 6 (that's `end_date`/`is_today_event`, sourced fresh
from Polymarket's `/positions` API every cycle, not persisted state) — `first_seen` is
bot-observation-relative (if the bot was offline when a position first appeared, `first_seen`
reflects whenever the bot next scanned it, not when the whale actually opened it), which is
exactly why it's the wrong signal for "does this event happen today."

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
- [x] **Task #20** — Added a read-only stats dashboard (`agents/application/dashboard.py` + `dashboard_static/index.html`), started in a background thread from `run_loop` on `$PORT`. Stdlib `http.server` only — deliberately no FastAPI/uvicorn re-added, given the whole point of Task #18 was cutting dependencies. Serves `/` (HTML page: balance, open positions, recent trades, W/L record) and `/api/stats` (same data as JSON), both gated by an optional `DASHBOARD_TOKEN` (`?key=` query param) since this exposes balance/positions/trade history if left public. Added `get_scoreboard_stats()` to `scoreboard.py` (structured dict version of the existing `get_scoreboard_line()` string) for the dashboard to consume. Verified with a real server-plus-headless-browser smoke test (mocked Polymarket, both with and without `DASHBOARD_TOKEN`, screenshot-checked the rendered page) before shipping — see git history for the test scripts (not committed, scratch-only).
- [x] **Task #21** — Dashboard v2: added a cumulative-P&L line chart (inline SVG, hover crosshair + tooltip, no charting library), ROI% card (`scoreboard.get_scoreboard_stats()` extended with `total_staked`/`roi_pct`; new `get_pnl_timeseries()`), the 4-window whale leaderboards, and a per-trader "Whale Open Positions" table. The leaderboard/whale-position data is **not** live-fetched per dashboard request — `WhaleTracker.get_whale_signals()` now writes `whale_scan_cache.json` (leaderboards + every scanned trader's current positions, with which leaderboard window(s) each appears on) at the end of every real scan (still on the bot's normal 15-minute cycle), and the dashboard just reads that file. This is why the frontend poll interval was bumped to 60s (`setInterval(refresh, 60000)` in `index.html`) without adding any new scraping load — the whale/leaderboard portion of the payload is only ever as fresh as the last bot cycle; the balance/positions/trade-history portion is genuinely live every poll. Verified with server+headless-browser smoke tests again, including simulating a pointer hover over the chart to confirm the crosshair/tooltip actually render with the right value.
- [x] **Task #22** — Fixed leaderboard scraping returning "(no data)" / "No traders found from any window" in production (confirmed live against polymarket.com on 2026-08-03). **Root cause: Polymarket changed how the leaderboard page embeds its data.** It used to be a plain JSON array directly in the SSR HTML; it's now a React Query "dehydrated state" cache inside a Next.js RSC flight chunk, serialized as an **escaped** JSON string (literal `\"` two-char sequences, not real quote characters) — so the old regex (which looked for unescaped `{"rank"`) matched zero bytes on the current page. Rewrote `_scrape_leaderboard_page()`: find the `\"queryKey\":[\"/leaderboard\",\"profit\"` marker for the window's profit-sorted query, then search **backwards** for its own preceding `\"data\":[` array — the serialized query object has `state` (containing `data`) *before* `queryKey`, so searching forward from the queryKey match finds the wrong query's data (e.g. the unrelated "biggestWins" widget, which is what the first debugging attempt did). Bracket-match to the array's end, un-escape, `json.loads`. Verified against live-fetched pages for all 4 windows (today/weekly/monthly/all) — each returns distinct, sensible rankings (e.g. all-time top trader completely different from today/weekly) — and ran the full `get_whale_signals()` pipeline end-to-end (real leaderboard fixtures + mocked positions) confirming consensus-signal detection and the `whale_scan_cache.json` write both work again. `re` import removed from `whale_tracker.py` (no longer used — the new parser doesn't need regex).
- [x] **Task #23** — Fixed a real gap the logs surfaced: when the top signal of a cycle couldn't actually execute (FOK killed, or Polymarket rejects the order because price has drifted outside its tradeable `[0.01, 0.99]` range — happened live: a signal at `cur_price` 0.792 had moved to an actual orderbook price of 0.995 by execution time, likely a fast-moving sports market near resolution), the bot gave up for the entire 15-minute cycle even when other eligible consensus signals existed. `one_best_trade()` now builds the full eligible-signal list up front and tries each **in order** until one fills, instead of stopping after the first attempt. New `"untradeable"` trade-log status (distinct from `fok_killed`) for the price-out-of-range case; `get_stats()` and the cycle-summary log line surface its count. Verified with a scripted scenario matching the exact production log (first signal rejected with `invalid price`, second one fills) — confirms exactly 2 order attempts, the correct statuses land in `trade_history.json`, only the successful trade is written to the dedup journal, and both attempts size off the same starting balance.
- [x] **Task #24** — Dashboard formatting pass. Fixed a real bug: market-question columns weren't the first column in the Recent Trades / Whale Positions tables, so the old CSS (`td:first-child { white-space: normal }`) never applied to them — long questions forced those rows wide instead of wrapping, unlike the Open Positions table where Market *is* first. Replaced with table-scoped `nth-child` rules targeting the actual question column in every table. Also: numeric columns (size/price/value/amount/P&L) are right-aligned with `tabular-nums`; trade status/outcome now render as human-readable labels (`fok_killed` → "FOK killed", `n/a` → "—") instead of raw snake_case; the whale-positions "Lists" column renders as small pill badges instead of a comma-joined string (this reused/renamed a CSS class — `.win-badge` — that had been defined back in Task #21 and never actually applied anywhere); Open Positions and Recent Trades are now wrapped in `.panel` cards like the other sections, for visual consistency; added table-row hover states, tightened card/section typography and spacing, and a small mobile breakpoint. Verified full-page and mobile-viewport screenshots, confirmed the fixed wrapping bug visually, and confirmed (by checking `scrollWidth` vs `clientWidth` directly, since Playwright's full-page screenshots don't expand nested horizontal-scroll containers) that tables which do need horizontal scroll on narrow screens still scroll correctly — that part was already working, not a bug.
- [x] **Task #25** — Dashboard readability pass on whale data: (1) address-style trader names (no pseudonym on the leaderboard API) now display truncated as `0x353563…feed` instead of the full 42-char hex string, in both the Whale Leaderboards and Whale Open Positions sections — new `displayName()` JS helper in `index.html`, falls back to the real pseudonym when one exists; (2) `whale_tracker.py`'s `get_whale_signals()` now tracks a `window_profit` dict per trader across all 4 leaderboard windows and stores `weekly_pnl` on each `trader_records` entry (written to `whale_scan_cache.json`); Whale Open Positions table gained a "Weekly P&L" column (green/positive, red/negative, `—` when the trader isn't on the weekly window) sourced straight from that field, no extra scraping; (3) `fmtUsd()` switched from `toFixed(2)` to `toLocaleString(...)` so every dollar figure across the whole dashboard (balance, positions, trades, whale values) now gets thousand-separator commas. Verified with a mocked-data server (stubbed `Polymarket` import to dodge this sandbox's web3/dotenv version mismatch, monkeypatched `_build_stats()`) driven by headless Chromium — screenshotted the leaderboard and whale-positions tables directly, confirmed truncated addresses, correct pos/neg coloring on Weekly P&L, `—` for missing records, and comma-formatted values everywhere (including a 7-figure balance/position value) with no layout regressions.
- [x] **Task #26** — Replaced the always-visible "Whale Open Positions" table with a click-to-view
  detail modal on the leaderboard entries themselves. Each row in the Whale Leaderboards — Top 10
  section is now a `<button class="lb-row" data-address="...">` (was a plain `<div>`) so it's
  natively keyboard-accessible (Enter/Space) with no extra JS; clicking/activating one opens a
  modal (`openTraderModal(address)` in `index.html`) that looks up the trader in the same
  `whale_traders` cache data by address and shows their weekly P&L, total position count/value,
  and a full positions table (market/side/avg/now/size/value) — same underlying data the deleted
  table used to dump in full for every trader at once, just scoped to one trader on demand. Modal
  closes via the × button, clicking the backdrop, or Escape. Handles the case where a trader is on
  a leaderboard but has no cached position record (e.g. zero qualifying open positions that cycle)
  with a plain empty state, not a misleading claim about leaderboard membership — an earlier draft
  said "not on a tracked leaderboard window," which was wrong for a trader who clearly *is* on one
  and just has no positions cached; simplified to leave that line blank when there's no window
  data instead of asserting something false. Removed the now-dead `#whale-positions-table`
  CSS/HTML/JS (`renderWhalePositions()`, `.scroll-y`, `.pnl-pos`/`.pnl-neg` — the pos/neg coloring
  moved onto the modal's reused `.value.pos`/`.value.neg` classes) and the `whale-positions-updated`
  timestamp element. `dashboard.py`'s `_build_stats()` was untouched — `whale_traders` was already
  a plain pass-through of `whale_scan_cache.json`, so the modal's per-address lookup needed no
  backend change. Verified with a mocked-data server + headless Chromium: confirmed the old table
  is gone, clicking a leaderboard row opens the modal with correct data, Escape/backdrop-click/×
  all close it, Enter on a keyboard-focused row opens it too, and the no-cached-positions trader
  renders the plain empty state instead of the old table row it would have been silently missing
  from before.
- [x] **Task #27** — Added rule 6: signals only eligible if `is_today` (some whale in the bucket
  first *observed* today, UTC), unless `whale_count >= HIGH_CONSENSUS_WHALES` (5). **Superseded
  by Task #28 below** — this interpreted "today" as bot-observation freshness (`first_seen`),
  which the user then clarified was wrong: they meant the market's own event/resolution date, not
  when the bot happened to notice the position. Left in the changelog for the record; the field
  and gating logic it added no longer exist as of Task #28.
- [x] **Task #28** — Corrected rule 6 per user clarification ("only make do positions on today's
  events, example if its august 17th, only open positions for august 17th, and so on"): replaced
  the `first_seen`-based `is_today` from Task #27 with `end_date`/`is_today_event`, sourced from
  the `endDate` field Polymarket's `/positions` API returns on every position (confirmed live via
  curl against real whale wallets — present on all 50/50 sampled positions, sports games and
  long-running markets alike, e.g. `"Knicks vs. Cavaliers" -> "2026-05-26"`). `end_date` is
  captured per-bucket in `whale_tracker.py`'s `get_whale_signals()` (truncated to `YYYY-MM-DD`,
  dropping any time-of-day component) and `is_today_event` is `end_date == today (UTC)`. This is
  a genuinely different signal from `first_seen`/`is_fresh` (kept, unchanged, for the `[NEW]`
  freshness tag only) — an event's resolution date doesn't move cycle to cycle, so unlike the old
  bot-observation-relative version, this doesn't depend on the bot having been running
  continuously. `trade.py`'s eligibility gate and log lines (cycle summary, `Selected:` line,
  "no eligible signal" message) all updated to reference `is_today_event`/`end_date` instead of
  the old fields; the override threshold (5+ whales) is unchanged from Task #27. Verified with a
  scripted `unittest.mock` scenario covering all three paths (future-dated event + low consensus
  → skipped; future-dated event + 5-whale consensus → eligible via override; today-dated event +
  low consensus → eligible on its own, default path unaffected) — same shape of test as Task #27,
  updated for the corrected field names and semantics.
- [x] **Task #29** — Dashboard Open Positions readability: (1) `fmtPrice()` in `index.html` now
  renders prices as cents (`55c`, or `55.5c` when there's sub-cent precision) instead of a bare
  decimal (`0.550`) — applies everywhere price is shown, including the whale trader modal's
  positions table, for consistency; (2) the Open Positions "Size" column (raw share count, which
  the user said they don't care about) is replaced with "Traded" — the actual dollar amount put
  into the position, sourced from Polymarket's `initialValue` field on `/positions` (added to
  `dashboard.py`'s `_build_stats()` as `amount_traded`) rather than `size * avg_price`, since
  `initialValue` is what Polymarket itself reports as the cost basis. Scoped to the bot's own
  Open Positions table only — the Whale Open Positions modal's "Size" column still shows share
  count, since that's about the scale of a whale's bet, not "how much did I trade." Verified with
  a mocked-data server + headless Chromium: confirmed `0.55` renders as `55c`, `0.555` as `55.5c`,
  and the Traded column shows the dollar amount (`$275.00`) instead of a raw share count.
- [x] **Task #30** — Open Positions: added a "To Win" column (net profit if the position resolves
  in our favor — `size * $1 payout - amount_traded`, computed in `dashboard.py`'s `_build_stats()`
  from Polymarket's `size`/`initialValue` fields, not exposed as raw `size` since the user said
  they don't care about share count) and color-coded the Value column by P&L: green when
  `current_value` is meaningfully above `amount_traded` (winning), red when meaningfully below
  (losing), grey/muted when within a cent of it (breakeven) — new `pnlClass()` helper and
  `.val-pos`/`.val-neg`/`.val-even` CSS classes in `index.html`. Verified with a mocked-data
  server + headless Chromium across all three states (winning/losing/breakeven), checking both
  the rendered CSS class and the actual computed text color on each Value cell.
- [x] **Task #31** — Extended Task #30's "To Win" column + P&L color-coding from the bot's own
  Open Positions to every whale's positions table in the trader detail modal (opened by clicking
  a leaderboard entry). `whale_tracker.py`'s `get_whale_signals()` now computes `amount_traded`
  (cost basis, from Polymarket's `initialValue`) and `to_win` (`size - amount_traded`) per whale
  position, mirroring `dashboard.py`'s bot-position fields exactly. `index.html`'s modal table
  reuses the same `pnlClass()` helper on its Value column and gained the same "To Win" column
  (kept "Size" here too, unlike the bot's own table — whale position size is informative about
  the scale of their bet, not something the user said they don't care about). Also confirmed the
  modal's "Weekly P&L" stat (a trader's weekly record, added back in Task #25) is already
  displayed prominently at the top of the modal — no change needed there, just verified it's
  showing correctly per the user's request to "see" it. Verified with a mocked-data server +
  headless Chromium: a trader with a winning/losing/breakeven position each showed the correct
  To Win amount and Value color (green/red/grey), and the Weekly P&L stat rendered correctly.
- [x] **Task #32** — Trader modal polish, from a real production screenshot (swisstony, 222
  positions): (1) the modal's top stats now match the main dashboard's own card style — reused
  the `.cards`/`.card` classes instead of the bespoke `.modal-stats`/`.modal-stat` ones, and
  expanded from a single "Weekly P&L" figure to one card per leaderboard window the trader is
  actually on (Today/Weekly/Monthly/All-Time, each pos/neg colored) plus Open Positions and Total
  Value — `whale_tracker.py`'s `get_whale_signals()` now exposes the full `window_profit` dict per
  trader instead of just `weekly_pnl`, replacing the now-redundant `windows` list (its badge
  rendering — `windowBadges()`/`.window-badge` — removed as dead code along with it); (2) the
  modal's positions table drops "Size" (raw share count) for "Traded" ($ cost basis, same
  `amount_traded` field added in Task #31), matching the bot's own Open Positions table exactly;
  (3) fixed a real overflow bug the screenshot exposed — the Side column showed long outcome
  names (e.g. "James Duckworth", not just Yes/No, since sports/player-prop markets have real
  player names as outcomes) that weren't allowed to wrap, forcing the row wide and triggering a
  left-right scrollbar the user didn't want. Side now wraps like Market does; `.modal-box` widened
  640px → 820px and the positions table's padding/font-size tightened to fit comfortably.
  Verified with a mocked-data server + headless Chromium reproducing the screenshot's exact shape
  (long player-name sides, 4 leaderboard windows including a negative all-time figure): confirmed
  `scrollWidth === clientWidth` on the table wrapper (no horizontal scrollbar) at 1300/1024/900/768px
  widths — true phone widths (~390px) still need the scrollbar, which is an acceptable, unavoidable
  fallback for 7 dense numeric columns on a screen that narrow, not a regression from before.
- [x] **Task #33** — Trader modal: fixed overflowing stat-card numbers, added a per-trader P&L
  chart. (1) The All-Time P&L card was showing a truncated number (`$22,892,345.` cut off
  mid-digit) because `.card .value` had no overflow handling and the box is a fixed
  `minmax(140px, 1fr)` grid cell — switched the modal's window-profit/total-value cards to
  `fmtCompactUsd()` (`$22.9M` instead of the full figure) with a `title` attribute holding the
  exact value on hover, and added `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`
  to `.card .value` globally as a safety net against any future overflow. (2) **Investigated
  "weekly record W-L-P" and confirmed it isn't available**: inspected every embedded React Query
  on a trader's own Polymarket profile page (`polymarket.com/profile/{address}`, same dehydrated-
  state format as the leaderboard page) — the only stats Polymarket computes for a trader are
  `trades`/`largestWin`/`joinDate` (`user-stats` query) and a $ P&L-over-time series
  (`portfolio-pnl` query, windows `1D`/`1W`/`1M`/`ALL`); no win/loss/push breakdown exists
  anywhere, for anyone. Also confirmed the bot's own `/positions` endpoint can't be used to
  reconstruct one: spot-checked a real wallet and found 500/500 "resolved" positions came back as
  losses and zero wins — winning positions are redeemed and disappear from the API once claimed,
  while worthless losing positions linger indefinitely as dust, making any position-based W/L
  count structurally biased toward losses, not a real record. Given no honest W-L-P is available,
  added the closest real substitute instead: a new `WhaleTracker.get_trader_pnl_history(address,
  window)` (in `whale_tracker.py`) scrapes the trader's own `portfolio-pnl` query the same way
  `_scrape_leaderboard_page()` scrapes the leaderboard, and a new `dashboard.py` endpoint
  (`GET /api/trader-pnl?address=...&window=...`, address validated against `^0x[a-fA-F0-9]{40}$`)
  serves it on demand. Deliberately **not** fetched during the bot's normal 15-minute scan cycle —
  a full profile-page HTML fetch (~300KB) per tracked whale every cycle would be a lot of
  unnecessary bandwidth for a chart nobody's looking at; instead the dashboard's `index.html`
  fetches it lazily only when a trader's modal is actually opened (with a request-sequence guard
  so a stale response can't overwrite a different trader's now-open modal), defaulting to the "1W"
  window per the user's "weekly record" framing. `renderPnlChart()` was refactored to accept an
  `opts` object (container/tooltip/empty element IDs, chart height) instead of hardcoded IDs, so
  the same chart code renders both the main dashboard's chart and the modal's — necessary since
  both can be mounted simultaneously and the SVG's internal element IDs would otherwise collide.
  Verified live against Polymarket (not mocked) that `get_trader_pnl_history()` returns real data
  for all 4 windows, then end-to-end with a mocked-data dashboard server + headless Chromium:
  confirmed no stat card overflows anymore (`scrollWidth === clientWidth` on every card value),
  the modal's chart renders and its hover crosshair/tooltip work independently of the main
  dashboard's own (separate, unaffected) chart instance.
- [x] **Task #34** — Added period tabs (Day/Week/Month/Year/YTD/All Time) to the trader modal's
  P&L chart. Task #33's initial `get_trader_pnl_history()` scraped the profile page's dehydrated
  state, which only ever had `1D`/`1W`/`1M`/`ALL` prefetched — no `1Y`/`YTD` data available that
  way at all. Found the real fix by grepping Polymarket's production JS chunks for the
  `"portfolio-pnl"` query's fetcher function, which led straight to the actual REST endpoint
  (`user-pnl-api.polymarket.com/user-pnl`, params `user_address`/`interval`/`fidelity`) — rewrote
  `get_trader_pnl_history()` to call it directly instead of scraping HTML, which is both simpler
  and supports all 6 windows the user asked for (see Whale API Notes below for the full endpoint
  writeup, including the fidelity-per-window mapping also pulled from the same JS). `dashboard.py`'s
  `/api/trader-pnl` endpoint needed no changes — it already passed `window` straight through.
  `index.html` gained a `.pnl-period-tabs` button row above the modal's chart; clicking a tab
  re-fetches and re-renders via a new `loadTraderPnl(address, window)` (extracted from the fetch
  logic Task #33 had inlined in `openTraderModal()`, now shared by both the initial load and every
  tab click), with the same request-sequence guard as before so a slow response for an abandoned
  tab/trader can't clobber a newer one. **Could not live-test the new endpoint from this dev
  sandbox** — its own egress policy blocks `user-pnl-api.polymarket.com` outright (confirmed via
  `/root/.ccr/README.md`, a 403 straight from the local agent proxy, unrelated to and stricter than
  the bot's actual Railway network policy) — so verification here relied on reading the production
  JS source directly rather than a live call, plus a mocked-backend Playwright test confirming the
  frontend correctly requests a distinct window per tab click, updates the active-tab styling, and
  re-renders the chart with that window's data. Flagged to the user to confirm it actually returns
  data once deployed to Railway.
- [x] **Task #35** — Fixed state getting wiped on every Railway deploy. Root cause: all four state
  files (`trade_history.json`, `trader_trade_history.json`, `whale_positions_state.json`,
  `whale_scan_cache.json`) were written to hardcoded, unconditional relative paths — Railway's
  container filesystem is ephemeral by default, so every redeploy (every PR merge) silently reset
  them to empty. An earlier version of this doc claimed state "persists on Railway volume," which
  was aspirational and never actually true — no volume had ever been attached. Every state-file
  path now resolves via a `DATA_DIR` env var (`os.path.join(DATA_DIR, "...")`, defaulting to `"."`
  — unchanged/ephemeral if unset, so this is a no-op until the user actually attaches a volume).
  Also removed a latent duplicate-source-of-truth bug found while fixing this: `scoreboard.py` had
  its own copy of `TRADE_HISTORY_FILE = "trade_history.json"` instead of importing it from
  `trade_log.py` — harmless while both were hardcoded to the same literal, but would have silently
  desynced the moment one of the two paths incorporated `DATA_DIR` and the other didn't. Fixing
  the code is only half the job — a Railway Volume still has to be created and mounted, and
  `DATA_DIR` set to that mount path, manually in the Railway dashboard; documented the exact steps
  in CLAUDE.md since this can't be done from the repo. Verified locally: with `DATA_DIR` set to a
  scratch directory, `trade_log.TRADE_HISTORY_FILE` and `scoreboard.TRADE_HISTORY_FILE` resolve to
  the identical path (confirming the import fixed the desync risk) and `log_trade()` actually
  writes there; with `DATA_DIR` unset, every path resolves exactly as before (`os.path.join("",
  "x") == "x"`), confirming zero behavior change for anyone who hasn't configured a volume yet.
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

### `/positions` win/loss bias (Task #33)
Do not use `/positions` (even with the `redeemable` filter removed) to compute a trader's win/
loss record — it's structurally biased toward losses. Winning positions get redeemed (claimed)
and disappear from the API once claimed; losing positions are worthless dust nobody bothers to
formally redeem, so they persist forever as `redeemable: true` entries with `cashPnl` equal to
`-initialValue`. Spot-checked live: one real wallet returned 500/500 "resolved" positions as
losses, zero wins. There is no reliable way to derive a win/loss/push record for an arbitrary
trader from Polymarket's public API — confirmed by inspecting every embedded query on their own
profile page (see below), none of which include one either.

### `user-pnl-api.polymarket.com/user-pnl` — per-trader P&L chart (Task #33/#34)
The real, directly-callable REST endpoint behind a trader's own profile-page chart —
found by grepping Polymarket's production Next.js JS chunks (`/_next/static/chunks/*.js`) for
the `"portfolio-pnl"` React Query key, which led to the actual `queryFn`:
`${USER_PNL_URL}/user-pnl?user_address=${address}&interval=${window.toLowerCase()}&fidelity=${fidelity}`,
`USER_PNL_URL` defaulting to `https://user-pnl-api.polymarket.com`. This **replaced** an earlier,
more fragile approach (Task #33's first cut) that scraped the same data out of the profile page's
escaped-JSON dehydrated state (same technique as `_scrape_leaderboard_page()`) — that only worked
for whatever windows the page happened to prefetch server-side (`1D`/`1W`/`1M`/`ALL`), which is
why Task #33 initially shipped without `1Y`/`YTD`. The direct endpoint accepts all six:
- `window` (as sent to `get_trader_pnl_history()`): `"1D"` / `"1W"` / `"1M"` / `"1Y"` / `"YTD"` /
  `"ALL"` — lowercased for the `interval` query param.
- `fidelity` (data-point spacing, also reverse-engineered from the same JS): `1D`→`1h`, `1W`→`3h`,
  `1M`→`18h`, everything else (`1Y`/`YTD`/`ALL`) → `1d`.
- Response: `[{"t": unix_seconds, "p": cumulative_pnl}, ...]` — same shape either way.
- **Could not be live-verified from inside a Claude Code sandbox session**: this dev environment's
  own egress policy blocks `user-pnl-api.polymarket.com` (403 from the local agent proxy, confirmed
  via `/root/.ccr/README.md` — a different, stricter restriction than the bot's own Railway network
  policy). Correctness here rests on reading the actual production JS source, not a live test.
  Verify this actually returns data once deployed; if Railway's own egress is ever restricted
  similarly, this host would need to be added there.
- `["user-stats", address]` (still only reachable via the profile-page scrape, not otherwise used)
  → `{"trades": int, "largestWin": float, "views": int, "joinDate": iso}` — no win/loss breakdown,
  just a trade count and single largest win.
- Other profile-page query keys present but unused (no realized-P&L-per-market breakdown in any):
  `/api/profile/marketsTraded`, `/api/profile/userData`, `/api/profile/volume`,
  `"positions","value"`, `"profile-biggest-wins"`, `"profile","positions",...,"CURRENT","DESC"`.

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
- Logs are collected by Railway's log aggregator — rapid-fire `print()` calls get reordered
  - **Fix**: batch entire log sections into single `print("\n".join([...]))` call
- **Stats dashboard**: enable "Public Networking" on the service to get a URL for it — Railway
  injects `$PORT`, which `dashboard.py` binds to automatically. Set `DASHBOARD_TOKEN` before
  doing this, or your balance/positions/trade history are readable by anyone with the URL.

### Persistent state (Task #35)
**Railway's container filesystem is ephemeral by default** — every redeploy (including every
PR merge, since that's what triggers a new deploy) starts from a fresh container, so any state
file written to the working directory (the old, unconditional `"trade_history.json"` etc. paths)
was silently wiped on every single deploy. This was a real bug, not a hypothetical: an earlier
version of this doc claimed state "persists on Railway volume," which was aspirational and never
actually true — no volume was ever attached, so trade history, the dedup journal, and whale
freshness tracking all reset to empty on every merge.

Fixed in two parts:
1. **Code**: every state file's path is now built from a `DATA_DIR` env var (default `"."`,
   i.e. unchanged/ephemeral behavior if unset) — `trade_log.TRADE_HISTORY_FILE`, `trade.
   STATE_FILE`, `whale_tracker.WHALE_STATE_FILE`, `whale_tracker.WHALE_CACHE_FILE` all resolve
   via `os.path.join(DATA_DIR, "...")`. `scoreboard.py` now imports `TRADE_HISTORY_FILE` from
   `trade_log.py` instead of redefining it (an earlier duplicate definition risked silently
   drifting out of sync with the real path).
2. **Railway config — you have to do this manually, it can't be done from code**:
   1. Railway dashboard → your service → **Settings → Volumes** → **Add Volume**
   2. Set a mount path, e.g. `/data` (any path works, just be consistent with step 3)
   3. Service → **Variables** → add `DATA_DIR=/data`
   4. Redeploy. From then on, every state file lives on the volume and survives future
      redeploys — only the *first* deploy after attaching the volume starts from empty (nothing
      to migrate; prior history was already being wiped every deploy anyway, so there's no old
      data to carry forward).

Without step 2, the code change alone does nothing — `DATA_DIR` stays unset, defaults to `"."`,
and behavior is unchanged (still ephemeral). This is deliberate: no volume should mean no
silent behavior change for anyone who hasn't set one up yet.

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
  │  Trades  : 1 attempts | 0 filled | 1 FOK killed | 0 untradeable
  └───────────────────────────────────────────────────────
```

If the top signal can't execute, the bot now tries the next eligible one in the same
cycle instead of stopping — logs `→ Trying next eligible signal (N more)...` between
attempts.

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
