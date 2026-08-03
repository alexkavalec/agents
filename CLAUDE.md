# Polymarket Trading Bot — Project Notes

## Repo & Deploy
- **Repo:** github.com/alexkavalec/agents (forked from Polymarket/agents)
- **Host:** Railway, runs 24/7. Start command: `python cli.py run-loop --interval-minutes 60`
- **Stack:** Python 3.9, py-clob-client-v2, OpenAI (GPT), Chroma RAG
- **Branch for new work:** `claude/polymarket-trading-bot-*` → PR → squash merge to `main`

## Key Files
- `cli.py` — run-loop entry point
- `agents/application/trade.py` — orchestration + ALL guardrails
- `agents/connectors/whale_tracker.py` — leaderboard scraping + consensus signal generation (the bot's SOLE trade signal source)
- `agents/application/executor.py` — OpenAI calls used by `maintain_positions` (take-profit re-eval) and by non-trading CLI commands (`ask_llm`, `create_market`, etc.); no longer used for entry decisions
- `agents/polymarket/polymarket.py` — CLOB client wrapper, auth, balance, order execution
- `agents/polymarket/gamma.py` — Gamma market metadata client (still used by `create_market`/`Creator`, not by the trading loop)

## Current Architecture — whale-leaderboard-only (as of 2026-08-02)
The bot no longer scans markets, runs RAG, or asks GPT to pick trades. Entry decisions
come **strictly from Polymarket leaderboard consensus**. `Trader.one_best_trade()` each cycle:

1. **Guardrail pre-checks** — daily loss floor, daily spend cap, max open positions, cooldown (unchanged)
2. **Whale scan** — `WhaleTracker.get_whale_signals()` scrapes the today/weekly/monthly/all-time
   profit leaderboards (`polymarket.com/leaderboard/overall/{window}/profit`), pulls open positions
   for every unique trader across all four windows, and buckets them by `(token, side)`
3. **Consensus signal** — a `(market, side)` bucket becomes a candidate signal only if
   `MIN_WHALES_AGREE` (2+) independent whales hold it and price hasn't drifted more than
   `MAX_PRICE_DRIFT` (20%, tightened from 40%) from their average entry
4. **Signal selection** — signals are pre-sorted (fresh first, then whale count, then combined
   $ volume/profit); the bot walks the list and picks the first signal not already held, not
   traded today, and not keyword-correlated with an existing open position
5. **Sizing** — scales with signal strength: `count_factor` (whales agreeing, saturates at 5) and
   `volume_factor` (combined $ profit/volume, saturates at $2M) are averaged into a fraction
   between `MIN_TRADE_FRACTION` (3%) and `MAX_TRADE_FRACTION` (10%); fresh signals (a whale that
   just opened this cycle) get a 1.25x bonus, still capped at 10%
6. **Order executor** — buys the exact token the whales hold (no YES/NO inference needed —
   the whale's position tells us the side directly) via FOK market order if risk passes
7. **Position management** (`maintain_positions`, unchanged) — still runs every cycle: unconditional
   -60% stop-loss, +60% take-profit gated on a GPT re-eval showing edge has collapsed. This is the
   one place GPT still runs in the trading loop.

There is no AI market scan, RAG filter, AI market selector, superforecaster, or trade constructor
in the entry path anymore — see git history on `agents/application/trade.py` /
`agents/application/executor.py` for the old pipeline. `Executor`'s market-scanning methods
(`filter_events_with_rag`, `map_filtered_events_to_markets`, `filter_markets`) still exist and are
used by the unrelated `create_market` CLI command (`Creator` class) — do not remove them for that
reason, but they are dead code for the trading loop itself.

---

## Memory: Two Different Kinds

### 1. Dev-time memory (this file)
`CLAUDE.md` is read by **Claude Code** (the coding tool) at the start of each dev session.
It is NOT read by the running trading bot. It's documentation for the developer.

### 2. Runtime memory (shared agent state)
Running agents share state via files or in-memory data structures — not via CLAUDE.md.
Current runtime state lives in `trader_daily_state.json` (project root):
```json
{
  "date": "2026-05-21",
  "start_balance": 19.38,
  "spent": 1.94,
  "last_trade_time": "2026-05-21T05:18:52",
  "traded_tokens": ["17522237181479319953..."]
}
```
Cooldown and dedup also use live Polymarket API calls (`data-api.polymarket.com/activity`
and `/positions`) so guards survive Railway container restarts.

---

## Future: Multi-Agent Architecture (saved for later)

Multiple Claude agents can live in **one repo** and coordinate via shared state files.
Each agent is just a Python class calling `anthropic.Anthropic()` with its own model + system prompt.
The orchestrator (`trade.py`) calls them in sequence or in parallel via `asyncio`.

### Proposed file layout
```
agents/
  application/
    trade.py              ← orchestrator (already exists)
    executor.py           ← single agent today (gets split into below)
    agents/
      scanner.py          ← haiku, fast parallel market scanning
      analyst.py          ← opus, deep per-market research
      forecaster.py       ← opus, probability + confidence intervals
      risk.py             ← opus, Kelly sizing + portfolio correlation
      postmortem.py       ← haiku, scores past trades after resolution
  memory/
    trade_history.json    ← every trade logged with outcome
    forecaster_log.json   ← postmortem feeds lessons back to forecaster
```

### Proposed agents

| Agent | Model | Role |
|---|---|---|
| **Scanner** | `claude-haiku-4-5-20251001` | Parallel scanning across event categories |
| **Analyst** | `claude-haiku-4-5-20251001` | Per-market research — news, base rates |
| **Forecaster** | `claude-opus-4-7` | Probability with confidence intervals |
| **Risk** | `claude-opus-4-7` | Kelly sizing, portfolio exposure, correlation |
| **Executor** | — | Order placement (no LLM needed, pure logic) |
| **Post-mortem** | `claude-haiku-4-5-20251001` | Scores predictions, writes to forecaster_log.json |

### What multi-agent would fix vs today
- **No post-mortem loop** — bot never learns from past trades; each cycle starts fresh
- **No parallel research** — markets evaluated one at a time, sequentially
- **No persistent memory** — no record of what worked/didn't across cycles
- **Risk is if-statements** — not a reasoning agent, can't adapt to novel situations
- **Single point of failure** — one GPT call chain, no cross-checking between agents

### When to build it
Wait until the single-agent pipeline is stable and trading correctly for ~1–2 weeks.
The bottleneck right now is trade quality, not agent specialization.

---

## Guardrails (all in trade.py, unless noted)
- `MAX_TRADE_FRACTION = 0.10` / `MIN_TRADE_FRACTION = 0.03` — trade size scales with whale
  signal strength between these bounds (see Architecture above)
- `WHALE_COUNT_SATURATION = 5`, `WHALE_VOLUME_SATURATION = 2_000_000`, `FRESH_SIGNAL_BONUS = 1.25`
  — sizing inputs
- `MIN_WHALES_AGREE = 2` (in `whale_tracker.py`) — minimum independent whales required for a signal
- `MAX_PRICE_DRIFT = 0.20` (in `whale_tracker.py`) — reject signals whose price has drifted
  more than 20% from the whales' average entry (tightened from 40%)
- `MAX_OPEN_POSITIONS = 25` — enforced via data-api.polymarket.com/positions
- `DAILY_SPEND_FRACTION = 0.30` — stop after spending 30% of day-start balance
- `DAILY_LOSS_FRACTION = 0.15` — halt for the day if balance drops 15%
- `TRADE_COOLDOWN_MINUTES = 55` — min gap between trades; reads activity API (survives redeploys)
- Dedup — skips if token already held in open positions or already traded today (reads positions API)
- Correlation filter — skips signals sharing ≥2 keywords with an existing open position's title
- Position management (unchanged) — unconditional -60% stop-loss; +60% take-profit gated on GPT
  re-eval showing edge has collapsed
- State file: `trader_daily_state.json` in project root

## Known Issues / To-Do
- [x] **Task #3** — Trade side logic fixed (`_resolve_trade` reads token IDs from selected market)
- [x] **Task #4** — Auth errors silenced (`derive_api_key()` + optional `CLOB_API_*` env vars)
- [x] **Task #7** — Log cleanup: batched prints, removed redundant lines, compact pipeline summary
- [x] **Task #8** — Trade size fix: bot was blocked at $9.40 balance (10% = $0.94 < $1 min); now checks `balance >= ABSOLUTE_MIN_TRADE` instead of `full_max >= ABSOLUTE_MIN_TRADE`
- [x] **Task #9** — Scoreboard premature LOSS fix: only resolve win/loss when `redeemable=True` (not just low price on open market)
- [x] **Task #10** — Correlation stop words expanded: added "meeting", month names, "presidential", "candidate", "winner" to `_CORR_STOP`
- [x] **Task #11** — Whale leaderboard fixed: API ignores `window` param, removed multi-window fetching, now fetches once; numbered display with box formatting
- [x] **Task #12** — Removed debug print from `whale_tracker.py` `get_top_traders_from_leaderboard()`
- [x] **Task #13** — Updated display label from "all-time PnL" to "unrealized PnL"; updated docstring. Confirmed `pnl` = unrealized. All-time profit field still unknown — revisit if API changes.
- [x] **Task #14** — Added "election", "elections", "elect", "elected", "vote", "votes", "voting" to `_CORR_STOP` to prevent cross-country election markets from triggering false correlation blocks
- [x] **Task #5** — Discord webhook notifications added (`DISCORD_WEBHOOK_URL` env var on Railway); fires on: trade filled, FOK killed, daily loss limit, daily spend cap, max positions, stop loss, take profit, fresh whale signals
- [x] **Task #15** — Whale position tracking: positions persisted to `whale_positions_state.json` between cycles; fresh entries (new this cycle) flagged as NEW in logs; fresh signals score 2 vs 1 in market boost; Discord alert when any whale opens a fresh position
- [x] **Task #16** — Three-window whale leaderboard: fetch top 10 from today, weekly, and all-time; auto-detect if `window` param works; fall back to trade-feed time filtering. Up to 30 unique wallets watched per cycle. Leaderboard box shows all three windows.
- [x] **Task #17** — Scrapped the AI market-scan pipeline (RAG filter, AI market selector, superforecaster, trade constructor) from the entry path. The bot now trades strictly off whale-leaderboard consensus (today/weekly/monthly/all-time) — buys the exact token whales hold, no YES/NO inference needed. Sizing scales with whale count + combined $ volume/profit (3%–10% of balance, fresh-signal 1.25x bonus). Drift cap tightened 40% → 20%. Daily spend/loss caps, max positions, cooldown, dedup, correlation filter, and stop-loss/take-profit position management are all unchanged. GPT is only still called for the take-profit re-eval in `maintain_positions`.
- [ ] **Task #6** — Multi-agent architecture (see above — do after ~2 weeks of stable trading; less relevant now that entries are mechanical, but post-mortem/lesson loop could still improve the take-profit re-eval)

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
- Container restarts don't lose state: `trader_daily_state.json` persists on Railway volume
- Cooldown and dedup use live API calls so they survive restarts even without the file
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
  │  Balance : $9.40  (start $9.40)
  │  Spent   : $0.00 / $2.82 daily cap
  │  Positions: 3 / 5 max
  │  Score   : 0W - 0L - 0P  (0% win rate)  P&L: +$0.00  [0 pending]
  │  Trades  : 1 attempts | 0 filled | 1 FOK killed | 0W 0L
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
  ┌─ WHALE LEADERBOARD ── top N traders by all-time PnL
  │   1.  $   566,239  LaBradfordSmith22
  │   2.  $   ...
  └─────────────────────────────────────────────────────
```
