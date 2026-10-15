"""
A.C.E.S. chat: one conversation where staff ask questions in plain language and
the model decides which of this server's tools to call.

How it works
  - The browser keeps the conversation (sessionStorage in that tab) and sends it
    with each new question to POST /chat. The server stays stateless, so restarts
    and redeploys don't lose anything the user can still see.
  - The server sends the conversation and this server's MCP tools to the chat
    model, runs any tool calls in-process through a FastMCP client, and loops
    until the model answers. Progress streams back as newline-delimited JSON.
  - Two model providers are supported. The conversation is always stored in the
    same shape (text, tool_use and tool_result blocks), so the browser page and
    the tool loop don't change when the provider does; only _call_model differs.
  - /chat is guarded by the same x-aces-mcp-key check as /mcp (see server.py).
  - Build_Chart (aces_charts.py) is offered to the chat model as one more tool
    but runs here, not through MCP. It reads values from a data tool result
    already in the conversation, never from the model, and the browser draws it.

Settings (environment variables)
  ACES_CHAT_PROVIDER         azure_openai or anthropic. Default: azure_openai when
                             AZURE_OPENAI_ENDPOINT is set, otherwise anthropic.

  Azure OpenAI (keeps prompts and tool results inside the county's Azure tenant)
  AZURE_OPENAI_ENDPOINT      e.g. https://aoai-aces-bernco.openai.azure.com
  AZURE_OPENAI_DEPLOYMENT    the deployment name, e.g. aces-chat
  AZURE_OPENAI_API_KEY       optional. Leave unset on Azure App Service: the app's
                             managed identity is used instead (needs the
                             Cognitive Services OpenAI User role on the resource).
  ACES_CHAT_REASONING_EFFORT optional: none, low, medium or high

  Anthropic
  ANTHROPIC_API_KEY          required for this provider
  ACES_CHAT_MODEL            default claude-sonnet-5-5

  Both
  ACES_CHAT_MAX_TOKENS       per model reply. Default 4096 (Anthropic) or 16000
                             (Azure OpenAI, where reasoning counts toward the limit)
  ACES_CHAT_MAX_TOOL_ROUNDS  default 8 (model/tool cycles per question)
  ACES_CHAT_HISTORY_CHARS    default 120000 (older turns are dropped past this)
  ACES_ANTHROPIC_BASE_URL    default https://api.anthropic.com (override for a gateway)
  ACES_ANTHROPIC_WORKSPACE_ID only for keys that aren't tied to one workspace (wrkspc_...)

Data note: staff questions and tool results (including ACES and public parcel
data) are sent to the configured model provider to produce answers. With
azure_openai that is the Azure OpenAI resource in the county's own tenant, and
requests are sent with store=false so the service doesn't keep the conversation.
With anthropic it is the Anthropic API. Prompts and answers are not written to
the server log; only tool names, timings and token counts are.
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

import aces_charts

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
AZURE_OPENAI_SCOPE_RESOURCE = "https://cognitiveservices.azure.com"
CHAT_REASONING_EFFORT = os.getenv("ACES_CHAT_REASONING_EFFORT", "").strip().lower()
# App Service sets these when the app has a managed identity.
IDENTITY_ENDPOINT = os.getenv("IDENTITY_ENDPOINT", "").strip()
IDENTITY_HEADER = os.getenv("IDENTITY_HEADER", "").strip()

CHAT_PROVIDER = os.getenv("ACES_CHAT_PROVIDER", "").strip().lower() or (
    "azure_openai" if AZURE_OPENAI_ENDPOINT else "anthropic"
)
if CHAT_PROVIDER == "azure_openai":
    CHAT_MODEL = AZURE_OPENAI_DEPLOYMENT
    CHAT_MAX_TOKENS = int(os.getenv("ACES_CHAT_MAX_TOKENS", "16000"))
else:
    CHAT_PROVIDER = "anthropic"
    CHAT_MODEL = os.getenv("ACES_CHAT_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"
    CHAT_MAX_TOKENS = int(os.getenv("ACES_CHAT_MAX_TOKENS", "4096"))
CHAT_MAX_TOOL_ROUNDS = max(1, int(os.getenv("ACES_CHAT_MAX_TOOL_ROUNDS", "8")))
CHAT_HISTORY_CHARS = int(os.getenv("ACES_CHAT_HISTORY_CHARS", "120000"))
ANTHROPIC_BASE_URL = os.getenv("ACES_ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_WORKSPACE_ID = os.getenv("ACES_ANTHROPIC_WORKSPACE_ID", "").strip()

if CHAT_PROVIDER == "azure_openai":
    CHAT_ENABLED = bool(
        AZURE_OPENAI_ENDPOINT
        and AZURE_OPENAI_DEPLOYMENT
        and (AZURE_OPENAI_API_KEY or (IDENTITY_ENDPOINT and IDENTITY_HEADER))
    )
    CHAT_OFF_REASON = (
        "set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT, and either give the app a managed "
        "identity or set AZURE_OPENAI_API_KEY"
    )
else:
    CHAT_ENABLED = bool(ANTHROPIC_API_KEY)
    CHAT_OFF_REASON = "set ANTHROPIC_API_KEY"

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
        "- For facts about a specific parcel (UPC, address, owner, class, values, notice details), use ACES_NOV_Search, "
        "ACES_NOV_Parcel or ArcGIS_Public_Parcel_Lookup; they answer in about a second. Call Assessment_Context_Expert "
        "only when staff ask for comps, sales, listings, a property record card or report, or interpretation the data "
        "tools can't give.\n"
        "- No tool here has approved exemption records. The public GIS layer may show exemption flags, but those aren't "
        "approvals. If asked which exemptions a parcel has, say so and point staff to the exemptions tab in iasWorld; "
        "don't send it to a specialist agent. General questions about how exemptions work go to Community_Educator.\n"
        "- Explain a code (land use, reason for change, class) only if a tool returns its meaning. Otherwise give the code "
        "as is.\n"
        "- If a tool returns an error, tell the user plainly what failed and stop there. Don't work around it with "
        "guesses or a different source.\n"
        "- When staff want to open, print or share a notice explanation, call NOV_Explainer_Link with the UPC and "
        "give them the link. For an address, get the UPC from ACES_NOV_Search or ArcGIS_Public_Parcel_Lookup first. "
        "ACES_NOV_Parcel and ACES_NOV_Search results already include NOV Explainer links; pass those along "
        "instead of calling NOV_Explainer_Link again.\n"
        "- When staff want a field route or to visit a list of parcels, call UPC_Router_Link once with all the UPCs. "
        "For addresses, get the UPCs from ArcGIS_Public_Parcel_Lookup first. Pass along any skipped accounts it reports.\n"
        "- If a specialist agent says its task is still running, give the user the Task ID and Project ID and tell "
        "them you can check it with Check_CustomGPT_Task when they ask. If Check_CustomGPT_Task says the task "
        "stalled, tell the user it didn't finish, suggest the alternative it names, and don't offer to check it again.\n"
        "- Don't offer to monitor, check back later or send updates. You only answer when someone asks.\n"
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
        "- If deed results include a DEED DATA GAP note, lead with it: recent deed figures are incomplete because of a "
        "loading gap, not zero activity.\n"
        "- Write only your conclusions. Don't pose questions to yourself or show working like 'Is X included? No.' "
        "If a total excludes something, say so once, plainly.\n"
        "- If a result shows partial_days above 0, say today's counts are still coming in and that averages leave "
        "today out. Otherwise don't speculate about partial or missing data.\n"
        "- When staff ask for a chart, graph or picture of a trend, get the data with the right ACES_* tool first, then "
        "call Build_Chart with that tool's name and the exact column names from its result. Build_Chart reads the "
        "numbers itself; never type numbers into it. Use avg_ columns to compare periods with different numbers of "
        "open days, a line chart for trends over time, and a bar chart for a few categories. After the chart, give the "
        "takeaway in a sentence or two, pass along any data notes it returns, and don't repeat its numbers in a table "
        "unless asked. If Build_Chart returns an error, say what failed.\n\n"
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
    """
    Returns {"content": [blocks], "stop_reason": ..., "usage": {...}} in the shape the
    tool loop uses (text / tool_use blocks; stop_reason tool_use, max_tokens or end_turn).
    """
    if CHAT_PROVIDER == "azure_openai":
        return await _call_azure_openai(messages, tools)
    return await _call_anthropic(messages, tools)


async def _call_anthropic(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Azure OpenAI (Responses API, v1 endpoint)
# ---------------------------------------------------------------------------

_token_cache: Dict[str, Any] = {"token": "", "expires": 0.0}


async def _managed_identity_token() -> str:
    """Token for Azure OpenAI from the App Service managed identity, cached until near expiry."""
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 300:
        return _token_cache["token"]
    if not (IDENTITY_ENDPOINT and IDENTITY_HEADER):
        raise ChatError(
            "This server has no managed identity to sign in to Azure OpenAI. "
            "Turn on the app's system-assigned identity, or set AZURE_OPENAI_API_KEY."
        )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            response = await client.get(
                IDENTITY_ENDPOINT,
                params={"resource": AZURE_OPENAI_SCOPE_RESOURCE, "api-version": "2019-08-01"},
                headers={"X-IDENTITY-HEADER": IDENTITY_HEADER},
            )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise ChatError(f"Couldn't get a managed identity token for Azure OpenAI: {exc.__class__.__name__}.")
    _token_cache["token"] = data.get("access_token", "")
    try:
        _token_cache["expires"] = float(data.get("expires_on", 0))
    except (TypeError, ValueError):
        _token_cache["expires"] = time.time() + 1800
    if not _token_cache["token"]:
        raise ChatError("The managed identity endpoint didn't return a token for Azure OpenAI.")
    return _token_cache["token"]


def _to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stored conversation (text / tool_use / tool_result blocks) to Responses API input items."""
    items: List[Dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        texts: List[str] = []

        def flush_text() -> None:
            if texts:
                items.append({"role": role, "content": "\n\n".join(texts)})
                texts.clear()

        for block in content:
            kind = block.get("type")
            if kind == "text":
                texts.append(block["text"])
            elif kind == "tool_use":
                flush_text()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    }
                )
            elif kind == "tool_result":
                flush_text()
                output = block.get("content") or ""
                if block.get("is_error"):
                    output = "TOOL ERROR: " + output
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": output})
        flush_text()
    return items


