import os
import json
import asyncio
import hashlib
import time
from typing import Optional, Any, Dict, List, Tuple

# FastMCP wants these as environment settings or HTTP-layer settings,

# not inside FastMCP(...).

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

# Set this in Render if you want to inspect cached task ids safely.

ACES_ADMIN_TOKEN = os.getenv("ACES_ADMIN_TOKEN", "").strip()

CUSTOMGPT_BASE = "https://app.customgpt.ai/api/v1"

# Render's filesystem is ephemeral. This survives while the instance is alive,

# but may reset after redeploy/cold start. The tool also returns Task ID in text.

TASK_CACHE_FILE = os.getenv("TASK_CACHE_FILE", "/tmp/aces_task_cache.json")

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _stable_hash(text: str) -> str:
value = text or ""
return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

def _safe_json_dumps(value: Any, max_len: int = 3000) -> str:
try:
text = json.dumps(value, ensure_ascii=False)
except Exception:
text = str(value)

```
if len(text) > max_len:
    return text[:max_len] + "...[truncated]"

return text
```

def _load_task_cache() -> Dict[str, Any]:
try:
if os.path.exists(TASK_CACHE_FILE):
with open(TASK_CACHE_FILE, "r", encoding="utf-8") as f:
loaded = json.load(f)
if isinstance(loaded, dict):
return loaded
except Exception as exc:
print(f"[task-cache] load failed: {exc}", flush=True)

```
return {}
```

def _save_task_cache(cache: Dict[str, Any]) -> None:
try:
with open(TASK_CACHE_FILE, "w", encoding="utf-8") as f:
json.dump(cache, f, ensure_ascii=False, indent=2)
except Exception as exc:
print(f"[task-cache] save failed: {exc}", flush=True)

TASK_CACHE: Dict[str, Any] = _load_task_cache()

def _task_cache_key(project_id: str, tool_name: str, prompt_text: str) -> str:
return f"{project_id}|{tool_name}|{_stable_hash(prompt_text)}"

def _find_task_cache_entry(project_id: str, task_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
for key, value in TASK_CACHE.items():
if not isinstance(value, dict):
continue
if str(value.get("project_id", "")) == str(project_id) and str(value.get("task_id", "")) == str(task_id):
return key, value
return None, None

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
) -> str:
key = _task_cache_key(project_id, tool_name, prompt_text)

```
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
        "message_id": str(message_id) if message_id else None,
        "prompt_hash": _stable_hash(prompt_text),
        "updated_at": _now_iso(),
    }
)

if "created_at" not in existing:
    existing["created_at"] = _now_iso()

if progress_log is not None:
    existing["progress_log"] = [str(x) for x in progress_log[-10:]]

if error:
    existing["error"] = str(error)

TASK_CACHE[key] = existing
_save_task_cache(TASK_CACHE)

print(
    f"[task-cache] saved tool={tool_name} project={project_id} "
    f"task_id={task_id} status={status} key={key}",
    flush=True,
)

return key
```

def _update_task_cache_by_task_id(
project_id: str,
task_id: str,
status: str,
latest_status: Optional[str] = None,
message_id: Optional[str] = None,
progress_log: Optional[List[str]] = None,
error: Optional[str] = None,
) -> str:
key, existing = _find_task_cache_entry(project_id, task_id)

```
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

TASK_CACHE[key] = existing
_save_task_cache(TASK_CACHE)

print(
    f"[task-cache] updated project={project_id} task_id={task_id} "
    f"status={status} key={key}",
    flush=True,
)

return key
```

def _admin_authorized(request) -> bool:
if not ACES_ADMIN_TOKEN:
return False

```
supplied_token = (
    request.headers.get("x-aces-admin-token", "")
    or request.query_params.get("token", "")
)

return supplied_token == ACES_ADMIN_TOKEN
```

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
return JSONResponse(
{
"enabled": False,
"message": "Set ACES_ADMIN_TOKEN in Render to enable this debug route.",
},
status_code=403,
)

```
if not _admin_authorized(request):
    return JSONResponse(
        {"error": "Unauthorized. Supply x-aces-admin-token header or ?token=..."},
        status_code=401,
    )

return JSONResponse(
    {
        "count": len(TASK_CACHE),
        "tasks": TASK_CACHE,
    }
)
```

