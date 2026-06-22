import os
import json
import re
import asyncio
import hashlib
import time
from typing import Optional, Any, Dict, List, Tuple

# FastMCP reads these through settings/env for Streamable HTTP behavior.
os.environ.setdefault("FASTMCP_STATELESS_HTTP", "true")
os.environ.setdefault("FASTMCP_JSON_RESPONSE", "true")

import httpx
from fastmcp import FastMCP
from starlette.responses import JSONResponse


mcp = FastMCP(
    "A.C.E.S. Specialist Tools",
    instructions=(
        "Internal Bernalillo County Assessor staff MCP server. "
        "Exposes five tools: Community_Educator, Assessment_Context_Expert, "
        "Clear_Expectations, Compliance_Expert, and Check_CustomGPT_Task. "
        "The four specialist tools take exactly one promptText string and return plain text. "
        "Check_CustomGPT_Task takes projectId and taskId to retrieve a delayed task result."
    ),
)


CUSTOMGPT_API_TOKEN = os.getenv("CUSTOMGPT_API_TOKEN", "").strip()

# Set these in Render Environment.
COMMUNITY_PROJECT_ID = os.getenv("COMMUNITY_PROJECT_ID", "").strip()
ASSESSMENT_PROJECT_ID = os.getenv("ASSESSMENT_PROJECT_ID", "94006").strip()
CLEAR_PROJECT_ID = os.getenv("CLEAR_PROJECT_ID", "").strip()
COMPLIANCE_PROJECT_ID = os.getenv("COMPLIANCE_PROJECT_ID", "").strip()

# HomeHarvest External API action ID inside CustomGPT project 94006.
HOMEHARVEST_ACTION_ID = os.getenv("HOMEHARVEST_ACTION_ID", "7").strip()

# Optional admin token for viewing /task-cache and /check-task.
ACES_ADMIN_TOKEN = os.getenv("ACES_ADMIN_TOKEN", "").strip()

CUSTOMGPT_BASE = os.getenv("CUSTOMGPT_BASE", "https://app.customgpt.ai/api/v1").rstrip("/")

# Render filesystem is ephemeral. This cache survives while the instance is alive,
# but may reset after redeploy/cold start. Always return Task ID + Project ID for
# unfinished tasks so Check_CustomGPT_Task can poll CustomGPT directly.
TASK_CACHE_FILE = os.getenv("TASK_CACHE_FILE", "/tmp/aces_task_cache.json")

# CustomGPT tasks are async. These values keep MCP calls from holding open too long
# while still allowing quick tasks to finish in one response.
DEFAULT_POLL_SECONDS = int(os.getenv("ACES_DEFAULT_POLL_SECONDS", "60"))
HOMEHARVEST_POLL_SECONDS = int(os.getenv("ACES_HOMEHARVEST_POLL_SECONDS", "25"))
POLL_INTERVAL_SECONDS = float(os.getenv("ACES_POLL_INTERVAL_SECONDS", "3"))
MAX_CACHED_ANSWER_CHARS = int(os.getenv("ACES_MAX_CACHED_ANSWER_CHARS", "80000"))

# Fresh lookup behavior:
# Address / HomeHarvest / comps / listing work should usually create a fresh
# CustomGPT task. Otherwise Copilot can appear to call the MCP tool while the
# server returns an old cached answer in ~0.25 seconds.
FRESH_HOMEHARVEST_LOOKUPS = os.getenv("ACES_FRESH_HOMEHARVEST_LOOKUPS", "true").strip().lower() not in {"0", "false", "no", "off"}

# Set this to "true" only if you want exact repeated HomeHarvest prompts to reuse
# completed cached answers. The safer default for staff testing is false.
REUSE_COMPLETED_HOMEHARVEST_ANSWERS = os.getenv("ACES_REUSE_COMPLETED_HOMEHARVEST_ANSWERS", "false").strip().lower() in {"1", "true", "yes", "on"}

# Optional: allow reuse of in-flight HomeHarvest tasks so repeated "check again"
# style calls do not spawn duplicate CustomGPT tasks while the first task is still running.
REUSE_RUNNING_HOMEHARVEST_TASKS = os.getenv("ACES_REUSE_RUNNING_HOMEHARVEST_TASKS", "true").strip().lower() in {"1", "true", "yes", "on"}



def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stable_hash(text: str) -> str:
    value = text or ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _log(message: str, **kwargs: Any) -> None:
    """Small structured-ish logger for Render stdout."""
    details = " ".join(f"{key}={value}" for key, value in kwargs.items() if value is not None)
    print(f"[aces] {message}" + (f" {details}" if details else ""), flush=True)


def _trim_cached_answer(answer: Optional[str]) -> Optional[str]:
    if answer is None:
        return None
    text = str(answer)
    if len(text) > MAX_CACHED_ANSWER_CHARS:
        return text[:MAX_CACHED_ANSWER_CHARS] + "\n...[cached answer truncated]"
    return text


