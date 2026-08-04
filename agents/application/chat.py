"""
Dashboard chat box — a casual, conversational assistant that can chat about anything,
but has exactly two ways to actually change something, and neither one executes
without an explicit user confirm click in the UI:

1. propose_override — a change to one of three TODAY-scoped mechanical trading
   knobs (see overrides.py): focus_trader, trade_count_cap, size_override_usd.
2. propose_manual_trade — a specific trade the user described in words. Claude
   never picks the market itself; it gives a search query + desired side, which
   market_search.py resolves against real, currently-open Polymarket markets.
   If that resolves to more than one plausible market, chat.py asks the user to
   pick rather than guessing — a wrong-market or wrong-side execution here is
   real money, not a cosmetic mistake.

Either way, Claude only ever proposes; trade.py's place_manual_trade() and
overrides.save_override() are what actually do something, and only after the
user confirms in the UI. It's given a read-only snapshot of the bot's live state
(balance, positions, scoreboard, leaderboard, active overrides) so it can answer
real questions instead of just parsing commands, and a short slice of recent
conversation history so follow-ups ("the second one") work naturally.
"""

import os
import uuid

import anthropic

from agents.connectors.market_search import search_markets, is_relevant

MODEL = "claude-sonnet-5"
MAX_HISTORY_MESSAGES = 12

BASE_SYSTEM_PROMPT = """You're the friendly assistant living in the chat box of a Polymarket \
whale-copying trading bot's dashboard. Talk like a normal, casual, helpful person — not a \
formal command parser. Chat, joke around, explain things, answer questions, whatever the \
person's actually asking for. No need to be stiff or robotic about it.

Here's the honest deal on what you can actually DO, though — it matters because real money \
moves through this. The bot trades automatically based on whale-leaderboard consensus \
(copying markets where 2+ independent top traders hold the same position). You have two real \
levers, and neither one takes effect until the person clicks Confirm in the UI:

1. propose_override — adjust one of three things for the rest of the current UTC day:
   - focus_trader_query: copy only one specific whale today (skips the normal 2+ whale \
agreement requirement just for that trader)
   - trade_count_cap: stop trading once N trades fill today
   - size_override_usd: flat dollar amount instead of the normal 25%-of-balance sizing
   A single request can set more than one at once (e.g. "do 1 trade of $20 today" = \
trade_count_cap 1 + size_override_usd 20 together).

2. propose_manual_trade — place a specific trade someone describes, e.g. "buy $30 of yes on \
the lakers game" or "put $50 on trump winning". Give your best search text for the market \
(market_query) plus which side they want (side) and the dollar amount (amount_usd) — the \
system will look up the real, currently open market for you; you don't need to know exact \
titles or prices. If it can't find a confident match, or finds more than one plausible market, \
you'll get a list back to relay to the person so they can clarify — don't guess between them \
yourself, and don't call the tool again until they've told you which one.

Always include a short, casual one-sentence "summary" with either tool describing exactly what \
will happen, since that's what gets shown for confirmation.

You can't sell or close a position, can't give market predictions or trading advice, and can't \
do anything beyond those two tools — if someone asks for that, just say so honestly and \
casually, don't fake it or call a tool anyway. Otherwise, just talk normally: answer questions \
about their balance, positions, the whale leaderboard, or what the bot's doing using the state \
snapshot below, explain how any of this works, or just have a normal conversation. You're \
allowed to have a personality."""

TOOLS = [
    {
        "name": "propose_override",
        "description": "Propose a change to today's mechanical trading behavior for the user to confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus_trader_query": {
                    "type": "string",
                    "description": "Name or partial name of the one whale to copy exclusively today, if requested.",
                },
                "trade_count_cap": {
                    "type": "integer",
                    "description": "Max number of trades to make today, if requested.",
                },
                "size_override_usd": {
                    "type": "number",
                    "description": "Flat dollar size for today's trade(s) instead of 25% of balance, if requested.",
                },
                "summary": {
                    "type": "string",
                    "description": "One plain-English sentence summarizing exactly what this override will do.",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "propose_manual_trade",
        "description": "Propose placing a specific trade the user described, for the user to confirm before it's actually executed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market_query": {
                    "type": "string",
                    "description": "Search text to find the market — the event, teams, or question the user described.",
                },
                "side": {
                    "type": "string",
                    "description": "Which outcome to buy, in the user's own words (e.g. 'yes', 'no', a team name).",
                },
                "amount_usd": {
                    "type": "number",
                    "description": "Dollar amount to spend.",
                },
                "summary": {
                    "type": "string",
                    "description": "One casual sentence summarizing the trade for confirmation.",
                },
            },
            "required": ["market_query", "side", "amount_usd", "summary"],
        },
    },
]