def _from_responses_output(data: Dict[str, Any]) -> Dict[str, Any]:
    """Responses API result to the {"content", "stop_reason", "usage"} shape the tool loop uses."""
    blocks: List[Dict[str, Any]] = []
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and str(part.get("text", "")).strip():
                    blocks.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "refusal" and str(part.get("refusal", "")).strip():
                    blocks.append({"type": "text", "text": part["refusal"]})
        elif kind == "function_call":
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
        # Reasoning items are not kept: requests use store=false, and the next turn
        # works from the tool results and visible text.

    if any(b["type"] == "tool_use" for b in blocks):
        stop_reason = "tool_use"
    elif data.get("status") == "incomplete" and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    usage = data.get("usage") or {}
    return {
        "content": blocks,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        },
    }


def _responses_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # strict=False: MCP tool schemas have optional fields, which strict mode rejects.
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
            "strict": False,
        }
        for t in tools
    ]


async def _call_azure_openai(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": AZURE_OPENAI_DEPLOYMENT,
        "instructions": _system_prompt(),
        "input": _to_responses_input(messages),
        "tools": _responses_tools(tools),
        "max_output_tokens": CHAT_MAX_TOKENS,
        "store": False,
    }
    if CHAT_REASONING_EFFORT:
        payload["reasoning"] = {"effort": CHAT_REASONING_EFFORT}

    url = f"{AZURE_OPENAI_ENDPOINT}/openai/v1/responses"
    timeout = httpx.Timeout(connect=15, read=180, write=30, pool=15)
    last_status = None
    for attempt in range(3):
        headers = {"content-type": "application/json"}
        if AZURE_OPENAI_API_KEY:
            headers["api-key"] = AZURE_OPENAI_API_KEY
        else:
            headers["authorization"] = f"Bearer {await _managed_identity_token()}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise ChatError(f"Couldn't reach Azure OpenAI: {exc.__class__.__name__}.")

        last_status = response.status_code
        if response.status_code == 401 and not AZURE_OPENAI_API_KEY and attempt < 2:
            _token_cache["token"] = ""  # token may have expired or been revoked; get a fresh one
            continue
        if response.status_code in (429, 500, 502, 503):
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except Exception:
                detail = response.text[:300]
            message = f"Azure OpenAI returned HTTP {response.status_code}. {detail}".strip()
            if response.status_code in (401, 403) and not AZURE_OPENAI_API_KEY:
                message += (
                    " Check that the app's managed identity has the Cognitive Services OpenAI User role "
                    "on the Azure OpenAI resource (new role assignments can take a few minutes)."
                )
            if response.status_code == 404:
                message += f" Check that a deployment named '{AZURE_OPENAI_DEPLOYMENT}' exists."
            raise ChatError(message)
        return _from_responses_output(response.json())
    raise ChatError(f"Azure OpenAI is busy right now (HTTP {last_status}).")


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