def _normalize_cache_text(text: str) -> str:
    """
    Normalize prompts for duplicate detection. This is only for cache keys; it is
    never used as the prompt sent to CustomGPT.
    """
    value = (text or "").lower()
    replacements = {
        "new mexico": "nm",
        "court": "ct",
        "drive": "dr",
        "road": "rd",
        "street": "st",
        "avenue": "ave",
        "lane": "ln",
        "boulevard": "blvd",
        "place": "pl",
        "circle": "cir",
        "trail": "trl",
        "comparable properties": "comps",
        "comparables": "comps",
        "comparable": "comp",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_label_block(prompt_text: str, label: str, stop_labels: Optional[List[str]] = None) -> str:
    """Extract a labeled section from promptText."""
    text = prompt_text or ""
    stop_labels = stop_labels or []
    pattern = re.compile(re.escape(label) + r"\s*(.*)", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    if not match:
        return ""

    block = match.group(1)
    for stop in stop_labels:
        stop_pattern = re.compile(r"\n\s*" + re.escape(stop), re.IGNORECASE)
        stop_match = stop_pattern.search(block)
        if stop_match:
            block = block[: stop_match.start()]
    return block.strip()


def _extract_address_for_cache(prompt_text: str) -> str:
    """
    Pull Address/search area if present. Falls back to a rough street-address
    regex so small routing changes do not create duplicate CustomGPT tasks.
    """
    text = prompt_text or ""
    labeled = _extract_label_block(
        text,
        "Address/search area:",
        ["Staff request:", "Instructions:", "Uploaded record transcription:"],
    )
    if labeled:
        return _normalize_cache_text(labeled.strip().splitlines()[0].strip())

    lowered = text.lower().replace("new mexico", "nm")
    match = re.search(
        r"\b\d{1,6}\s+[a-z0-9 .'-]{2,80}\s+"
        r"(?:ct|court|dr|drive|rd|road|st|street|ave|avenue|ln|lane|way|blvd|boulevard|pl|place|cir|circle|trl|trail)"
        r"(?:[\s,]+[a-z .'-]{2,40})?(?:[\s,]+nm)?(?:[\s,]+\d{5})?",
        lowered,
        re.IGNORECASE,
    )
    if match:
        return _normalize_cache_text(match.group(0))
    return ""


def _canonical_prompt_for_cache(tool_name: str, prompt_text: str) -> str:
    """
    Build a semantic cache key so repeated address/comps requests reuse the same
    task even when the front router rephrases or echoes MODE blocks.
    """
    raw = prompt_text or ""
    norm = _normalize_cache_text(raw)

    if tool_name == "Assessment_Context_Expert":
        mode = "assessment"
        if "mode address homeharvest lookup" in norm:
            mode = "address_homeharvest"
        elif "mode record homeharvest comp support" in norm:
            mode = "record_homeharvest_comp"
        elif "mode record analysis" in norm:
            mode = "record_analysis"

        address = _extract_address_for_cache(raw)

        wants_comps = any(
            word in norm
            for word in ["comp", "comps", "nearby sale", "nearby sales", "sold", "sale", "sales", "market", "similar"]
        )
        wants_listing = any(word in norm for word in ["listing", "listings", "active", "for sale"])
        wants_lookup = any(word in norm for word in ["look up", "lookup", "property", "address", "homeharvest"])

        if wants_comps:
            intent = "comps"
        elif wants_listing:
            intent = "listings"
        elif wants_lookup or address:
            intent = "address_lookup"
        else:
            intent = "assessment"

        count_match = re.search(r"\b(\d{1,2})\s+(?:comp|comps|properties|sales|listings)\b", norm)
        years_match = re.search(r"\b(?:past|last|within)\s+(\d{1,2})\s+years?\b", norm)
        count = count_match.group(1) if count_match else ""
        years = years_match.group(1) if years_match else ""

        if address:
            return f"{tool_name}|{mode}|{address}|{intent}|count={count}|years={years}"

    return f"{tool_name}|{norm[:1200]}"


def _safe_json_dumps(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text


def _load_task_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(TASK_CACHE_FILE):
            with open(TASK_CACHE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:
        print(f"[task-cache] load failed: {exc}", flush=True)
    return {}


def _save_task_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(TASK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[task-cache] save failed: {exc}", flush=True)


TASK_CACHE: Dict[str, Any] = _load_task_cache()


def _task_cache_key(project_id: str, tool_name: str, prompt_text: str) -> str:
    canonical = _canonical_prompt_for_cache(tool_name, prompt_text)
    return f"{project_id}|{tool_name}|{_stable_hash(canonical)}"


def _find_task_cache_entry(project_id: str, task_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    for key, value in TASK_CACHE.items():
        if not isinstance(value, dict):
            continue
        if str(value.get("project_id", "")) == str(project_id) and str(value.get("task_id", "")) == str(task_id):
            return key, value
    return None, None


def _cached_answer_for_task(project_id: str, task_id: str) -> str:
    _, entry = _find_task_cache_entry(project_id, task_id)
    if isinstance(entry, dict):
        answer = str(entry.get("last_answer") or "").strip()
        if answer:
            return answer
    return ""


def _find_existing_prompt_task(project_id: str, tool_name: str, prompt_text: str) -> Optional[Dict[str, Any]]:
    """Return an existing cached task for the same project/tool/semantic prompt."""
    key = _task_cache_key(project_id, tool_name, prompt_text)
    dedupe_hash = _stable_hash(_canonical_prompt_for_cache(tool_name, prompt_text))
    reusable_statuses = {"submitted", "polling", "still_running", "completed", "answered"}

    direct = TASK_CACHE.get(key)
    if isinstance(direct, dict):
        task_id = str(direct.get("task_id") or "").strip()
        status = str(direct.get("status") or "").strip()
        if task_id and status in reusable_statuses:
            return direct

    for value in TASK_CACHE.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("project_id", "")) != str(project_id):
            continue
        if str(value.get("tool_name", "")) != str(tool_name):
            continue
        task_id = str(value.get("task_id") or "").strip()
        status = str(value.get("status") or "").strip()
        value_dedupe_hash = str(value.get("dedupe_hash") or "").strip()
        if task_id and status in reusable_statuses and value_dedupe_hash == dedupe_hash:
            return value
    return None


def _remember_task(
    project_id: str,
    tool_name: str,
    prompt_text: str,
    task_id: str,
    status: str,
    message_id: Optional[str] = None,
    latest_status: Optional[str] = None,
    progress_log: Optional[List[str]] = None,
    error: Optional[str] = None,
    answer: Optional[str] = None,
) -> str:
    key = _task_cache_key(project_id, tool_name, prompt_text)
    existing = TASK_CACHE.get(key, {})
    if not isinstance(existing, dict):
        existing = {}

    existing.update(
        {
            "cache_key": key,
            "project_id": str(project_id),
            "tool_name": str(tool_name),
            "task_id": str(task_id),
            "status": str(status or ""),
            "latest_status": str(latest_status or status or ""),
            "message_id": str(message_id) if message_id else existing.get("message_id"),
            "prompt_hash": _stable_hash(prompt_text),
            "dedupe_hash": _stable_hash(_canonical_prompt_for_cache(tool_name, prompt_text)),
            "updated_at": _now_iso(),
        }
    )
    if "created_at" not in existing:
        existing["created_at"] = _now_iso()
    if progress_log is not None:
        existing["progress_log"] = [str(x) for x in progress_log[-10:]]
    if error:
        existing["error"] = str(error)
    if answer is not None:
        existing["last_answer"] = _trim_cached_answer(answer)
        existing["answered_at"] = _now_iso()

    TASK_CACHE[key] = existing
    _save_task_cache(TASK_CACHE)
    print(f"[task-cache] saved tool={tool_name} project={project_id} task_id={task_id} status={status} key={key}", flush=True)
    return key


def _update_task_cache_by_task_id(
    project_id: str,
    task_id: str,
    status: str,
    latest_status: Optional[str] = None,
    message_id: Optional[str] = None,
    progress_log: Optional[List[str]] = None,
    error: Optional[str] = None,
    answer: Optional[str] = None,
) -> str:
    key, existing = _find_task_cache_entry(project_id, task_id)
    if not key:
        key = f"{project_id}|task_id|{task_id}"
        existing = {
            "cache_key": key,
            "project_id": str(project_id),
            "tool_name": "Check_CustomGPT_Task",
            "task_id": str(task_id),
            "created_at": _now_iso(),
        }
    if existing is None:
        existing = {}

    existing.update(
        {
            "cache_key": key,
            "project_id": str(project_id),
            "task_id": str(task_id),
            "status": str(status or ""),
            "latest_status": str(latest_status or status or ""),
            "message_id": str(message_id) if message_id else existing.get("message_id"),
            "updated_at": _now_iso(),
        }
    )
    if progress_log is not None:
        existing["progress_log"] = [str(x) for x in progress_log[-10:]]
    if error:
        existing["error"] = str(error)
    if answer is not None:
        existing["last_answer"] = _trim_cached_answer(answer)
        existing["answered_at"] = _now_iso()

    TASK_CACHE[key] = existing
    _save_task_cache(TASK_CACHE)
    print(f"[task-cache] updated project={project_id} task_id={task_id} status={status} key={key}", flush=True)
    return key


def _admin_authorized(request) -> bool:
    if not ACES_ADMIN_TOKEN:
        return False
    supplied_token = request.headers.get("x-aces-admin-token", "") or request.query_params.get("token", "")
    return supplied_token == ACES_ADMIN_TOKEN


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse(
        {
            "status": "healthy",
            "service": "aces-mcp-server",
            "mcp_endpoint": "/mcp",
            "assessment_project_id": ASSESSMENT_PROJECT_ID,
            "homeharvest_action_id": HOMEHARVEST_ACTION_ID,
            "stateless_http": os.getenv("FASTMCP_STATELESS_HTTP", ""),
            "json_response": os.getenv("FASTMCP_JSON_RESPONSE", ""),
            "task_cache_file": TASK_CACHE_FILE,
            "task_cache_count": len(TASK_CACHE),
            "task_cache_debug_enabled": bool(ACES_ADMIN_TOKEN),
            "default_poll_seconds": DEFAULT_POLL_SECONDS,
            "homeharvest_poll_seconds": HOMEHARVEST_POLL_SECONDS,
            "duplicate_prompt_reuse": True,
            "semantic_duplicate_prompt_reuse": True,
            "cached_final_answers": True,
            "fresh_homeharvest_lookups": FRESH_HOMEHARVEST_LOOKUPS,
            "reuse_completed_homeharvest_answers": REUSE_COMPLETED_HOMEHARVEST_ANSWERS,
            "reuse_running_homeharvest_tasks": REUSE_RUNNING_HOMEHARVEST_TASKS,
            "tools": [
                "Community_Educator",
                "Assessment_Context_Expert",
                "Clear_Expectations",
                "Compliance_Expert",
                "Check_CustomGPT_Task",
            ],
        }
    )


@mcp.custom_route("/task-cache", methods=["GET"])
async def task_cache_debug(request):
    if not ACES_ADMIN_TOKEN:
        return JSONResponse({"enabled": False, "message": "Set ACES_ADMIN_TOKEN in Render to enable this debug route."}, status_code=403)
    if not _admin_authorized(request):
        return JSONResponse({"error": "Unauthorized. Supply x-aces-admin-token header or ?token=..."}, status_code=401)
    return JSONResponse({"count": len(TASK_CACHE), "tasks": TASK_CACHE})


@mcp.custom_route("/task-cache/clear", methods=["POST"])
async def task_cache_clear(request):
    """Admin-only route to clear the local Render task cache."""
    if not ACES_ADMIN_TOKEN:
        return JSONResponse({"enabled": False, "message": "Set ACES_ADMIN_TOKEN in Render to enable this debug route."}, status_code=403)
    if not _admin_authorized(request):
        return JSONResponse({"error": "Unauthorized. Supply x-aces-admin-token header or ?token=..."}, status_code=401)

    count = len(TASK_CACHE)
    TASK_CACHE.clear()
    _save_task_cache(TASK_CACHE)
    _log("task cache cleared", count=count)
    return JSONResponse({"cleared": True, "previous_count": count, "count": len(TASK_CACHE)})


@mcp.custom_route("/check-task", methods=["GET"])
async def check_task_route(request):
    if not ACES_ADMIN_TOKEN:
        return JSONResponse({"enabled": False, "message": "Set ACES_ADMIN_TOKEN in Render to enable this debug route."}, status_code=403)
    if not _admin_authorized(request):
        return JSONResponse({"error": "Unauthorized. Supply x-aces-admin-token header or ?token=..."}, status_code=401)

    project_id = str(request.query_params.get("project_id") or ASSESSMENT_PROJECT_ID).strip()
    task_id = str(request.query_params.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "Missing task_id query parameter."}, status_code=400)

    result = await _check_customgpt_task_result(project_id=project_id, task_id=task_id)
    return JSONResponse({"project_id": project_id, "task_id": task_id, "result": result})


def _require_config(project_id: str, tool_name: str) -> Optional[str]:
    if not CUSTOMGPT_API_TOKEN:
        return "Missing CUSTOMGPT_API_TOKEN on Render."
    if not project_id:
        return f"Missing project id for {tool_name}. Set it in Render Environment."
    return None


def _should_enable_homeharvest(prompt_text: str) -> bool:
    """
    Enable HomeHarvest only for explicit HomeHarvest/address/comps/listing/public
    aggregator work. Keep ordinary PRC/code/value review from receiving action overrides.
    """
    text = (prompt_text or "").lower()
    padded = f" {text} "

    explicit_mode = (
        "mode: address / homeharvest lookup" in text
        or "mode: record + homeharvest comp support" in text
        or "homeharvest" in text
        or "public aggregator" in text
    )

    comp_or_listing_words = [
        "comps",
        "comp ",
        "comparable",
        "nearby sales",
        "nearby sale",
        "sold properties",
        "sold property",
        "listing data",
        "listings",
        "market support",
    ]

    street_suffixes = [
        " ct", " court", " dr", " drive", " rd", " road", " st", " street", " ave", " avenue",
        " ln", " lane", " way", " blvd", " boulevard", " pl", " place", " cir", " circle", " trl", " trail",
    ]
    looks_like_nm_address = (
        (" nm" in padded or "new mexico" in text or "albuquerque" in text or "tijeras" in text)
        and any(suffix in padded for suffix in street_suffixes)
    )

    address_lookup_request = ("look up" in text or "lookup" in text or "search" in text or "find" in text) and looks_like_nm_address
    return explicit_mode or any(word in text for word in comp_or_listing_words) or address_lookup_request


def _enrich_homeharvest_prompt(prompt_text: str) -> str:
    """Add action-use guidance without overriding an existing MODE block."""
    text = prompt_text or ""
    if "homeharvest action rule" in text.lower():
        return text

    action_rule = (
        "\n\nHOMEHARVEST ACTION RULE:\n"
        "- Use the enabled HomeHarvest custom action when the request is an address, nearby sale, or comp lookup.\n"
        "- Do not answer from memory, previous runs, cached examples, or stale conversation context. Run the action for this request.\n"
        "- Prefer operation homeharvestSearchProperties.\n"
        "- Use POST /properties/search.\n"
        "- For sold/comps requests, use sold/listing_type=sold filters when supported and honor date range instructions.\n"
        "- Return staff-readable numbered cards, not raw JSON.\n"
        "- If no exact match appears, say no exact match was returned and summarize nearby/public aggregator results.\n"
        "- A no-result response is not a tool failure.\n"
    )

    if "mode:" in text.lower():
        return text + action_rule

    return "MODE: ADDRESS / HOMEHARVEST LOOKUP\n\nSTAFF REQUEST:\n" + text + action_rule


def _detect_assessment_status_message(prompt_text: str) -> str:
    text = (prompt_text or "").lower()
    if "mode: address / homeharvest lookup" in text:
        return "Address/HomeHarvest task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
    if "mode: record + homeharvest comp support" in text:
        return "Record/HomeHarvest comp-support task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
    return "Task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."


async def _read_json_or_text(response: httpx.Response) -> Any:
    text = response.text or ""
    try:
        return response.json()
    except Exception:
        return {"raw": text}


def _extract_answer_from_message(final_data: Any) -> str:
    """Extract answer text from known CustomGPT response envelopes."""
    if not isinstance(final_data, dict):
        return ""

    candidates: List[Any] = []
    data = final_data.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        result = data.get("result")
        if isinstance(result, dict):
            candidates.append(result)
        message = data.get("message")
        if isinstance(message, dict):
            candidates.append(message)
    candidates.append(final_data)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("response", "openai_response", "agent_answer", "answer", "content", "message"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _extract_answer_from_message(value)
                if nested:
                    return nested
    return ""


def _extract_progress_log_from_task_data(data: Dict[str, Any]) -> List[str]:
    progress_log: List[str] = []
    events = data.get("events", []) or []
    for ev in events:
        ev = ev or {}
        ev_data = ev.get("data", {}) or {}
        if ev_data.get("current_task"):
            current_task = str(ev_data.get("current_task"))
            if not progress_log or progress_log[-1] != current_task:
                progress_log.append(current_task)
    return progress_log


def _format_still_running_response(
    project_id: str,
    tool_name: str,
    prompt_text: str,
    task_id: str,
    cache_key: str,
    latest_status: str,
    progress_log: List[str],
) -> str:
    if tool_name == "Assessment_Context_Expert":
        first_line = _detect_assessment_status_message(prompt_text)
    else:
        first_line = f"{tool_name} task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."

    return (
        f"{first_line}\n"
        f"Task ID: {task_id}\n"
        f"Project ID: {project_id}\n"
        f"Tool: {tool_name}\n"
        f"Cache key: {cache_key}\n"
        f"Latest status: {latest_status or 'unknown'}\n"
        f"Progress: {_safe_json_dumps(progress_log[-10:], 1000)}"
    )


async def _fetch_customgpt_final_message(
    client: httpx.AsyncClient,
    project_id: str,
    task_id: str,
    message_id: str,
    headers: Dict[str, str],
) -> str:
    base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
    final = await client.get(f"{base}/messages/{message_id}", headers=headers)
    final_data = await _read_json_or_text(final)

    if final.status_code >= 400:
        return (
            "Final message fetch failed.\n"
            f"Project ID: {project_id}\n"
            f"Task ID: {task_id}\n"
            f"Message ID: {message_id}\n"
            f"HTTP status: {final.status_code}\n"
            f"Response: {_safe_json_dumps(final_data)}"
        )

    answer = _extract_answer_from_message(final_data)
    if not answer:
        return (
            "Task completed but the final answer was empty.\n"
            f"Project ID: {project_id}\n"
            f"Task ID: {task_id}\n"
            f"Message ID: {message_id}\n"
            f"Final response: {_safe_json_dumps(final_data)}"
        )
    return answer


async def _fetch_task_history_fallback(
    client: httpx.AsyncClient,
    project_id: str,
    task_id: str,
    headers: Dict[str, str],
) -> str:
    """
    Fallback for cases where the task poll result was consumed/expired. CustomGPT
    documentation says persisted history is available via GET /tasks/{taskId}/messages.
    """
    url = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}/messages"
    response = await client.get(url, headers=headers)
    data = await _read_json_or_text(response)
    if response.status_code >= 400:
        return ""

    # Try common list shapes and return the last answer-like message.
    possible_lists: List[Any] = []
    if isinstance(data, dict):
        for key in ("data", "messages", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                possible_lists.append(value)
            elif isinstance(value, dict):
                for nested_key in ("messages", "items", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        possible_lists.append(nested)
    elif isinstance(data, list):
        possible_lists.append(data)

    for messages in possible_lists:
        for item in reversed(messages):
            answer = _extract_answer_from_message(item)
            if answer:
                return answer
    return _extract_answer_from_message(data)


async def _check_customgpt_task_result(project_id: str, task_id: str) -> str:
    project_id = str(project_id or "").strip()
    task_id = str(task_id or "").strip()

    config_error = _require_config(project_id, "Check_CustomGPT_Task")
    if config_error:
        return config_error
    if not task_id:
        return "Missing taskId."

    cached_answer = _cached_answer_for_task(project_id, task_id)
    if cached_answer:
        return cached_answer

    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "application/json"}
    base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
    timeout = httpx.Timeout(connect=15, read=35, write=35, pool=15)

    async with httpx.AsyncClient(timeout=timeout) as client:
        poll = await client.get(base, headers=headers)
        poll_data = await _read_json_or_text(poll)

        if poll.status_code >= 400:
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status="history_fallback", answer=history_answer)
                return history_answer

            error_text = _safe_json_dumps(poll_data)
            _update_task_cache_by_task_id(project_id, task_id, "check_failed", latest_status="check_failed", error=error_text)
            return (
                "Task check failed.\n"
                f"Project ID: {project_id}\n"
                f"Task ID: {task_id}\n"
                f"HTTP status: {poll.status_code}\n"
                f"Response: {error_text}"
            )

        data = poll_data.get("data", {}) if isinstance(poll_data, dict) else {}
        latest_status = str(data.get("status") or "unknown")
        progress_log = _extract_progress_log_from_task_data(data)
        result = data.get("result") or {}
        message_id = result.get("message_id") if isinstance(result, dict) else None

        if latest_status != "completed":
            _update_task_cache_by_task_id(project_id, task_id, "still_running", latest_status=latest_status, progress_log=progress_log)
            return (
                "Task is not complete yet.\n"
                f"Project ID: {project_id}\n"
                f"Task ID: {task_id}\n"
                f"Latest status: {latest_status}\n"
                f"Progress: {_safe_json_dumps(progress_log[-10:], 1000)}"
            )

        inline_answer = _extract_answer_from_message({"data": data}) or _extract_answer_from_message(data)
        if inline_answer:
            _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status=latest_status, progress_log=progress_log, answer=inline_answer)
            return inline_answer

        if not message_id:
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status="history_fallback", progress_log=progress_log, answer=history_answer)
                return history_answer

            _update_task_cache_by_task_id(project_id, task_id, "completed_no_message_id", latest_status=latest_status, progress_log=progress_log)
            return (
                "Task completed but no message_id was returned.\n"
                f"Project ID: {project_id}\n"
                f"Task ID: {task_id}\n"
                f"Response: {_safe_json_dumps(data)}"
            )

        answer = await _fetch_customgpt_final_message(client, project_id, task_id, str(message_id), headers)
        if answer.startswith("Final message fetch failed") or answer.startswith("Task completed but the final answer was empty"):
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status="history_fallback", message_id=str(message_id), progress_log=progress_log, answer=history_answer)
                return history_answer

            _update_task_cache_by_task_id(project_id, task_id, "final_fetch_or_empty_failed", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, error=answer)
            return answer

        _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, answer=answer)
        return answer