def _resolve_trader(query: str, known_traders: list):
    """Fuzzy-match a chat-provided name against this cycle's cached whale traders
    (name or address, case-insensitive substring). Returns (match_dict_or_None,
    error_message_or_None)."""
    q = (query or "").strip().lower()
    if not q:
        return None, "No trader name given."
    matches = [
        t for t in known_traders
        if q in (t.get("name") or "").lower() or q in (t.get("address") or "").lower()
    ]
    if not matches:
        known = sorted({t.get("name") or (t.get("address") or "")[:10] for t in known_traders})
        hint = ", ".join(known[:15]) if known else "(none scanned yet this cycle)"
        return None, f'No trader matching "{query}" in the current leaderboard scan. Known this cycle: {hint}.'
    if len(matches) > 1:
        names = sorted({t.get("name") or (t.get("address") or "")[:10] for t in matches})
        return None, f'"{query}" matches more than one trader ({", ".join(names)}) — can you be more specific?'
    t = matches[0]
    return {"address": t["address"], "name": t.get("name") or t["address"][:10] + "..."}, None


def _resolve_manual_trade(call: dict):
    """Search for the described market and resolve it to a single confident
    match. Returns (proposal_dict_or_None, error_message_or_None) — the error
    message doubles as the clarifying question shown back to the user when the
    match is ambiguous or not found, so the caller never has to guess."""
    query = (call.get("market_query") or "").strip()
    side_query = (call.get("side") or "").strip().lower()
    if not query:
        return None, "I need to know which market you mean."
    if not side_query:
        return None, "Which side/outcome do you want?"
    try:
        amount = float(call.get("amount_usd"))
    except (TypeError, ValueError):
        return None, "Couldn't parse the dollar amount."
    if amount <= 0:
        return None, "The amount has to be a positive dollar figure."

    results = search_markets(query, limit=10)
    candidates = [m for m in results if is_relevant(m["question"], query)]

    if not candidates:
        return None, f'Couldn\'t find an open market that clearly matches "{query}" — try naming it more specifically.'
    if len(candidates) > 1:
        listing = "; ".join(f'"{m["question"]}"' for m in candidates[:5])
        return None, f'Found more than one open market matching that — which did you mean? {listing}'

    market = candidates[0]
    outcome_idx = None
    for i, outcome in enumerate(market["outcomes"]):
        if side_query in outcome.lower() or outcome.lower() in side_query:
            outcome_idx = i
            break
    if outcome_idx is None:
        options = ", ".join(market["outcomes"])
        return None, f'"{market["question"]}" has outcomes {options} — which one did you mean by "{call.get("side", "")}"?'

    proposal = {
        "id": str(uuid.uuid4()),
        "manual_trade": {
            "market_title": market["question"],
            "condition_id": market["condition_id"],
            "side": market["outcomes"][outcome_idx],
            "token_id": market["token_ids"][outcome_idx],
            "price": market["outcome_prices"][outcome_idx],
            "amount_usd": round(amount, 2),
        },
        "summary": (call.get("summary") or "").strip()
        or f'Buy ${amount:.2f} of "{market["outcomes"][outcome_idx]}" on "{market["question"]}".',
    }
    return proposal, None