@mcp.custom_route("/check-task", methods=["GET"])
async def check_task_route(request):
if not ACES_ADMIN_TOKEN:
return JSONResponse(
{
"enabled": False,
"message": "Set ACES_ADMIN_TOKEN in Render to enable this debug route.",
},
status_code=403,
)

```
if not _admin_authorized(request):
    return JSONResponse(
        {"error": "Unauthorized. Supply x-aces-admin-token header or ?token=..."},
        status_code=401,
    )

project_id = str(request.query_params.get("project_id") or ASSESSMENT_PROJECT_ID).strip()
task_id = str(request.query_params.get("task_id") or "").strip()

if not task_id:
    return JSONResponse(
        {"error": "Missing task_id query parameter."},
        status_code=400,
    )

result = await _check_customgpt_task_result(project_id=project_id, task_id=task_id)

return JSONResponse(
    {
        "project_id": project_id,
        "task_id": task_id,
        "result": result,
    }
)
```

def _require_config(project_id: str, tool_name: str) -> Optional[str]:
if not CUSTOMGPT_API_TOKEN:
return "Missing CUSTOMGPT_API_TOKEN on Render."

```
if not project_id:
    return f"Missing project id for {tool_name}. Set it in Render Environment."

return None
```

def _should_enable_homeharvest(prompt_text: str) -> bool:
text = (prompt_text or "").lower()
padded = f" {text} "

```
property_words = [
    "address",
    "situs",
    "parcel",
    "property",
    "homeharvest",
    "nearby sales",
    "nearby sale",
    "comps",
    "comparable",
    "sold properties",
    "public aggregator",
]

street_suffixes = [
    " ct",
    " court",
    " dr",
    " drive",
    " rd",
    " road",
    " st",
    " street",
    " ave",
    " avenue",
    " ln",
    " lane",
    " way",
    " blvd",
    " boulevard",
    " pl",
    " place",
    " cir",
    " circle",
    " trl",
    " trail",
]

explicit_mode = (
    "mode: address / homeharvest lookup" in text
    or "mode: record + homeharvest comp support" in text
)

property_request = any(word in text for word in property_words)
looks_like_nm_address = (
    (" nm" in padded or "new mexico" in text or "albuquerque" in text or "tijeras" in text)
    and any(suffix in padded for suffix in street_suffixes)
)

look_up_request = ("look up" in text or "lookup" in text or "search" in text)

return explicit_mode or property_request or (look_up_request and looks_like_nm_address)
```

def _enrich_homeharvest_prompt(prompt_text: str) -> str:
text = prompt_text or ""

```
if "homeharvest action rule" in text.lower():
    return text

return (
    "MODE: ADDRESS / HOMEHARVEST LOOKUP\n\n"
    "HOMEHARVEST ACTION RULE:\n"
    "- Use the enabled HomeHarvest custom action when the request is an address, nearby sale, or comp lookup.\n"
    "- Prefer operation homeharvestSearchProperties.\n"
    "- Use POST /properties/search.\n"
    "- Return staff-readable numbered cards, not raw JSON.\n"
    "- If no exact match appears, say no exact match was returned and summarize nearby/public aggregator results.\n"
    "- A no-result response is not a tool failure.\n\n"
    "STAFF REQUEST:\n"
    f"{text}"
)
```

def _detect_assessment_status_message(prompt_text: str) -> str:
text = (prompt_text or "").lower()

```
if "mode: address / homeharvest lookup" in text:
    return "Address lookup is still running. Check again in about a minute."

if "mode: record + homeharvest comp support" in text:
    return "The record and comp-support analysis is still running. Check again in about a minute."

return "The analysis is still running. Check again in about a minute."
```

async def _read_json_or_text(response: httpx.Response) -> Any:
text = response.text or ""

```
try:
    return response.json()
except Exception:
    return {"raw": text}
```

def _extract_answer_from_message(final_data: Any) -> str:
if not isinstance(final_data, dict):
return ""

```
data = final_data.get("data") or {}

if isinstance(data, dict):
    answer = (
        data.get("response")
        or data.get("openai_response")
        or data.get("agent_answer")
        or data.get("message")
        or data.get("content")
        or ""
    )

    if answer:
        return str(answer)

answer = (
    final_data.get("response")
    or final_data.get("openai_response")
    or final_data.get("agent_answer")
    or final_data.get("message")
    or final_data.get("content")
    or ""
)

return str(answer or "")
```