def _is_homeharvest_lookup(tool_name: str, prompt_text: str, action_id: Optional[str]) -> bool:
    return tool_name == "Assessment_Context_Expert" and bool(action_id or _should_enable_homeharvest(prompt_text))


def _should_reuse_prompt_cache(tool_name: str, prompt_text: str, action_id: Optional[str]) -> bool:
    """
    Decide whether a direct specialist tool call may reuse a prompt-level cached task.

    Important: this controls only the initial tool call. Task IDs can still be
    checked through Check_CustomGPT_Task, and every submitted task is still saved.
    """
    if not _is_homeharvest_lookup(tool_name, prompt_text, action_id):
        return True

    if not FRESH_HOMEHARVEST_LOOKUPS:
        return True

    # For staff property/comps/address lookups, a completed cached answer is the
    # common reason Copilot says the MCP tool completed but the Context Expert was
    # never queried again.
    return False


def _should_reuse_existing_homeharvest_task(existing_task: Dict[str, Any]) -> bool:
    """Allow reuse only for still-running HomeHarvest tasks, not completed answers."""
    if not REUSE_RUNNING_HOMEHARVEST_TASKS:
        return False
    status = str(existing_task.get("status") or "").strip()
    latest_status = str(existing_task.get("latest_status") or "").strip()
    if status in {"submitted", "polling", "still_running"}:
        return True
    if latest_status and latest_status not in {"completed", "answered", "history_fallback"}:
        return True
    return False