def _format_context(context: dict) -> str:
    """Turn a snapshot of the bot's live state into plain text for the system
    prompt — read-only reference material, not something Claude can act on
    except via the two tools above."""
    if not context:
        return ""
    lines = ["Current bot state (read-only reference — you can't change any of this except via the two tools above):"]

    balance = context.get("balance")
    if balance is not None:
        lines.append(f"- Balance: ${balance:,.2f}")
    lines.append(f"- Open positions: {context.get('open_positions_count', 0)}")

    sb = context.get("scoreboard") or {}
    if sb:
        lines.append(
            f"- Record: {sb.get('wins', 0)}W-{sb.get('losses', 0)}L-{sb.get('pushes', 0)}P, "
            f"P&L ${sb.get('total_pnl', 0):,.2f}, ROI {sb.get('roi_pct', 0)}%"
        )

    ov = context.get("active_overrides") or {}
    active = []
    if ov.get("focus_trader"):
        active.append(f"focused on {ov['focus_trader'].get('name')}")
    if ov.get("trade_count_cap") is not None:
        active.append(f"capped at {ov['trade_count_cap']} trades/day")
    if ov.get("size_override_usd") is not None:
        active.append(f"sized at ${ov['size_override_usd']}/trade")
    lines.append("- Active overrides: " + (", ".join(active) if active else "none"))

    lb = context.get("whale_leaderboards") or {}
    for window, label in (("today", "Today"), ("weekly", "Weekly")):
        entries = (lb.get(window) or [])[:5]
        if entries:
            names = ", ".join(f"{e.get('name', '?')} (${e.get('profit', 0):,.0f})" for e in entries)
            lines.append(f"- {label} leaderboard top {len(entries)}: {names}")

    return "\n".join(lines)


def handle_message(message: str, known_traders: list, context: dict = None, history: list = None) -> dict:
    """
    Returns {"reply": str, "proposal": dict | None}. A non-None proposal is
    ready to send to /api/overrides/confirm (has "focus_trader"/"trade_count_cap"/
    "size_override_usd") or /api/manual-trade/confirm (has "manual_trade") as-is
    if the user approves it — nothing is written to disk or executed here.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"reply": "Chat isn't configured yet — ANTHROPIC_API_KEY isn't set on the server.", "proposal": None}
    if not (message or "").strip():
        return {"reply": "Say something first.", "proposal": None}

    past = [
        {"role": h["role"], "content": str(h.get("content", ""))[:2000]}
        for h in (history or [])
        if h.get("role") in ("user", "assistant") and h.get("content")
    ][-MAX_HISTORY_MESSAGES:]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=700,
            system=BASE_SYSTEM_PROMPT + "\n\n" + _format_context(context),
            tools=TOOLS,
            messages=past + [{"role": "user", "content": message[:2000]}],
        )
    except Exception as e:
        return {"reply": f"Chat error: {e}", "proposal": None}

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    tool_calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    fallback_reply = " ".join(p.strip() for p in text_parts if p.strip()) or "I didn't catch an actionable request there."

    if not tool_calls:
        return {"reply": fallback_reply, "proposal": None}

    call_block = tool_calls[0]
    call = call_block.input

    if call_block.name == "propose_manual_trade":
        proposal, err = _resolve_manual_trade(call)
        if err:
            return {"reply": err, "proposal": None}
        return {"reply": " ".join(text_parts).strip() or proposal["summary"], "proposal": proposal}

    # propose_override
    proposal = {"id": str(uuid.uuid4())}
    errors = []

    if call.get("focus_trader_query"):
        trader, err = _resolve_trader(call["focus_trader_query"], known_traders)
        if err:
            errors.append(err)
        else:
            proposal["focus_trader"] = trader

    if call.get("trade_count_cap") is not None:
        try:
            n = int(call["trade_count_cap"])
            if n > 0:
                proposal["trade_count_cap"] = n
            else:
                errors.append("Trade count cap must be a positive number.")
        except (TypeError, ValueError):
            errors.append("Couldn't parse the trade count.")

    if call.get("size_override_usd") is not None:
        try:
            amt = float(call["size_override_usd"])
            if amt > 0:
                proposal["size_override_usd"] = round(amt, 2)
            else:
                errors.append("Trade size must be a positive dollar amount.")
        except (TypeError, ValueError):
            errors.append("Couldn't parse the dollar amount.")

    if errors:
        return {"reply": " ".join(errors), "proposal": None}

    if len(proposal) == 1:  # only "id" got set — nothing valid was actually proposed
        return {"reply": fallback_reply, "proposal": None}

    proposal["summary"] = (call.get("summary") or "").strip() or "Apply this change to today's trading."
    return {"reply": " ".join(text_parts).strip() or proposal["summary"], "proposal": proposal}