def _extract_progress_log_from_task_data(data: Dict[str, Any]) -> List[str]:
progress_log: List[str] = []

```
events = data.get("events", []) or []
for ev in events:
    ev = ev or {}
    ev_data = ev.get("data", {}) or {}
    if ev_data.get("current_task"):
        current_task = str(ev_data.get("current_task"))
        if not progress_log or progress_log[-1] != current_task:
            progress_log.append(current_task)

return progress_log
```

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
first_line = f"{tool_name} is still running. Check again in about a minute."

```
return (
    f"{first_line}\n"
    f"Task ID: {task_id}\n"
    f"Project ID: {project_id}\n"
    f"Tool: {tool_name}\n"
    f"Cache key: {cache_key}\n"
    f"Latest status: {latest_status or 'unknown'}\n"
    f"Progress: {_safe_json_dumps(progress_log[-10:], 1000)}"
)
```

async def _fetch_customgpt_final_message(
client: httpx.AsyncClient,
project_id: str,
task_id: str,
message_id: str,
headers: Dict[str, str],
) -> str:
base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"

```
final = await client.get(f"{base}/messages/{message_id}", headers=headers)
final_data = await _read_json_or_text(final)

if final.status_code >= 400:
    return (
        f"Final message fetch failed.\n"
        f"Project ID: {project_id}\n"
        f"Task ID: {task_id}\n"
        f"Message ID: {message_id}\n"
        f"HTTP status: {final.status_code}\n"
        f"Response: {_safe_json_dumps(final_data)}"
    )

answer = _extract_answer_from_message(final_data)

if not answer:
    return (
        f"Task completed but the final answer was empty.\n"
        f"Project ID: {project_id}\n"
        f"Task ID: {task_id}\n"
        f"Message ID: {message_id}\n"
        f"Final response: {_safe_json_dumps(final_data)}"
    )

return answer
```

async def _check_customgpt_task_result(project_id: str, task_id: str) -> str:
project_id = str(project_id or "").strip()
task_id = str(task_id or "").strip()

```
config_error = _require_config(project_id, "Check_CustomGPT_Task")
if config_error:
    return config_error

if not task_id:
    return "Missing taskId."

headers = {
    "Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}",
    "Accept": "application/json",
}

base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
timeout = httpx.Timeout(connect=15, read=35, write=35, pool=15)

async with httpx.AsyncClient(timeout=timeout) as client:
    poll = await client.get(base, headers=headers)
    poll_data = await _read_json_or_text(poll)

    if poll.status_code >= 400:
        error_text = _safe_json_dumps(poll_data)
        _update_task_cache_by_task_id(
            project_id=project_id,
            task_id=task_id,
            status="check_failed",
            latest_status="check_failed",
            error=error_text,
        )

        return (
            f"Task check failed.\n"
            f"Project ID: {project_id}\n"
            f"Task ID: {task_id}\n"
            f"HTTP status: {poll.status_code}\n"
            f"Response: {error_text}"
        )

    data = poll_data.get("data", {}) if isinstance(poll_data, dict) else {}
    latest_status = str(data.get("status") or "unknown")
    progress_log = _extract_progress_log_from_task_data(data)

    result = data.get("result") or {}
    message_id = result.get("message_id")

    if latest_status != "completed":
        _update_task_cache_by_task_id(
            project_id=project_id,
            task_id=task_id,
            status="still_running",
            latest_status=latest_status,
            progress_log=progress_log,
        )

        return (
            f"Task is not complete yet.\n"
            f"Project ID: {project_id}\n"
            f"Task ID: {task_id}\n"
            f"Latest status: {latest_status}\n"
            f"Progress: {_safe_json_dumps(progress_log[-10:], 1000)}"
        )

    if not message_id:
        _update_task_cache_by_task_id(
            project_id=project_id,
            task_id=task_id,
            status="completed_no_message_id",
            latest_status=latest_status,
            progress_log=progress_log,
        )

        return (
            f"Task completed but no message_id was returned.\n"
            f"Project ID: {project_id}\n"
            f"Task ID: {task_id}\n"
            f"Response: {_safe_json_dumps(data)}"
        )

    answer = await _fetch_customgpt_final_message(
        client=client,
        project_id=project_id,
        task_id=task_id,
        message_id=str(message_id),
        headers=headers,
    )

    if answer.startswith("Final message fetch failed") or answer.startswith("Task completed but the final answer was empty"):
        _update_task_cache_by_task_id(
            project_id=project_id,
            task_id=task_id,
            status="final_fetch_or_empty_failed",
            latest_status=latest_status,
            message_id=str(message_id),
            progress_log=progress_log,
            error=answer,
        )
    else:
        _update_task_cache_by_task_id(
            project_id=project_id,
            task_id=task_id,
            status="answered",
            latest_status=latest_status,
            message_id=str(message_id),
            progress_log=progress_log,
        )

    return answer
```

