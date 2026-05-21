# Polymarket Trading Bot — Project Notes

## Repo & Deploy
- **Repo:** github.com/alexkavalec/agents (forked from Polymarket/agents)
- **Host:** Railway, runs 24/7. Start command: `python cli.py run-loop --interval-minutes 60`
- **Stack:** Python 3.9, py-clob-client-v2, OpenAI (GPT), Chroma RAG
- **Branch for new work:** `claude/polymarket-trading-bot-*` → PR → squash merge to `main`

## Key Files
- `cli.py` — run-loop entry point
- `agents/application/trade.py` — orchestration + ALL guardrails
- `agents/application/executor.py` — OpenAI calls, RAG, price enrichment, market mapping
- `agents/polymarket/polymarket.py` — CLOB client wrapper, auth, balance, order execution
- `agents/polymarket/gamma.py` — Gamma market metadata client

## Current Architecture (simple, single-agent pipeline)
One `Executor` object runs everything sequentially each hour:

1. **Market scanner** — fetches ~50 events from Gamma API
2. **RAG filter** — Chroma vector search narrows to 4 events
3. **Market mapper** — expands to ~17 markets with live CLOB prices (114 skipped — no usable prices)
4. **AI market selector** — GPT picks 1 best market
5. **Superforecaster** — GPT assigns probability to the chosen outcome
6. **Trade constructor** — GPT outputs price/size/side
7. **Risk checker** — guardrails in trade.py (edge, spend, loss, position cap, cooldown, dedup)
8. **Order executor** — CLOB client places the order if risk passes

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

## Guardrails (all in trade.py)
- `MIN_EDGE = 0.10` — only trade if |bot estimate − market price| ≥ 0.10
- `MAX_TRADE_FRACTION = 0.10` — max 10% of balance per trade
- `MAX_OPEN_POSITIONS = 5` — enforced via data-api.polymarket.com/positions
- `DAILY_SPEND_FRACTION = 0.30` — stop after spending 30% of day-start balance
- `DAILY_LOSS_FRACTION = 0.15` — halt for the day if balance drops 15%
- `TRADE_COOLDOWN_MINUTES = 55` — min gap between trades; reads activity API (survives redeploys)
- Dedup — skips if token already held in open positions (reads positions API)
- State file: `trader_daily_state.json` in project root

## Known Issues / To-Do
- [x] **Task #3** — Trade side logic fixed (`_resolve_trade` reads token IDs from selected market)
- [x] **Task #4** — Auth errors silenced (`derive_api_key()` + optional `CLOB_API_*` env vars)
- [ ] **Task #5** — Discord webhook notifications when bot trades or hits a guardrail
- [ ] **Task #6** — Multi-agent architecture (see above — do after ~2 weeks of stable trading)
