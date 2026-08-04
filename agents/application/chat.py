"""
Dashboard chat box — translates a plain-English message into one of a small,
fixed set of TODAY-scoped trading overrides (see overrides.py) via a single
Claude tool-use call. Claude never picks markets, sizes trades on its own
judgment, or executes anything directly here — it only proposes a change,
returned to the caller for the user to confirm in the UI before it's written
to overrides.py's state file. The bot's own signal-following logic in
trade.py is unchanged; these three knobs are the entire surface area chat
can touch.
"""

import os
import uuid

import anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are a command parser for a mechanical Polymarket trading bot's \
dashboard chat box. The bot normally trades automatically based on whale-leaderboard \
consensus (2+ independent top traders holding the same position) — you do NOT pick \
markets, analyze odds, give trading advice, or place trades yourself. You ONLY \
translate the user's plain-English request into a small set of TODAY-scoped overrides \
to the bot's mechanical rules, which expire automatically at UTC midnight:

- focus_trader_query: copy only one specific whale's positions today, bypassing the \
normal 2+ whale consensus requirement for that trader alone
- trade_count_cap: stop trading once N trades have filled today
- size_override_usd: use a flat dollar amount instead of the normal 25%-of-balance \
sizing for today's trade(s)

If the request maps to one or more of these, call propose_override with the fields \
that apply — a single message can set more than one at once (e.g. "do 1 trade of $20 \
today" sets both trade_count_cap=1 and size_override_usd=20). Always include a \
one-sentence "summary" describing exactly what will happen, in plain English, for the \
user to confirm before it takes effect.

If the request is outside this scope (picking a specific market, market analysis, \
predictions, anything not one of the three overrides above) or too ambiguous to act \
on, do NOT call the tool — reply in plain text explaining what you can and can't do."""

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
    }
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


def handle_message(message: str, known_traders: list) -> dict:
    """
    Returns {"reply": str, "proposal": dict | None}. A non-None proposal is
    ready to send to /api/overrides/confirm as-is if the user approves it —
    nothing is written to disk by this function.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"reply": "Chat isn't configured yet — ANTHROPIC_API_KEY isn't set on the server.", "proposal": None}
    if not (message or "").strip():
        return {"reply": "Say something first.", "proposal": None}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=[{"role": "user", "content": message[:2000]}],
        )
    except Exception as e:
        return {"reply": f"Chat error: {e}", "proposal": None}

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    tool_calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "propose_override"]
    fallback_reply = " ".join(p.strip() for p in text_parts if p.strip()) or "I didn't catch an actionable request there."

    if not tool_calls:
        return {"reply": fallback_reply, "proposal": None}

    call = tool_calls[0].input
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