async def _call_customgpt_task(
project_id: str,
prompt_text: str,
tool_name: str,
action_id: Optional[str] = None,
poll_seconds: int = 120,
) -> str:
config_error = _require_config(project_id, tool_name)
if config_error:
return config_error

```
prompt_text = str(prompt_text or "").strip()

if not prompt_text:
    return f"{tool_name} received an empty promptText."

if tool_name == "Assessment_Context_Expert" and action_id:
    prompt_text = _enrich_homeharvest_prompt(prompt_text)

headers = {
    "Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}",
    "Accept": "application/json",
}

task_name = f"ACES|{tool_name}|{_stable_hash(prompt_text)}"

# CustomGPT Tasks API uses multipart/form-data.
# response_source uses the actual final answer content.
multipart: Dict[str, Any] = {
    "name": (None, task_name),
    "prompt": (None, prompt_text),
    "response_source": (None, "openai_content"),
    "agent_capability": (None, "optimal-choice"),
}

if action_id:
    multipart["action_overrides"] = (
        None,
        json.dumps(
            {
                "enabled": [str(action_id)],
                "disabled": [],
            }
        ),
    )

timeout = httpx.Timeout(connect=15, read=35, write=35, pool=15)

async with httpx.AsyncClient(timeout=timeout) as client:
    submit = await client.post(
        f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks",
        headers=headers,
        files=multipart,
    )

    submit_data = await _read_json_or_text(submit)

    if submit.status_code >= 400:
        return (
            f"{tool_name} task submit failed.\n"
            f"HTTP status: {submit.status_code}\n"
            f"Response: {_safe_json_dumps(submit_data)}"
        )

    task_id = None

    if isinstance(submit_data, dict):
        task_id = (submit_data.get("data") or {}).get("id")

    if not task_id:
        return (
            f"{tool_name} task submit did not return a task id.\n"
            f"Response: {_safe_json_dumps(submit_data)}"
        )

    task_id = str(task_id)

    cache_key = _remember_task(
        project_id=project_id,
        tool_name=tool_name,
        prompt_text=prompt_text,
        task_id=task_id,
        status="submitted",
        latest_status="submitted",
    )

    base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_seconds

    message_id = None
    latest_status = "submitted"
    progress_log: List[str] = []

    while loop.time() < deadline:
        await asyncio.sleep(3)

        poll = await client.get(base, headers=headers)
        poll_data = await _read_json_or_text(poll)

        if poll.status_code >= 400:
            error_text = _safe_json_dumps(poll_data)

            _remember_task(
                project_id=project_id,
                tool_name=tool_name,
                prompt_text=prompt_text,
                task_id=task_id,
                status="poll_failed",
                latest_status=latest_status,
                progress_log=progress_log,
                error=error_text,
            )

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

                _remember_task(
                    project_id=project_id,
                    tool_name=tool_name,
                    prompt_text=prompt_text,
                    task_id=task_id,
                    status="task_failed",
                    latest_status=latest_status,
                    progress_log=progress_log,
                    error=error_message,
                )

                return (
                    f"{tool_name} task failed.\n"
                    f"Task ID: {task_id}\n"
                    f"Project ID: {project_id}\n"
                    f"Cache key: {cache_key}\n"
                    f"Error: {error_message}"
                )

        _remember_task(
            project_id=project_id,
            tool_name=tool_name,
            prompt_text=prompt_text,
            task_id=task_id,
            status="polling",
            latest_status=latest_status,
            progress_log=progress_log,
        )

        if latest_status == "completed":
            result = data.get("result") or {}
            message_id = result.get("message_id")

            _remember_task(
                project_id=project_id,
                tool_name=tool_name,
                prompt_text=prompt_text,
                task_id=task_id,
                status="completed",
                latest_status=latest_status,
                message_id=str(message_id) if message_id else None,
                progress_log=progress_log,
            )

            break

    if not message_id:
        _remember_task(
            project_id=project_id,
            tool_name=tool_name,
            prompt_text=prompt_text,
            task_id=task_id,
            status="still_running",
            latest_status=latest_status,
            progress_log=progress_log,
        )

        return _format_still_running_response(
            project_id=project_id,
            tool_name=tool_name,
            prompt_text=prompt_text,
            task_id=task_id,
            cache_key=cache_key,
            latest_status=latest_status,
            progress_log=progress_log,
        )

    answer = await _fetch_customgpt_final_message(
        client=client,
        project_id=project_id,
        task_id=task_id,
        message_id=str(message_id),
        headers=headers,
    )

    if answer.startswith("Final message fetch failed") or answer.startswith("Task completed but the final answer was empty"):
        _remember_task(
            project_id=project_id,
            tool_name=tool_name,
            prompt_text=prompt_text,
            task_id=task_id,
            status="final_fetch_or_empty_failed",
            latest_status=latest_status,
            message_id=str(message_id),
            progress_log=progress_log,
            error=answer,
        )

        return (
            f"{tool_name} final retrieval failed.\n"
            f"Task ID: {task_id}\n"
            f"Project ID: {project_id}\n"
            f"Cache key: {cache_key}\n"
            f"{answer}"
        )

    _remember_task(
        project_id=project_id,
        tool_name=tool_name,
        prompt_text=prompt_text,
        task_id=task_id,
        status="answered",
        latest_status=latest_status,
        message_id=str(message_id),
        progress_log=progress_log,
    )

    return answer
```

