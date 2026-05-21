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

1. **Market scanner** — fetches ~50 events + ~1000 markets from Gamma API
2. **RAG filter** — Chroma vector search narrows to 4 events
3. **Market mapper** — expands to ~131 markets, enriches with live CLOB prices
4. **AI market selector** — GPT picks 1 best market from 131
5. **Superforecaster** — GPT assigns probability to the chosen outcome
6. **Trade constructor** — GPT outputs price/size/side
7. **Risk checker** — guardrails in trade.py (edge, spend, loss, position cap)
8. **Order executor** — CLOB client places the order if risk passes

---

## Future: Multi-Agent Architecture (saved for later)

The current pipeline is one GPT call chain. A proper multi-agent version would split
this into specialized agents running in parallel with structured handoffs:

### Proposed agents

| Agent | Role |
|---|---|
| **Scanner** | Parallel market research across multiple event categories simultaneously |
| **Analyst** | Per-market deep research — news, base rates, expert opinion |
| **Forecaster** | Probability assignment with confidence intervals, not just a point estimate |
| **Risk** | Dedicated reasoning agent: portfolio exposure, correlation, Kelly sizing |
| **Executor** | Order placement, slippage monitoring, partial fill handling |
| **Post-mortem** | After each market resolves, scores the prediction, updates strategy memory |

### What's missing from current setup that multi-agent would fix
- **No post-mortem loop** — bot never learns from past trades; each cycle starts fresh
- **No parallel research** — markets evaluated one at a time, sequentially
- **No persistent memory** — no record of what worked/didn't across cycles
- **Risk is if-statements** — not a reasoning agent, can't adapt to novel situations
- **Single point of failure** — one GPT call chain, no cross-checking between agents

### Implementation notes (when ready)
- Use the Anthropic Claude API with `claude-opus-4-7` for high-stakes reasoning agents (forecaster, risk)
- Use `claude-haiku-4-5-20251001` for high-volume scanning/filtering tasks
- Persist post-mortem results to a simple SQLite or JSON log so forecaster can reference history
- Consider the Claude Agent SDK for orchestration once complexity warrants it

---

## Guardrails (all in trade.py)
- `MIN_EDGE = 0.10` — only trade if |bot estimate − market price| ≥ 0.10
- `MAX_TRADE_FRACTION = 0.10` — max 10% of balance per trade
- `MAX_OPEN_POSITIONS = 5` — enforced via data-api.polymarket.com/positions
- `DAILY_SPEND_FRACTION = 0.30` — stop after spending 30% of day-start balance
- `DAILY_LOSS_FRACTION = 0.15` — halt for the day if balance drops 15%
- Daily state persisted in `/tmp/trader_daily_state.json`

## Known Issues / To-Do
- [ ] **Task #3** — Improve trade sizing/side logic in `source_best_trade` (crude original logic)
- [ ] **Task #4** — Silence cosmetic `Could not create api key` auth errors (feed real API creds as env vars)
- [ ] **Task #5** — Optional monitoring (Discord webhook or simple dashboard)