async def _run_tool(client: Client, block: Dict[str, Any], emit, log, full_text: Dict[str, str]) -> Dict[str, Any]:
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
    full_text[block["id"]] = text
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


async def _run_chart_tool(block: Dict[str, Any], sources: List[Dict[str, Any]], emit, log) -> Dict[str, Any]:
    """Build_Chart runs here, not through MCP: it needs the conversation, and it draws in the browser."""
    tool_input = block.get("input") or {}
    await emit({"type": "tool_start", "id": block["id"], "name": block["name"], "input": tool_input})
    started = time.monotonic()
    try:
        spec, summary = aces_charts.build_chart(tool_input, sources)
        content = aces_charts.chart_result_content(spec, summary)
        is_error = False
    except aces_charts.ChartError as exc:
        summary = f"Chart not drawn: {exc}"
        content, spec, is_error = summary, None, True
    except Exception as exc:
        summary = f"Chart not drawn: the chart builder failed ({exc.__class__.__name__})."
        content, spec, is_error = summary, None, True
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if spec is not None and len(content) > MAX_TOOL_RESULT_CHARS:
        summary = "Chart not drawn: it has too much data to keep in the conversation. Use a coarser grain or fewer series."
        content, spec, is_error = summary, None, True
    log(
        "chat chart",
        source=str(tool_input.get("sourceTool", ""))[:80],
        ms=elapsed_ms,
        is_error=is_error,
        points=len(spec["x"]["values"]) if spec else 0,
        series=len(spec["series"]) if spec else 0,
    )
    if spec is not None:
        await emit({"type": "chart", "id": block["id"], "spec": spec})
    await emit(
        {"type": "tool_end", "id": block["id"], "name": block["name"], "is_error": is_error, "ms": elapsed_ms, "preview": summary[:PREVIEW_CHARS]}
    )
    return {"type": "tool_result", "tool_use_id": block["id"], "content": content, "is_error": is_error}