@mcp.tool
async def Community_Educator(promptText: str) -> str:
"""
Use for public/taxpayer-facing process questions, exemptions, Notices of Value,
protests, forms, deadlines, outreach, value freeze, and owner-facing explanations.
Takes exactly one parameter: promptText.
"""
return await _call_customgpt_task(
COMMUNITY_PROJECT_ID,
promptText,
"Community_Educator",
)

@mcp.tool
async def Assessment_Context_Expert(promptText: str) -> str:
"""
Use for specific address, situs, parcel/account, owner/property lookup, PRC,
OD report, iasWorld export, property record card, HomeHarvest, comps, sales,
values, exemptions on a record, record interpretation, and assessment context.
Takes exactly one parameter: promptText.
"""
action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(promptText) else None

```
return await _call_customgpt_task(
    ASSESSMENT_PROJECT_ID,
    promptText,
    "Assessment_Context_Expert",
    action_id=action_id,
)
```

@mcp.tool
async def Clear_Expectations(promptText: str) -> str:
"""
Use for staff/HR/training, roles, onboarding, IAAO/USPAP, internal policy,
expectations, benefits, and development questions.
Takes exactly one parameter: promptText.
"""
return await _call_customgpt_task(
CLEAR_PROJECT_ID,
promptText,
"Clear_Expectations",
)

@mcp.tool
async def Compliance_Expert(promptText: str) -> str:
"""
Use for legal/statutory questions, NMSA, regulations, case law, AG opinions,
statutory interpretation, protest standards, exemption basis, valuation authority,
and legal risk. Takes exactly one parameter: promptText.
"""
return await _call_customgpt_task(
COMPLIANCE_PROJECT_ID,
promptText,
"Compliance_Expert",
)

@mcp.tool
async def Check_CustomGPT_Task(projectId: str, taskId: str) -> str:
"""
Use to check a previously created CustomGPT task when an A.C.E.S. specialist tool
returned still_running. Takes projectId and taskId and returns the final answer
if the task has completed.
"""
return await _check_customgpt_task_result(
project_id=str(projectId).strip(),
task_id=str(taskId).strip(),
)

if **name** == "**main**":
port = int(os.getenv("PORT", "10000"))
mcp.run(transport="http", host="0.0.0.0", port=port)