async def _call_customgpt_task(
    project_id: str,
    prompt_text: str,
    tool_name: str,
    action_id: Optional[str] = None,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> str:
    config_error = _require_config(project_id, tool_name)
    if config_error:
        return config_error

    prompt_text = str(prompt_text or "").strip()
    if not prompt_text:
        return f"{tool_name} received an empty promptText."

    if tool_name == "Assessment_Context_Expert" and action_id:
        prompt_text = _enrich_homeharvest_prompt(prompt_text)

    _log("specialist tool received prompt", tool=tool_name, project=project_id, action_id=action_id or "", prompt_preview=_safe_json_dumps(prompt_text[:800], 900))

    should_reuse_prompt_cache = _should_reuse_prompt_cache(tool_name, prompt_text, action_id)

    # Duplicate-proofing: non-HomeHarvest tools can reuse cached answers.
    # HomeHarvest/address/comps lookups default to fresh submissions so repeated
    # Copilot tests actually reach CustomGPT/Context Expert instead of returning
    # an old last_answer from the Render cache.
    existing_task = _find_existing_prompt_task(project_id, tool_name, prompt_text)
    if existing_task and should_reuse_prompt_cache:
        existing_answer = str(existing_task.get("last_answer") or "").strip()
        if existing_answer:
            _log("CACHE HIT - returning cached answer without CustomGPT submit", tool=tool_name, project=project_id, task_id=existing_task.get("task_id"))
            return existing_answer
        existing_task_id = str(existing_task.get("task_id") or "").strip()
        if existing_task_id:
            _log("CACHE HIT - rechecking existing task", tool=tool_name, project=project_id, task_id=existing_task_id)
            return await _check_customgpt_task_result(project_id=project_id, task_id=existing_task_id)

    if existing_task and _is_homeharvest_lookup(tool_name, prompt_text, action_id) and _should_reuse_existing_homeharvest_task(existing_task):
        existing_task_id = str(existing_task.get("task_id") or "").strip()
        if existing_task_id:
            _log("REUSING IN-FLIGHT HOMEHARVEST TASK", tool=tool_name, project=project_id, task_id=existing_task_id)
            return await _check_customgpt_task_result(project_id=project_id, task_id=existing_task_id)

    if existing_task and not should_reuse_prompt_cache:
        _log("BYPASSING PROMPT CACHE - fresh HomeHarvest/Assessment lookup", tool=tool_name, project=project_id, previous_task_id=existing_task.get("task_id"))

    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "application/json"}

    if not should_reuse_prompt_cache:
        task_name = f"ACES|{tool_name}|fresh|{int(time.time())}|{_stable_hash(prompt_text)}"
    else:
        task_name = f"ACES|{tool_name}|{_stable_hash(prompt_text)}"

    _log("SUBMITTING NEW CUSTOMGPT TASK", tool=tool_name, project=project_id, task_name=task_name)

    multipart: Dict[str, Any] = {
        "name": (None, task_name),
        "prompt": (None, prompt_text),
        "response_source": (None, "openai_content"),
        "agent_capability": (None, "optimal-choice"),
    }
    if action_id:
        multipart["action_overrides"] = (None, json.dumps({"enabled": [str(action_id)], "disabled": []}))

    timeout = httpx.Timeout(connect=15, read=35, write=35, pool=15)

    async with httpx.AsyncClient(timeout=timeout) as client:
        submit = await client.post(f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks", headers=headers, files=multipart)
        submit_data = await _read_json_or_text(submit)
        _log("CustomGPT submit response", tool=tool_name, project=project_id, http_status=submit.status_code, response_preview=_safe_json_dumps(submit_data, 1200))

        if submit.status_code >= 400:
            return f"{tool_name} task submit failed.\nHTTP status: {submit.status_code}\nResponse: {_safe_json_dumps(submit_data)}"

        task_id = None
        if isinstance(submit_data, dict):
            task_id = (submit_data.get("data") or {}).get("id")
        if not task_id:
            return f"{tool_name} task submit did not return a task id.\nResponse: {_safe_json_dumps(submit_data)}"

        task_id = str(task_id)
        cache_key = _remember_task(project_id, tool_name, prompt_text, task_id, "submitted", latest_status="submitted")
        base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, int(poll_seconds))

        message_id = None
        latest_status = "submitted"
        progress_log: List[str] = []
        completed_data: Dict[str, Any] = {}

        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            poll = await client.get(base, headers=headers)
            poll_data = await _read_json_or_text(poll)

            if poll.status_code >= 400:
                error_text = _safe_json_dumps(poll_data)
                _remember_task(project_id, tool_name, prompt_text, task_id, "poll_failed", latest_status=latest_status, progress_log=progress_log, error=error_text)
                return (
                    f"{tool_name} task poll failed.\n"
                    f"Task ID: {task_id}\n"
                    f"Project ID: {project_id}\n"
                    f"Cache key: {cache_key}\n"
                    f"HTTP status: {poll.status_code}\n"
                    f"Response: {error_text}"
                )

            data = poll_data.get("data", {}) if isinstance(poll_data, dict) else {}
            latest_status = str(data.get("status") or latest_status or "unknown")

            new_progress = _extract_progress_log_from_task_data(data)
            for item in new_progress:
                if not progress_log or progress_log[-1] != item:
                    progress_log.append(item)

            events = data.get("events", []) or []
            for ev in events:
                ev = ev or {}
                ev_data = ev.get("data", {}) or {}
                if ev.get("type") == "error":
                    error_message = str(ev_data.get("message", "Unknown error"))
                    _remember_task(project_id, tool_name, prompt_text, task_id, "task_failed", latest_status=latest_status, progress_log=progress_log, error=error_message)
                    return (
                        f"{tool_name} task failed.\n"
                        f"Task ID: {task_id}\n"
                        f"Project ID: {project_id}\n"
                        f"Cache key: {cache_key}\n"
                        f"Error: {error_message}"
                    )

            _remember_task(project_id, tool_name, prompt_text, task_id, "polling", latest_status=latest_status, progress_log=progress_log)

            if latest_status == "completed":
                completed_data = data
                result = data.get("result") or {}
                if isinstance(result, dict):
                    message_id = result.get("message_id")
                _remember_task(
                    project_id,
                    tool_name,
                    prompt_text,
                    task_id,
                    "completed",
                    latest_status=latest_status,
                    message_id=str(message_id) if message_id else None,
                    progress_log=progress_log,
                )
                break

        if latest_status != "completed":
            _remember_task(project_id, tool_name, prompt_text, task_id, "still_running", latest_status=latest_status, progress_log=progress_log)
            return _format_still_running_response(project_id, tool_name, prompt_text, task_id, cache_key, latest_status, progress_log)

        inline_answer = _extract_answer_from_message({"data": completed_data}) or _extract_answer_from_message(completed_data)
        if inline_answer:
            _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status=latest_status, message_id=str(message_id) if message_id else None, progress_log=progress_log, answer=inline_answer)
            return inline_answer

        if not message_id:
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status="history_fallback", progress_log=progress_log, answer=history_answer)
                return history_answer

            _remember_task(project_id, tool_name, prompt_text, task_id, "completed_no_message_id", latest_status=latest_status, progress_log=progress_log, error=_safe_json_dumps(completed_data))
            return (
                f"{tool_name} task completed but no message_id was returned.\n"
                f"Task ID: {task_id}\n"
                f"Project ID: {project_id}\n"
                f"Cache key: {cache_key}\n"
                f"Response: {_safe_json_dumps(completed_data)}"
            )

        answer = await _fetch_customgpt_final_message(client, project_id, task_id, str(message_id), headers)
        if answer.startswith("Final message fetch failed") or answer.startswith("Task completed but the final answer was empty"):
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status="history_fallback", message_id=str(message_id), progress_log=progress_log, answer=history_answer)
                return history_answer

            _remember_task(project_id, tool_name, prompt_text, task_id, "final_fetch_or_empty_failed", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, error=answer)
            return f"{tool_name} final retrieval failed.\nTask ID: {task_id}\nProject ID: {project_id}\nCache key: {cache_key}\n{answer}"

        _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, answer=answer)
        return answer


