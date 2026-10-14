"""
A.C.E.S. chat: one conversation where staff ask questions in plain language and
Claude decides which of this server's tools to call.

How it works
  - The browser keeps the conversation (sessionStorage in that tab) and sends it
    with each new question to POST /chat. The server stays stateless, so restarts
    and redeploys don't lose anything the user can still see.
  - The server calls the Anthropic Messages API with this server's MCP tools as
    Claude tools, runs any tool calls in-process through a FastMCP client, and
    loops until Claude answers. Progress streams back as newline-delimited JSON.
  - /chat is guarded by the same x-aces-mcp-key check as /mcp (see server.py).

Settings (environment variables)
  ANTHROPIC_API_KEY          required; chat is off without it
  ACES_CHAT_MODEL            default claude-sonnet-5-5
  ACES_CHAT_MAX_TOKENS       default 4096 (per model reply)
  ACES_CHAT_MAX_TOOL_ROUNDS  default 8 (model/tool cycles per question)
  ACES_CHAT_HISTORY_CHARS    default 120000 (older turns are dropped past this)
  ACES_ANTHROPIC_BASE_URL    default https://api.anthropic.com (override for a gateway)
  ACES_ANTHROPIC_WORKSPACE_ID only for keys that aren't tied to one workspace (wrkspc_...)

Data note: staff questions and tool results (including ACES and public parcel
data) are sent to the Anthropic API to produce answers. Prompts and answers are
not written to the server log; only tool names, timings and token counts are.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastmcp import Client
from starlette.responses import JSONResponse, StreamingResponse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CHAT_MODEL = os.getenv("ACES_CHAT_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"
CHAT_MAX_TOKENS = int(os.getenv("ACES_CHAT_MAX_TOKENS", "4096"))
CHAT_MAX_TOOL_ROUNDS = max(1, int(os.getenv("ACES_CHAT_MAX_TOOL_ROUNDS", "8")))
CHAT_HISTORY_CHARS = int(os.getenv("ACES_CHAT_HISTORY_CHARS", "120000"))
ANTHROPIC_BASE_URL = os.getenv("ACES_ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_WORKSPACE_ID = os.getenv("ACES_ANTHROPIC_WORKSPACE_ID", "").strip()

CHAT_ENABLED = bool(ANTHROPIC_API_KEY)

MAX_USER_TEXT_CHARS = 8000
MAX_TOOL_RESULT_CHARS = 20000
TOOL_CALL_TIMEOUT_SECONDS = 200
PREVIEW_CHARS = 1200

_ALLOWED_BLOCK_KEYS = {
    "text": ("type", "text"),
    "tool_use": ("type", "id", "name", "input"),
    "tool_result": ("type", "tool_use_id", "content", "is_error"),
}


class ChatError(Exception):
    pass


def _today_text() -> str:
    try:
        if ZoneInfo is not None:
            now = datetime.now(ZoneInfo("America/Denver"))
            return now.strftime("%A, %B %d, %Y")
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y")


def _system_prompt() -> str:
    return (
        "You are A.C.E.S., the staff assistant for the Bernalillo County Assessor's Office in New Mexico. "
        f"Today is {_today_text()} (Mountain time).\n\n"
        "How to work:\n"
        "- Answer staff questions by calling the tools. For anything about parcels, values, notices, office call "
        "and visitor volume, forecasts, deeds, or office policy, use a tool instead of memory.\n"
        "- ACES_* tools read the ACES database through fixed read-only procedures. ArcGIS_Public_* tools read the "
        "public parcel map layer. Community_Educator, Assessment_Context_Expert, Clear_Expectations, "
        "Compliance_Expert and Smart_Tasks are specialist agents; pass the staff request to them as promptText in "
        "plain words.\n"
        "- Never invent numbers, parcel facts, owners, statutes, deadlines or policy. If the tools don't return it, "
        "say you don't have it.\n"
        "- If a tool returns an error, tell the user plainly what failed and stop there. Don't work around it with "
        "guesses or a different source.\n"
        "- When staff want to open, print or share a notice explanation, call NOV_Explainer_Link with the UPC and "
        "give them the link. For an address, get the UPC from ACES_NOV_Search or ArcGIS_Public_Parcel_Lookup first. "
        "ACES_NOV_Parcel and ACES_NOV_Search results already include NOV Explainer links; pass those along "
        "instead of calling NOV_Explainer_Link again.\n"
        "- When staff want a field route or to visit a list of parcels, call UPC_Router_Link once with all the UPCs. "
        "For addresses, get the UPCs from ArcGIS_Public_Parcel_Lookup first. Pass along any skipped accounts it reports.\n"
        "- If a specialist agent says its task is still running, give the user the Task ID and Project ID and tell "
        "them you can check it with Check_CustomGPT_Task when they ask.\n"
        "- For daily averages from office volume, use open_days or the avg_ columns, never calendar_days.\n"
        "- For a morning briefing, or a general 'how are we looking today' question, call ACES_Daily_Briefing once "
        "instead of the separate volume, forecast, deed and calendar tools. Deed figures are volume, not backlog. "
        "If a briefing section says UNAVAILABLE, name it and don't estimate it.\n"
        "- When call or visit volume looks unusual, check ACES_Assessment_Calendar for events whose impact window "
        "covers that period, such as notices or tax bills being mailed. Only cite dates the tool returns, and describe "
        "expected impact as expected, not certain.\n"
        "- Parcel values, notice values and exemption figures (from ArcGIS_Public_* or ACES_NOV_*) are for reference; "
        "when you give them, say to verify in iasWorld before official use. Office call, visit, forecast and deed "
        "figures come from ACES office data, not iasWorld, so don't add that note to them.\n"
        "- Write only your conclusions. Don't pose questions to yourself or show working like 'Is X included? No.' "
        "If a total excludes something, say so once, plainly.\n"
        "- If a result shows partial_days above 0, say today's counts are still coming in and that averages leave "
        "today out. Otherwise don't speculate about partial or missing data.\n\n"
        "How to answer:\n"
        "- Lead with the answer. Keep it short and plain.\n"
        "- Say which tool the facts came from.\n"
        "- Use a small markdown table when showing more than three numbers. No headings for short answers."
    )


# ---------------------------------------------------------------------------
# Conversation history from the browser
# ---------------------------------------------------------------------------

def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _clean_block(block: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    keys = _ALLOWED_BLOCK_KEYS.get(kind)
    if not keys:
        return None
    out = {k: block[k] for k in keys if k in block}
    if kind == "text":
        out["text"] = str(out.get("text", ""))
        if not out["text"].strip():
            return None
    elif kind == "tool_use":
        if not out.get("id") or not out.get("name"):
            return None
        out["id"] = str(out["id"])
        out["name"] = str(out["name"])
        out["input"] = out.get("input") if isinstance(out.get("input"), dict) else {}
    elif kind == "tool_result":
        if not out.get("tool_use_id"):
            return None
        out["tool_use_id"] = str(out["tool_use_id"])
        out["content"] = _tool_result_text(out.get("content"))[:MAX_TOOL_RESULT_CHARS]
        out["is_error"] = bool(out.get("is_error", False))
    return out


def _is_question(message: Dict[str, Any]) -> bool:
    """A user turn the person typed, as opposed to a turn carrying tool results."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and all(b.get("type") == "text" for b in content) and bool(content)