async def _run_chat(mcp, history: List[Dict[str, Any]], emit, log) -> None:
    conversation = list(history)
    new_messages: List[Dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    full_text: Dict[str, str] = {}  # untruncated results from this request, for Build_Chart

    async with Client(mcp) as client:
        tools = _tool_definitions(await client.list_tools())
        if not any(t["name"] == aces_charts.CHART_TOOL_NAME for t in tools):
            tools.append(aces_charts.CHART_TOOL)

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
                # Show what the model said before using tools, the same way it shows after a reload.
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

            # Data tools first, then charts, so a chart can use a result from the same round.
            data_uses = [b for b in tool_uses if b["name"] != aces_charts.CHART_TOOL_NAME]
            chart_uses = [b for b in tool_uses if b["name"] == aces_charts.CHART_TOOL_NAME]
            by_id: Dict[str, Dict[str, Any]] = {}
            for r in await asyncio.gather(*(_run_tool(client, b, emit, log, full_text) for b in data_uses)):
                by_id[r["tool_use_id"]] = r
            if chart_uses:
                pending = conversation + [{"role": "user", "content": list(by_id.values())}]
                sources = aces_charts.collect_sources(pending, full_text)
                for b in chart_uses:
                    by_id[b["id"]] = await _run_chart_tool(b, sources, emit, log)
            results = [by_id[b["id"]] for b in tool_uses]
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
            return JSONResponse({"error": f"Chat is off on this server. An administrator needs to {CHAT_OFF_REASON}."}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            history = _clean_history(body.get("messages") if isinstance(body, dict) else None)
        except ChatError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        # Set by server.py's middleware when the request came from a county sign-in.
        user = str((request.scope.get("state") or {}).get("aces_user") or "")

        def chat_log(message: str, **kwargs: Any) -> None:
            if user:
                kwargs["user"] = user
            log(message, **kwargs)

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: Dict[str, Any]) -> None:
            await queue.put(event)

        async def worker() -> None:
            try:
                await _run_chat(mcp, history, emit, chat_log)
            except ChatError as exc:
                chat_log("chat failed", error=str(exc)[:300])
                await emit({"type": "error", "message": str(exc)})
            except Exception as exc:
                chat_log("chat failed unexpectedly", error=f"{exc.__class__.__name__}: {str(exc)[:300]}")
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
        log("chat enabled", provider=CHAT_PROVIDER, model=CHAT_MODEL, max_tool_rounds=CHAT_MAX_TOOL_ROUNDS)
    else:
        log("chat disabled", provider=CHAT_PROVIDER, fix=CHAT_OFF_REASON)
    return CHAT_ENABLED