@mcp.tool
async def Community_Educator(promptText: str) -> str:
    """
    Use for public/taxpayer-facing process questions, exemptions, Notices of Value,
    protests, forms, deadlines, outreach, value freeze, and owner-facing explanations.
    Takes exactly one parameter: promptText.
    """
    return await _call_customgpt_task(COMMUNITY_PROJECT_ID, promptText, "Community_Educator", poll_seconds=DEFAULT_POLL_SECONDS)


@mcp.tool
async def Assessment_Context_Expert(promptText: str) -> str:
    """
    Use for address, situs, parcel/account, owner/property lookup, PRC, OD report,
    iasWorld export, property record card, HomeHarvest, comps, sales, values,
    exemptions on a record, record interpretation, and assessment context.
    Takes exactly one parameter: promptText.
    """
    action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(promptText) else None
    poll_seconds = HOMEHARVEST_POLL_SECONDS if action_id else DEFAULT_POLL_SECONDS
    _log("Assessment_Context_Expert invoked", homeharvest_enabled=bool(action_id), action_id=action_id or "", poll_seconds=poll_seconds)
    return await _call_customgpt_task(
        ASSESSMENT_PROJECT_ID,
        promptText,
        "Assessment_Context_Expert",
        action_id=action_id,
        poll_seconds=poll_seconds,
    )


@mcp.tool
async def Clear_Expectations(promptText: str) -> str:
    """
    Use for staff/HR/training, roles, onboarding, IAAO/USPAP, internal policy,
    expectations, benefits, and development questions. Takes exactly one parameter.
    """
    return await _call_customgpt_task(CLEAR_PROJECT_ID, promptText, "Clear_Expectations", poll_seconds=DEFAULT_POLL_SECONDS)


@mcp.tool
async def Compliance_Expert(promptText: str) -> str:
    """
    Use for legal/statutory questions, NMSA, regulations, case law, AG opinions,
    statutory interpretation, protest standards, exemption basis, valuation authority,
    and legal risk. Takes exactly one parameter: promptText.
    """
    return await _call_customgpt_task(COMPLIANCE_PROJECT_ID, promptText, "Compliance_Expert", poll_seconds=DEFAULT_POLL_SECONDS)


@mcp.tool
async def Check_CustomGPT_Task(projectId: str, taskId: str) -> str:
    """
    Use to check a previously created CustomGPT task when an A.C.E.S. specialist tool
    returned still_running. Takes projectId and taskId and returns the final answer
    if the task has completed.
    """
    return await _check_customgpt_task_result(project_id=str(projectId).strip(), task_id=str(taskId).strip())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