def _clean_history(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ChatError("Send the conversation as a list of messages.")

    messages: List[Dict[str, Any]] = []
    for item in raw[-400:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            text = content.strip()
            if not text:
                continue
            messages.append({"role": role, "content": text[:MAX_USER_TEXT_CHARS]})
        elif isinstance(content, list):
            blocks = [b for b in (_clean_block(x) for x in content) if b]
            if blocks:
                messages.append({"role": role, "content": blocks})

    if not messages or not _is_question(messages[-1]):
        raise ChatError("The last message must be the question you want answered.")

    # Keep whole exchanges: only cut the history at a question, so every tool call
    # keeps its result. Drop the oldest exchanges until the history fits the budget.
    sizes = [len(json.dumps(m, ensure_ascii=False)) for m in messages]
    boundaries = [i for i, m in enumerate(messages) if _is_question(m)]
    start = boundaries[-1]
    for i in boundaries:
        if sum(sizes[i:]) <= CHAT_HISTORY_CHARS:
            start = i
            break
    return messages[start:]


# ---------------------------------------------------------------------------
# Model and tool calls
# ---------------------------------------------------------------------------

async def _call_model(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "model": CHAT_MODEL,
        "max_tokens": CHAT_MAX_TOKENS,
        "system": _system_prompt(),
        "tools": tools,
        "messages": messages,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if ANTHROPIC_WORKSPACE_ID:
        headers["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID
    timeout = httpx.Timeout(connect=15, read=180, write=30, pool=15)
    last_status = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{ANTHROPIC_BASE_URL}/v1/messages", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise ChatError(f"Couldn't reach the Anthropic API: {exc.__class__.__name__}.")

        last_status = response.status_code
        if response.status_code in (429, 500, 502, 503, 529):
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except Exception:
                detail = response.text[:300]
            message = f"The Anthropic API returned HTTP {response.status_code}. {detail}".strip()
            if "workspace" in detail.lower() and not ANTHROPIC_WORKSPACE_ID:
                message += (
                    " This server was started without a workspace ID. Close the server window, run "
                    "start-aces.bat again, and paste the wrkspc_ ID when it asks."
                )
            raise ChatError(message)
        return response.json()
    raise ChatError(f"The Anthropic API is busy right now (HTTP {last_status}).")


def _tool_definitions(listed: List[Any]) -> List[Dict[str, Any]]:
    tools = []
    for tool in listed:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
        tools.append(
            {
                "name": tool.name,
                "description": (tool.description or tool.name)[:4000],
                "input_schema": schema,
            }
        )
    return tools


async def _run_tool(client: Client, block: Dict[str, Any], emit, log) -> Dict[str, Any]:
    name = block["name"]
    tool_input = block.get("input") or {}
    await emit({"type": "tool_start", "id": block["id"], "name": name, "input": tool_input})
    started = time.monotonic()
    try:
        result = await client.call_tool(name, tool_input, raise_on_error=False, timeout=TOOL_CALL_TIMEOUT_SECONDS)
        text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "type", "") == "text")
        is_error = bool(result.is_error)
        if not text and not is_error:
            text = "The tool returned no text."
    except Exception as exc:
        text = f"The tool call failed: {exc.__class__.__name__}: {str(exc)[:300]}"
        is_error = True
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("chat tool call", tool=name, ms=elapsed_ms, is_error=is_error, result_chars=len(text))
    await emit(
        {
            "type": "tool_end",
            "id": block["id"],
            "name": name,
            "is_error": is_error,
            "ms": elapsed_ms,
            "preview": text[:PREVIEW_CHARS],
        }
    )
    return {
        "type": "tool_result",
        "tool_use_id": block["id"],
        "content": text[:MAX_TOOL_RESULT_CHARS],
        "is_error": is_error,
    }


async def _run_chat(mcp, history: List[Dict[str, Any]], emit, log) -> None:
    conversation = list(history)
    new_messages: List[Dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    async with Client(mcp) as client:
        tools = _tool_definitions(await client.list_tools())

        for round_number in range(CHAT_MAX_TOOL_ROUNDS + 1):
            await emit({"type": "model"})
            response = await _call_model(conversation, tools)
            for key in usage:
                usage[key] += int((response.get("usage") or {}).get(key, 0) or 0)

            blocks = [b for b in (_clean_block(x) for x in response.get("content") or []) if b]
            assistant = {"role": "assistant", "content": blocks or [{"type": "text", "text": "(no answer)"}]}
            conversation.append(assistant)
            new_messages.append(assistant)

            stop_reason = response.get("stop_reason")
            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            reply_text = "\n\n".join(b["text"] for b in blocks if b["type"] == "text").strip()

            if stop_reason != "tool_use" or not tool_uses:
                if stop_reason == "max_tokens":
                    reply_text += "\n\n(The answer was cut off at the length limit. Ask me to continue.)"
                await emit({"type": "reply", "text": reply_text or "I don't have an answer for that."})
                break

            if reply_text:
                # Show what Claude said before using tools, the same way it shows after a reload.
                await emit({"type": "text", "text": reply_text})

            if round_number == CHAT_MAX_TOOL_ROUNDS:
                # Out of rounds: answer the pending tool calls so the history stays valid, then stop.
                results = [
                    {"type": "tool_result", "tool_use_id": b["id"], "content": "Not run: the tool limit for one question was reached.", "is_error": True}
                    for b in tool_uses
                ]
                tool_turn = {"role": "user", "content": results}
                conversation.append(tool_turn)
                new_messages.append(tool_turn)
                stop_note = {"role": "assistant", "content": [{"type": "text", "text": "I stopped after the maximum number of tool calls for one question. Ask a narrower question or ask me to continue."}]}
                new_messages.append(stop_note)
                await emit({"type": "reply", "text": stop_note["content"][0]["text"]})
                break

            results = await asyncio.gather(*(_run_tool(client, b, emit, log) for b in tool_uses))
            tool_turn = {"role": "user", "content": list(results)}
            conversation.append(tool_turn)
            new_messages.append(tool_turn)

    log("chat answered", model=CHAT_MODEL, turns=len(new_messages), input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"])
    await emit({"type": "done", "messages": new_messages, "usage": usage, "model": CHAT_MODEL})


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

def register_chat(mcp: Any, log: Callable[..., None]) -> bool:
    """Register POST /chat. The x-aces-mcp-key check happens in server.py's middleware."""

    @mcp.custom_route("/chat", methods=["POST"])
    async def chat_route(request):
        if not CHAT_ENABLED:
            return JSONResponse({"error": "Chat is off on this server. An administrator needs to set ANTHROPIC_API_KEY."}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            history = _clean_history(body.get("messages") if isinstance(body, dict) else None)
        except ChatError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: Dict[str, Any]) -> None:
            await queue.put(event)

        async def worker() -> None:
            try:
                await _run_chat(mcp, history, emit, log)
            except ChatError as exc:
                log("chat failed", error=str(exc)[:300])
                await emit({"type": "error", "message": str(exc)})
            except Exception as exc:
                log("chat failed unexpectedly", error=f"{exc.__class__.__name__}: {str(exc)[:300]}")
                await emit({"type": "error", "message": "The chat failed on the server. Check the server log for details."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(worker())

        async def stream():
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    if CHAT_ENABLED:
        log("chat enabled", model=CHAT_MODEL, max_tool_rounds=CHAT_MAX_TOOL_ROUNDS)
    else:
        log("chat disabled (ANTHROPIC_API_KEY is not set)")
    return CHAT_ENABLED
