import os
import json
import re
import asyncio
import hashlib
import time
import secrets
from datetime import datetime, timezone
from urllib.parse import quote_plus
from typing import Optional, Any, Dict, List, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None

# FastMCP reads these through settings/env for Streamable HTTP behavior.
os.environ.setdefault("FASTMCP_STATELESS_HTTP", "true")
os.environ.setdefault("FASTMCP_JSON_RESPONSE", "true")

import httpx
from fastmcp import FastMCP
from starlette.responses import JSONResponse, RedirectResponse, Response


mcp = FastMCP(
    "A.C.E.S. Specialist Tools",
    instructions=(
        "Internal Bernalillo County Assessor staff MCP server. "
        "Exposes nine tools: Community_Educator, Assessment_Context_Expert, "
        "Clear_Expectations, Compliance_Expert, Smart_Tasks, Check_CustomGPT_Task, "
        "ArcGIS_Public_Parcel_Lookup, ArcGIS_Public_Parcel_Map, and ArcGIS_Public_Candidate_Peers. "
        "The five CustomGPT specialist tools take exactly one promptText string and return plain text. "
        "Smart_Tasks submits to CustomGPT project 9262 using Plan & Act task mode for Smart Tasks, code, file, dashboard, and multi-step work. "
        "Check_CustomGPT_Task takes projectId and taskId to retrieve a delayed task result. "
        "ArcGIS_Public_Parcel_Lookup performs a read-only public parcel lookup. "
        "ArcGIS_Public_Parcel_Map returns BernCo Assessor map links and Google Maps routing links. "
        "ArcGIS_Public_Candidate_Peers returns ArcGIS-only candidate parcel peers for comp triage when living area/sale data is unavailable in GIS. "
        "Assessment_Context_Expert can pre-enrich HomeHarvest/address/comps requests with ArcGIS parcel context."
    ),
)


CUSTOMGPT_API_TOKEN = os.getenv("CUSTOMGPT_API_TOKEN", "").strip()

# Set these in Render Environment.
COMMUNITY_PROJECT_ID = os.getenv("COMMUNITY_PROJECT_ID", "").strip()
ASSESSMENT_PROJECT_ID = os.getenv("ASSESSMENT_PROJECT_ID", "94006").strip()
CLEAR_PROJECT_ID = os.getenv("CLEAR_PROJECT_ID", "").strip()
COMPLIANCE_PROJECT_ID = os.getenv("COMPLIANCE_PROJECT_ID", "").strip()

# CustomGPT project/agent with Plan & Act + Smart Tasks enabled.
SMART_TASKS_PROJECT_ID = os.getenv("SMART_TASKS_PROJECT_ID", "9262").strip()
SMART_TASKS_POLL_SECONDS = int(os.getenv("ACES_SMART_TASKS_POLL_SECONDS", "90"))
SMART_TASKS_AGENT_CAPABILITY = os.getenv(
    "ACES_SMART_TASKS_AGENT_CAPABILITY",
    "complex-tasks",
).strip() or "complex-tasks"
SMART_TASKS_RESPONSE_SOURCE = os.getenv(
    "ACES_SMART_TASKS_RESPONSE_SOURCE",
    "default",
).strip() or "default"

# HomeHarvest External API action ID inside CustomGPT project 94006.
HOMEHARVEST_ACTION_ID = os.getenv("HOMEHARVEST_ACTION_ID", "7").strip()

# Public Bernalillo County Assessor parcel layer.
# Default is the richer BernCo public MapServer layer. You can override this
# with ARCGIS_PUBLIC_PARCEL_LAYER_URL in Render if GIS publishes a new URL.
ARCGIS_PUBLIC_PARCEL_LAYER_URL = os.getenv(
    "ARCGIS_PUBLIC_PARCEL_LAYER_URL",
    "https://assessormap.bernco.gov/server/rest/services/GIS/Assessor_Parcels_Public/MapServer/0",
).rstrip("/")
ARCGIS_PUBLIC_PARCEL_MAX_RESULTS = int(os.getenv("ARCGIS_PUBLIC_PARCEL_MAX_RESULTS", "10"))

# ArcGIS-only candidate peer search. These are NOT final comparable sales;
# they are parcel peers used to guide HomeHarvest/CAMA/MLS enrichment when the
# public GIS layer lacks living-area and verified sale fields.
ARCGIS_CANDIDATE_PEER_MAX_RESULTS = int(os.getenv("ACES_ARCGIS_CANDIDATE_PEER_MAX_RESULTS", "25"))
ARCGIS_CANDIDATE_PEER_RADIUS_MILES = os.getenv("ACES_ARCGIS_CANDIDATE_PEER_RADIUS_MILES", "1,3,5,10").strip()


# BernCo Assessor Experience Builder app. The data source ID is from the public
# Assessor map URL fragment and is used with OBJECTID to open/select a parcel.
BERNCO_EXPERIENCE_APP_URL = os.getenv(
    "BERNCO_EXPERIENCE_APP_URL",
    "https://assessormap.bernco.gov/portal/apps/experiencebuilder/experience/?id=9757f76e51d048d393c44d6487771bf7",
).rstrip("/")
BERNCO_EXPERIENCE_DATA_SOURCE_ID = os.getenv(
    "BERNCO_EXPERIENCE_DATA_SOURCE_ID",
    "f0093002972e4f1e823c0368ea06cf75-19e83adb259-layer-9-1",
).strip()

# When true, Assessment_Context_Expert HomeHarvest/address/comps tasks are
# pre-enriched with public ArcGIS parcel context before the CustomGPT task is
# submitted. This lets HomeHarvest use the GIS subject parcel as an anchor.
ENRICH_HOMEHARVEST_WITH_ARCGIS = os.getenv(
    "ACES_ENRICH_HOMEHARVEST_WITH_ARCGIS", "true"
).strip().lower() not in {"0", "false", "no", "off"}

# Optional admin token for viewing /task-cache and /check-task.
ACES_ADMIN_TOKEN = os.getenv("ACES_ADMIN_TOKEN", "").strip()

CUSTOMGPT_BASE = os.getenv("CUSTOMGPT_BASE", "https://app.customgpt.ai/api/v1").rstrip("/")

# Render filesystem is ephemeral. This cache survives while the instance is alive,
# but may reset after redeploy/cold start. Always return Task ID + Project ID for
# unfinished tasks so Check_CustomGPT_Task can poll CustomGPT directly.
TASK_CACHE_FILE = os.getenv("TASK_CACHE_FILE", "/tmp/aces_task_cache.json")

# Generated Context Expert / CustomGPT artifact links.
# Set ACES_PUBLIC_BASE_URL to your Render public URL, for example:
#   https://aces-mcp-server.onrender.com
# The server returns staff-safe download links that proxy through Render without
# exposing the CustomGPT API token to Copilot/Teams users.
CONTEXT_EXPERT_FILE_LINKS_ENABLED = os.getenv(
    "ACES_CONTEXT_EXPERT_FILE_LINKS_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}
PUBLIC_BASE_URL = os.getenv(
    "ACES_PUBLIC_BASE_URL",
    os.getenv("PUBLIC_BASE_URL", "https://aces-mcp-server.onrender.com"),
).rstrip("/")
FILE_LINK_CACHE_FILE = os.getenv("ACES_FILE_LINK_CACHE_FILE", "/tmp/aces_file_link_cache.json")
FILE_LINK_TTL_SECONDS = int(os.getenv("ACES_FILE_LINK_TTL_SECONDS", "86400"))

# CustomGPT tasks are async. These values keep MCP calls from holding open too long
# while still allowing quick tasks to finish in one response.
DEFAULT_POLL_SECONDS = int(os.getenv("ACES_DEFAULT_POLL_SECONDS", "60"))
HOMEHARVEST_POLL_SECONDS = int(os.getenv("ACES_HOMEHARVEST_POLL_SECONDS", "25"))
REPORT_GENERATION_POLL_SECONDS = int(os.getenv("ACES_REPORT_GENERATION_POLL_SECONDS", "25"))
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


def _today_mountain_date():
    """Return today's date in America/Denver, falling back safely to UTC."""
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("America/Denver")).date()
    except Exception:
        pass
    return datetime.now(timezone.utc).date()


def _past_years_date_range(years: int = 10) -> Tuple[str, str]:
    """Return YYYY-MM-DD date_from/date_to for a rolling lookback window."""
    today = _today_mountain_date()
    years = max(1, int(years or 10))
    try:
        start = today.replace(year=today.year - years)
    except ValueError:
        # Handles leap-day current dates.
        start = today.replace(year=today.year - years, month=2, day=28)
    return start.isoformat(), today.isoformat()


def _has_any_word_or_phrase(text: str, terms: List[str]) -> bool:
    """Match whole words/phrases so 'comp' does not match 'complete' or 'compliance'."""
    value = re.sub(r"\s+", " ", (text or "").lower()).strip()
    for term in terms:
        term_value = re.sub(r"\s+", " ", (term or "").lower()).strip()
        if not term_value:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term_value) + r"(?![a-z0-9])"
        if re.search(pattern, value):
            return True
    return False


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

        wants_comps = _has_any_word_or_phrase(
            norm,
            ["comp", "comps", "nearby sale", "nearby sales", "sold", "sale", "sales", "market", "similar"],
        )
        wants_listing = _has_any_word_or_phrase(norm, ["listing", "listings", "active", "for sale"])
        wants_report = _request_needs_report_generation(raw)
        wants_lookup = _has_any_word_or_phrase(norm, ["look up", "lookup", "property", "address", "homeharvest"])

        if wants_report:
            intent = "report_generation"
        elif wants_comps:
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


def _load_file_link_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(FILE_LINK_CACHE_FILE):
            with open(FILE_LINK_CACHE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                # Drop expired links on startup/load.
                now = time.time()
                return {
                    str(key): value
                    for key, value in loaded.items()
                    if isinstance(value, dict) and float(value.get("expires_at", 0) or 0) > now
                }
    except Exception as exc:
        print(f"[file-link-cache] load failed: {exc}", flush=True)
    return {}


def _save_file_link_cache(cache: Dict[str, Any]) -> None:
    try:
        # Keep the JSON small by pruning expired entries before every save.
        now = time.time()
        expired = [
            key for key, value in cache.items()
            if not isinstance(value, dict) or float(value.get("expires_at", 0) or 0) <= now
        ]
        for key in expired:
            cache.pop(key, None)

        with open(FILE_LINK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[file-link-cache] save failed: {exc}", flush=True)


FILE_LINK_CACHE: Dict[str, Any] = _load_file_link_cache()


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




def _extract_id_from_text(text: str, label: str) -> str:
    """Extract an ID from a plain-text status block like 'Task ID: ...'."""
    pattern = re.compile(rf"(?im)^\s*{re.escape(label)}\s*:\s*([^\s]+)\s*$")
    match = pattern.search(text or "")
    return match.group(1).strip() if match else ""


def _normalize_aces_result(
    result_text: Any,
    project_id: str = "",
    task_id: str = "",
    default_failed_answer: str = "The lookup did not return usable results.",
) -> Dict[str, str]:
    """
    Convert existing MCP/plain-text tool results into the stable JSON contract
    Power Automate/Copilot Studio expects:
      status, answer, task_id, project_id

    Existing MCP tools return plain text. Long-running CustomGPT tasks include
    lines such as 'Task ID:' and 'Project ID:'. This helper preserves that text
    as the answer while exposing machine-readable status and IDs.
    """
    answer = str(result_text or "").strip()
    lower = answer.lower()

    extracted_task_id = _extract_id_from_text(answer, "Task ID")
    extracted_project_id = _extract_id_from_text(answer, "Project ID")

    final_task_id = str(task_id or extracted_task_id or "").strip()
    final_project_id = str(project_id or extracted_project_id or ASSESSMENT_PROJECT_ID or "").strip()

    if not answer:
        return {
            "status": "failed",
            "answer": default_failed_answer,
            "task_id": final_task_id,
            "project_id": final_project_id,
        }

    # Still running / polling language produced by _format_still_running_response
    # and _check_customgpt_task_result.
    still_processing_markers = [
        "still running",
        "not complete yet",
        "use check_customgpt_task",
        "latest status: submitted",
        "latest status: pending",
        "latest status: running",
        "latest status: processing",
        "latest status: queued",
    ]
    if any(marker in lower for marker in still_processing_markers):
        return {
            "status": "still_processing",
            "answer": answer,
            "task_id": final_task_id,
            "project_id": final_project_id,
        }

    task_not_found_markers = [
        "task not found",
        "customgpt task not found",
        "task check failed: 404",
        "http_status\": 404",
        "task was consumed",
        "task expired",
        "download link expired",
    ]
    if any(marker in lower for marker in task_not_found_markers):
        return {
            "status": "task_not_found",
            "answer": answer,
            "task_id": final_task_id,
            "project_id": final_project_id,
        }

    failed_markers = [
        "missing customgpt_api_token",
        "missing project id",
        "missing taskid",
        "missing task id",
        "missing task_id",
        "task submit failed",
        "task poll failed",
        "task failed",
        "task check failed",
        "final retrieval failed",
        "final message fetch failed",
        "completed but no message_id",
        "final answer was empty",
        "did not return a task id",
        "received an empty prompttext",
    ]
    if any(marker in lower for marker in failed_markers):
        return {
            "status": "failed",
            "answer": answer,
            "task_id": final_task_id,
            "project_id": final_project_id,
        }

    return {
        "status": "completed",
        "answer": answer,
        "task_id": final_task_id,
        "project_id": final_project_id,
    }


async def _request_json_or_empty(request) -> Dict[str, Any]:
    """Read JSON safely from Starlette request; return {} for empty/invalid JSON."""
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json_unauthorized(message: str = "Unauthorized. Supply x-aces-admin-token header or ?token=...") -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "answer": message,
            "task_id": "",
            "project_id": "",
        },
        status_code=401,
    )


def _json_not_configured(message: str = "Set ACES_ADMIN_TOKEN in Render to enable REST wrapper routes.") -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "answer": message,
            "task_id": "",
            "project_id": "",
        },
        status_code=403,
    )


def _require_rest_auth(request) -> Optional[JSONResponse]:
    """
    Protect Power Automate REST wrapper routes. Put the same token in your
    Power Automate HTTP action headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
    """
    if not ACES_ADMIN_TOKEN:
        return _json_not_configured()
    if not _admin_authorized(request):
        return _json_unauthorized()
    return None


# ---------------------------------------------------------------------------
# ArcGIS public parcel lookup helpers
# ---------------------------------------------------------------------------

# Use * so the tool works against both the older hosted FeatureServer and the
# richer BernCo MapServer schema. The REST response is normalized before it is
# returned to Copilot, and geometry is disabled by default.
ARCGIS_PUBLIC_PARCEL_OUT_FIELDS = "*"


def _arcgis_request_headers() -> Dict[str, str]:
    """Headers that keep BernCo ArcGIS/Cloudflare from treating server-side REST calls as suspicious."""
    return {
        "User-Agent": "ACES-MCP-Server/1.0 (+Bernalillo County Assessor internal staff tool)",
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"{BERNCO_EXPERIENCE_APP_URL}/",
    }


_ARCGIS_STREET_SUFFIX_REPLACEMENTS = {
    "COURT": "CT",
    "DRIVE": "DR",
    "ROAD": "RD",
    "STREET": "ST",
    "AVENUE": "AVE",
    "LANE": "LN",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "CIRCLE": "CIR",
    "TRAIL": "TRL",
    "TERRACE": "TER",
    "HIGHWAY": "HWY",
    "PARKWAY": "PKWY",
}

_ARCGIS_CITY_TRAILING_WORDS = [
    "ALBUQUERQUE",
    "TIJERAS",
    "CEDAR CREST",
    "EDGEWOOD",
    "LOS RANCHOS DE ALBUQUERQUE",
    "LOS RANCHOS",
    "CORRALES",
    "ISLETA",
    "SANDIA PARK",
]


def _clean_arcgis_sql_text(value: str) -> str:
    """Normalize text for ArcGIS SQL where clauses and escape single quotes."""
    cleaned = str(value or "").upper()
    cleaned = cleaned.replace("NEW MEXICO", "NM")
    cleaned = re.sub(r"[,;]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.replace("'", "''")


def _compact_digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _normalize_arcgis_situs_from_user_text(search_text: str) -> str:
    """
    Convert a user-entered address into the parcel layer's SITUSADD style.
    Example: '2 Lauren Taylor Court Tijeras NM 87059' -> '2 LAUREN TAYLOR CT'.

    This intentionally stops at the street suffix so typos in the city, such as
    'Tijersa', do not get included in the exact SITUSADD query.
    """
    value = _clean_arcgis_sql_text(search_text)
    if not re.match(r"^\d+\s+", value):
        return ""

    suffix_values = set(_ARCGIS_STREET_SUFFIX_REPLACEMENTS.keys()) | set(_ARCGIS_STREET_SUFFIX_REPLACEMENTS.values()) | {"WAY"}
    suffix_pattern = "|".join(sorted((re.escape(s) for s in suffix_values), key=len, reverse=True))

    # Prefer the first complete street-address span: house number + street + suffix.
    match = re.search(rf"\b(\d{{1,6}}\s+[A-Z0-9 .'-]{{1,90}}?\s+(?:{suffix_pattern}))\b", value)
    if match:
        value = match.group(1).strip()
    else:
        # Fallback: drop ZIP/state/city from the right side.
        value = re.sub(r"\s+\d{5}(?:-\d{4})?\s*$", "", value).strip()
        value = re.sub(r"\s+NM\s*$", "", value).strip()
        for city in sorted(_ARCGIS_CITY_TRAILING_WORDS, key=len, reverse=True):
            if value.endswith(" " + city):
                value = value[: -len(city)].strip()
                break

    parts = value.split()
    if len(parts) >= 2:
        last = parts[-1]
        if last in _ARCGIS_STREET_SUFFIX_REPLACEMENTS:
            parts[-1] = _ARCGIS_STREET_SUFFIX_REPLACEMENTS[last]
        value = " ".join(parts)

    return value.strip()


def _arcgis_where_candidates(search_text: str) -> List[Tuple[str, str]]:
    """
    Return query candidates in safest order: UPC exact, SITUSADD exact,
    full-address prefix, then broad fallback.
    """
    raw = str(search_text or "").strip()
    safe = _clean_arcgis_sql_text(raw)
    compact = _compact_digits(raw)
    candidates: List[Tuple[str, str]] = []

    if re.fullmatch(r"[0-9]{12,30}", compact):
        # UPC exists in both public layers. TXTUPC/PIN exist in the richer BernCo MapServer.
        candidates.append(("upc_exact", f"UPC = '{compact}' OR TXTUPC = '{compact}' OR PIN = '{compact}'"))

    situs = _normalize_arcgis_situs_from_user_text(raw)
    if situs:
        # SITUSADD exists in both public layers. Avoid CompleteSiteAddress here
        # because the richer MapServer does not expose that field.
        candidates.append(("situs_exact", f"SITUSADD = '{situs}'"))
        candidates.append(("situs_prefix", f"SITUSADD LIKE '{situs}%'"))

    if safe:
        # Use only fields that exist in the richer public MapServer.
        candidates.append(
            (
                "broad_text",
                " OR ".join(
                    [
                        f"SITUSADD LIKE '%{safe}%'",
                        f"SITUSADD2 LIKE '%{safe}%'",
                        f"OWNER LIKE '%{safe}%'",
                        f"UPC LIKE '%{safe}%'",
                        f"LEGALDESC LIKE '%{safe}%'",
                    ]
                ),
            )
        )

    # De-dupe while preserving order.
    seen = set()
    unique: List[Tuple[str, str]] = []
    for mode, where in candidates:
        if where not in seen:
            seen.add(where)
            unique.append((mode, where))
    return unique


def _compact_none_dict(value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a dict without empty/None values, or None if nothing remains."""
    cleaned = {
        key: val
        for key, val in (value or {}).items()
        if val is not None and val != ""
    }
    return cleaned or None


def _normalize_arcgis_parcel(attrs: Dict[str, Any]) -> Dict[str, Any]:
    owner_address = attrs.get("CompleteOwnerAddress") or " ".join(
        part for part in [attrs.get("OWNADD"), attrs.get("OWNADD2")] if part
    )
    situs_address = attrs.get("CompleteSiteAddress") or " ".join(
        part for part in [attrs.get("SITUSADD"), attrs.get("SITUSADD2")] if part
    )

    acreage = attrs.get("ACREAGE")
    if acreage is None:
        acreage = attrs.get("INTACRES")
    if acreage is None:
        acreage = attrs.get("PAR_CALCAC")

    year_built = attrs.get("DWEL_YRBLT") or attrs.get("COM_YRBLT")

    assessment_values = _compact_none_dict(
        {
            "land_value": attrs.get("LANDVALUE"),
            "ag_value": attrs.get("AGVALUE"),
            "improvement_value": attrs.get("IMPTVALUE"),
            "total_value": attrs.get("TOTVALUE"),
            "land_taxable": attrs.get("LANDTXBLE"),
            "improvement_taxable": attrs.get("IMPTTXBLE"),
            "total_taxable": attrs.get("TOTTXBLE"),
            "net_taxable": attrs.get("NETTAXABLE"),
        }
    )

    exemptions = _compact_none_dict(
        {
            "head_of_household": attrs.get("HOHEXEMP"),
            "veteran": attrs.get("VETEXEMP"),
            "other": attrs.get("OTHEREXEMP"),
            "total": attrs.get("TOTALEXEMP"),
        }
    )

    situs_components = _compact_none_dict(
        {
            "number": attrs.get("SITUSNUM"),
            "street": attrs.get("SITUSSTR"),
            "street_type": attrs.get("SITUSSTRTY"),
            "direction": attrs.get("SITUSDIREC"),
            "city": attrs.get("SITUSCITY"),
            "state": attrs.get("SITUSSTATE"),
            "zip": attrs.get("SITUSZIP"),
            "zip4": attrs.get("SITUSZIP2"),
        }
    )

    owner_components = _compact_none_dict(
        {
            "house_number": attrs.get("OWNHSENUM"),
            "sub_number": attrs.get("OWNSUBNUM"),
            "address_direction": attrs.get("OWNADDIR"),
            "street": attrs.get("OWNSTR"),
            "street_type": attrs.get("OWNSTRTYPE"),
            "direction": attrs.get("OWNDIRECT"),
            "box": attrs.get("OWNBOX"),
            "unit": attrs.get("OWNUNIT"),
            "unit_number": attrs.get("OWNUNITNO"),
            "city": attrs.get("OWNCITY"),
            "state": attrs.get("OWNSTATE"),
            "country": attrs.get("OWNCOUNTRY"),
            "zip": attrs.get("OWNZIPCODE"),
            "zip4": attrs.get("OWNZIP4"),
            "code": attrs.get("OWNCODE"),
        }
    )

    coordinates = _compact_none_dict(
        {
            "x": attrs.get("X_Coord"),
            "y": attrs.get("Y_Coord"),
        }
    )

    return {
        "object_id": attrs.get("OBJECTID"),
        "oid": attrs.get("OID"),
        "upc": attrs.get("UPC") or attrs.get("TXTUPC"),
        "txt_upc": attrs.get("TXTUPC"),
        "tax_year": attrs.get("TAXYR") or attrs.get("INTTAXYR"),
        "int_tax_year": attrs.get("INTTAXYR"),
        "pin": attrs.get("PIN"),
        "pid": attrs.get("PID"),
        "tid": attrs.get("TID"),
        "owner": attrs.get("OWNER"),
        "owner_address": owner_address or None,
        "owner_components": owner_components,
        "situs_address": situs_address or None,
        "situs_components": situs_components,
        "tax_district": attrs.get("TAXDIST"),
        "legal_description": attrs.get("LEGALDESC"),
        "document_number": attrs.get("DOCNUM"),
        "roll_type": attrs.get("ROLLTYPE"),
        "valuation_class": attrs.get("VALCLASS"),
        "property_class": attrs.get("PROPCLASS"),
        "land_use_code": attrs.get("LUC"),
        "land_use_description": attrs.get("LUC_MSG"),
        "class_description": attrs.get("C_DESCR"),
        "style": attrs.get("STYLE"),
        "year_built": year_built,
        "dwelling_year_built": attrs.get("DWEL_YRBLT"),
        "commercial_year_built": attrs.get("COM_YRBLT"),
        "acreage": acreage,
        "calculated_acres": attrs.get("PAR_CALCAC"),
        "int_acres": attrs.get("INTACRES"),
        "job_type": attrs.get("INTJOBTYPE"),
        "condominium": attrs.get("TXTCONDOMI"),
        "building": attrs.get("TXTBLDG"),
        "unit": attrs.get("TXTUNIT"),
        "floor": attrs.get("TXTFLR"),
        "duplicate_flag": attrs.get("DUPL"),
        "parcel_type": attrs.get("INTTYPE"),
        "coordinates": coordinates,
        "assessment_values": assessment_values,
        "exemptions": exemptions,
    }

def _fmt_arcgis_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _fmt_arcgis_money(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return str(value)


def _build_bernco_assessor_map_link(parcel: Dict[str, Any]) -> str:
    """Build a BernCo Assessor Experience Builder link that selects the parcel by OBJECTID."""
    object_id = parcel.get("object_id") or parcel.get("oid")
    if not object_id or not BERNCO_EXPERIENCE_DATA_SOURCE_ID or not BERNCO_EXPERIENCE_APP_URL:
        return ""

    return (
        f"{BERNCO_EXPERIENCE_APP_URL}"
        f"#data_s=id%3A{BERNCO_EXPERIENCE_DATA_SOURCE_ID}%3A{object_id}"
        f"&zoom_to_selection=true"
    )


def _build_google_maps_routing_links(parcel: Dict[str, Any]) -> Dict[str, str]:
    """Build Google Maps search/directions URLs from the public GIS situs address."""
    situs = parcel.get("situs_address") or ""
    if not situs:
        return {}

    destination = quote_plus(f"{situs}, Bernalillo County, NM")
    return {
        "google_maps_directions": (
            "https://www.google.com/maps/dir/?api=1"
            f"&destination={destination}"
            "&travelmode=driving"
        ),
        "google_maps_search": (
            "https://www.google.com/maps/search/?api=1"
            f"&query={destination}"
        ),
    }


def _format_arcgis_map_routing_text(result: Dict[str, Any]) -> str:
    """Format BernCo Assessor map and Google Maps routing links for staff."""
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    if status != "completed":
        return answer or "No matching public ArcGIS parcel record was found."

    lines = [
        answer,
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: Map/location support only. This is not a certified record, final appraisal, tax/legal decision, or routing guarantee.",
        "Note: BernCo Assessor Map opens the public Assessor map and selects the parcel by OBJECTID. Google Maps uses the public GIS situs address for driving directions.",
        "",
    ]

    for idx, item in enumerate(result.get("results") or [], start=1):
        bernco_map = _build_bernco_assessor_map_link(item)
        google_links = _build_google_maps_routing_links(item)

        lines.extend(
            [
                f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                f"   Owner: {item.get('owner') or ''}",
                f"   OBJECTID: {item.get('object_id') or ''}",
            ]
        )

        if bernco_map:
            lines.append(f"   BernCo Assessor Map: {bernco_map}")
        if google_links.get("google_maps_directions"):
            lines.append(f"   Google Maps Directions: {google_links['google_maps_directions']}")
        if google_links.get("google_maps_search"):
            lines.append(f"   Google Maps Search: {google_links['google_maps_search']}")

        lines.append("")

    return "\n".join(lines).strip()


def _format_arcgis_parcel_lookup_text(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    if status != "completed":
        return answer or "ArcGIS parcel lookup did not return usable results."

    lines = [
        answer,
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: Public GIS parcel context only. Verify final assessment details, value fields, exemption amount fields, ownership, and tax status in iasWorld.",
        "Note: Value, taxable, and exemption amount fields are public GIS attributes only; they are not verified sale prices, final tax amounts, exemption approvals, or legal determinations.",
        "",
    ]

    for idx, item in enumerate(result.get("results") or [], start=1):
        values = item.get("assessment_values") or {}
        exemptions = item.get("exemptions") or {}
        coords = item.get("coordinates") or {}

        lines.extend(
            [
                f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                f"   Tax Year: {item.get('tax_year') or ''}",
                f"   Owner: {item.get('owner') or ''}",
                f"   Owner Address: {item.get('owner_address') or ''}",
                f"   Legal Description: {item.get('legal_description') or ''}",
                f"   Roll/Class: {item.get('roll_type') or ''} / {item.get('valuation_class') or ''} / {item.get('property_class') or ''}",
                f"   Tax District / Doc: {item.get('tax_district') or ''} / {item.get('document_number') or ''}",
                f"   Land Use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                f"   Class/Style: {item.get('class_description') or ''} / {item.get('style') or ''}",
                f"   Built/Acres: {item.get('year_built') or ''} / {_fmt_arcgis_number(item.get('acreage'))}",
            ]
        )

        value_rows = [
            ("Land Value", values.get("land_value")),
            ("Agricultural Value", values.get("ag_value")),
            ("Improvement Value", values.get("improvement_value")),
            ("Total Value", values.get("total_value")),
            ("Land Taxable", values.get("land_taxable")),
            ("Improvement Taxable", values.get("improvement_taxable")),
            ("Total Taxable", values.get("total_taxable")),
            ("Net Taxable", values.get("net_taxable")),
        ]
        value_rows = [(label, value) for label, value in value_rows if value is not None and value != ""]
        if value_rows:
            lines.append("   Public GIS Value Fields:")
            for label, value in value_rows:
                lines.append(f"      {label}: {_fmt_arcgis_money(value)}")

        exemption_rows = [
            ("Head of Household Amount", exemptions.get("head_of_household")),
            ("Veteran Amount", exemptions.get("veteran")),
            ("Other Amount", exemptions.get("other")),
            ("Total Exemption Amount", exemptions.get("total")),
        ]
        exemption_rows = [(label, value) for label, value in exemption_rows if value is not None and value != ""]
        if exemption_rows:
            lines.append("   Public GIS Exemption Amount Fields:")
            for label, value in exemption_rows:
                lines.append(f"      {label}: {_fmt_arcgis_money(value)}")

        if value_rows or exemption_rows:
            lines.append("   Verification Note: Confirm value fields, exemption status/amounts, calculations, and taxability in iasWorld before relying on them.")

        lines.extend(
            [
                f"   X/Y: {_fmt_arcgis_number(coords.get('x'))} / {_fmt_arcgis_number(coords.get('y'))}",
                f"   OBJECTID: {item.get('object_id') or ''}",
                "",
            ]
        )

    return "\n".join(lines).strip()


async def _arcgis_public_parcel_lookup_result(
    search_text: str,
    max_results: int = ARCGIS_PUBLIC_PARCEL_MAX_RESULTS,
    return_geometry: bool = False,
) -> Dict[str, Any]:
    """Query the public BernCo parcel FeatureServer layer with exact-first fallback."""
    search_text = str(search_text or "").strip()
    if len(search_text) < 3:
        return {
            "status": "failed",
            "answer": "Provide an address, UPC, parcel ID, or owner name with at least 3 characters.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    try:
        max_results_int = max(1, min(int(max_results), 25))
    except Exception:
        max_results_int = ARCGIS_PUBLIC_PARCEL_MAX_RESULTS

    candidates = _arcgis_where_candidates(search_text)
    if not candidates:
        return {
            "status": "failed",
            "answer": "Could not build an ArcGIS query from the supplied search text.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    timeout = httpx.Timeout(connect=10, read=25, write=20, pool=10)
    last_error: Optional[Any] = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for query_mode, where_clause in candidates:
            params = {
                "f": "json",
                "where": where_clause,
                "outFields": ARCGIS_PUBLIC_PARCEL_OUT_FIELDS,
                "returnGeometry": "true" if return_geometry else "false",
                "resultRecordCount": str(max_results_int),
            }
            if return_geometry:
                # Layer coordinates are WKID 2903; use WGS84 when geometry is requested.
                params["outSR"] = "4326"

            try:
                response = await client.post(f"{ARCGIS_PUBLIC_PARCEL_LAYER_URL}/query", data=params, headers=_arcgis_request_headers())
                data = await _read_json_or_text(response)
            except Exception as exc:
                last_error = str(exc)
                continue

            if response.status_code >= 400:
                last_error = {"http_status": response.status_code, "response": data}
                continue

            if isinstance(data, dict) and data.get("error"):
                last_error = data.get("error")
                continue

            features = data.get("features", []) if isinstance(data, dict) else []
            if not features:
                continue

            results = [_normalize_arcgis_parcel(feature.get("attributes", {}) or {}) for feature in features]
            count = len(results)
            exact_note = "" if count == 1 else " Broad or fallback search returned multiple possible matches."
            transfer_note = " ArcGIS indicated there may be more matches than returned." if isinstance(data, dict) and data.get("exceededTransferLimit") else ""
            return {
                "status": "completed",
                "answer": (
                    f"Found {count} public ArcGIS parcel match(es). "
                    "Verify final assessment details in iasWorld before relying on them."
                    f"{exact_note}{transfer_note}"
                ).strip(),
                "task_id": "",
                "project_id": "",
                "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
                "layer_url": ARCGIS_PUBLIC_PARCEL_LAYER_URL,
                "query_mode": query_mode,
                "where": where_clause,
                "count": count,
                "results": results,
            }

    if last_error:
        return {
            "status": "failed",
            "answer": f"ArcGIS REST lookup failed or returned an error: {_safe_json_dumps(last_error, 1200)}",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    return {
        "status": "empty",
        "answer": "No matching public ArcGIS parcel records were found.",
        "task_id": "",
        "project_id": "",
        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
        "query_mode": candidates[-1][0] if candidates else "none",
        "where": candidates[-1][1] if candidates else "",
        "results": [],
    }



def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _arcgis_candidate_radius_values() -> List[float]:
    values: List[float] = []
    for raw in re.split(r"[,;\s]+", ARCGIS_CANDIDATE_PEER_RADIUS_MILES or ""):
        try:
            miles = float(raw)
        except Exception:
            continue
        if miles > 0:
            values.append(miles)
    return values or [1.0, 3.0, 5.0, 10.0]


def _arcgis_sql_equals(field: str, value: Any) -> str:
    return f"{field} = '{_clean_arcgis_sql_text(str(value))}'"


def _arcgis_candidate_where_stages(subject: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Build progressively broader, Cloudflare-safe where clauses for ArcGIS-only
    parcel peers. Do NOT include SQL exclusion logic such as UPC <> subject,
    OBJECTID <>, IS NULL, or OR here. Those are filtered client-side after the
    ArcGIS response. Keeping these as simple equality-only clauses avoids WAF
    blocks while still limiting the candidate pool.
    """
    tax_year = subject.get("tax_year") or subject.get("int_tax_year")
    roll_type = subject.get("roll_type")
    prop_class = subject.get("property_class")
    val_class = subject.get("valuation_class")
    luc = subject.get("land_use_code")
    tax_district = subject.get("tax_district")
    style = subject.get("style")

    base: List[str] = []
    if tax_year:
        base.append(_arcgis_sql_equals("TAXYR", tax_year))

    def build(extra: List[str]) -> str:
        clauses = base + [item for item in extra if item]
        return " AND ".join(clauses) if clauses else "1=1"

    stages: List[Tuple[str, str]] = []

    tight: List[str] = []
    if roll_type:
        tight.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        tight.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if val_class:
        tight.append(_arcgis_sql_equals("VALCLASS", val_class))
    if luc:
        tight.append(_arcgis_sql_equals("LUC", luc))
    if style:
        tight.append(_arcgis_sql_equals("STYLE", style))
    if tax_district:
        tight.append(_arcgis_sql_equals("TAXDIST", tax_district))
    if tight:
        stages.append(("same_class_use_style_taxdist", build(tight)))

    strong: List[str] = []
    if roll_type:
        strong.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        strong.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if luc:
        strong.append(_arcgis_sql_equals("LUC", luc))
    if strong:
        stages.append(("same_roll_propclass_luc", build(strong)))

    moderate: List[str] = []
    if roll_type:
        moderate.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        moderate.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if moderate:
        stages.append(("same_roll_propclass", build(moderate)))

    broad: List[str] = []
    if roll_type:
        broad.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if broad:
        stages.append(("same_roll_type", build(broad)))

    stages.append(("nearby_public_parcels", build([])))

    seen = set()
    unique: List[Tuple[str, str]] = []
    for name, where in stages:
        if where not in seen:
            seen.add(where)
            unique.append((name, where))
    return unique

def _arcgis_candidate_peer_score(subject: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Score public GIS candidate peers using only fields available in ArcGIS."""
    score = 0.0
    reasons: List[str] = []

    def same(field: str, label: str, points: float) -> None:
        nonlocal score
        left = str(subject.get(field) or "").strip().upper()
        right = str(candidate.get(field) or "").strip().upper()
        if left and right and left == right:
            score += points
            reasons.append(label)

    same("roll_type", "same roll type", 8)
    same("property_class", "same property class", 18)
    same("valuation_class", "same valuation class", 10)
    same("land_use_code", "same land use code", 22)
    same("tax_district", "same tax district", 8)
    same("style", "same style", 10)

    subject_year = _float_or_none(subject.get("year_built"))
    candidate_year = _float_or_none(candidate.get("year_built"))
    if subject_year is not None and candidate_year is not None:
        diff = abs(subject_year - candidate_year)
        if diff <= 5:
            score += 12
            reasons.append("year built within 5 years")
        elif diff <= 10:
            score += 9
            reasons.append("year built within 10 years")
        elif diff <= 20:
            score += 5
            reasons.append("year built within 20 years")

    subject_acres = _float_or_none(subject.get("acreage"))
    candidate_acres = _float_or_none(candidate.get("acreage"))
    if subject_acres and candidate_acres:
        ratio_diff = abs(candidate_acres - subject_acres) / max(subject_acres, 0.01)
        if ratio_diff <= 0.20:
            score += 10
            reasons.append("acreage within 20%")
        elif ratio_diff <= 0.50:
            score += 6
            reasons.append("acreage within 50%")
        elif ratio_diff <= 1.00:
            score += 3
            reasons.append("acreage within 100%")

    subject_coords = subject.get("coordinates") or {}
    cand_coords = candidate.get("coordinates") or {}
    sx = _float_or_none(subject_coords.get("x"))
    sy = _float_or_none(subject_coords.get("y"))
    cx = _float_or_none(cand_coords.get("x"))
    cy = _float_or_none(cand_coords.get("y"))
    distance_miles: Optional[float] = None
    if sx is not None and sy is not None and cx is not None and cy is not None:
        distance_miles = (((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5) / 5280.0
        if distance_miles <= 1:
            score += 12
            reasons.append("within 1 mile")
        elif distance_miles <= 3:
            score += 9
            reasons.append("within 3 miles")
        elif distance_miles <= 5:
            score += 6
            reasons.append("within 5 miles")
        elif distance_miles <= 10:
            score += 3
            reasons.append("within 10 miles")

    return {
        "candidate_score": round(min(score, 100.0), 1),
        "distance_miles": round(distance_miles, 3) if distance_miles is not None else None,
        "match_reasons": reasons[:12],
        "missing_for_final_comp": [
            "living/building square footage from an official or approved source",
            "verified sale date",
            "verified sale price",
        ],
    }


async def _arcgis_public_candidate_peers_result(
    search_text: str,
    max_results: int = 10,
) -> Dict[str, Any]:
    """
    Find ArcGIS-only candidate parcel peers around a subject.

    This is intentionally a comp-triage helper, not a comparable-sales tool.
    The public ArcGIS layer lacks living-area and verified sale fields, so the
    output must be enriched with HomeHarvest/CAMA/MLS before final comp use.
    """
    try:
        max_results_int = max(1, min(int(max_results), ARCGIS_CANDIDATE_PEER_MAX_RESULTS))
    except Exception:
        max_results_int = 10

    subject_lookup = await _arcgis_public_parcel_lookup_result(
        search_text=search_text,
        max_results=5,
        return_geometry=False,
    )
    if subject_lookup.get("status") != "completed" or int(subject_lookup.get("count") or 0) != 1:
        return {
            "status": "needs_subject_selection" if subject_lookup.get("status") == "completed" else subject_lookup.get("status", "empty"),
            "answer": (
                "ArcGIS candidate peer search needs exactly one subject parcel. "
                f"Subject lookup status: {subject_lookup.get('status')}; count: {subject_lookup.get('count', 0)}."
            ),
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": None,
            "peers": [],
        }

    subject = (subject_lookup.get("results") or [None])[0]
    if not isinstance(subject, dict):
        return {
            "status": "failed",
            "answer": "ArcGIS subject lookup did not return a usable normalized subject parcel.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": None,
            "peers": [],
        }

    coords = subject.get("coordinates") or {}
    sx = _float_or_none(coords.get("x"))
    sy = _float_or_none(coords.get("y"))
    if sx is None or sy is None:
        return {
            "status": "failed",
            "answer": "ArcGIS subject parcel did not include X_Coord/Y_Coord, so distance-based candidate peer search could not run.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": subject,
            "peers": [],
        }

    timeout = httpx.Timeout(connect=10, read=30, write=20, pool=10)
    stages = _arcgis_candidate_where_stages(subject)
    radii = _arcgis_candidate_radius_values()
    last_error: Optional[Any] = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for radius_miles in radii:
            for stage_name, where_clause in stages:
                params = {
                    "f": "json",
                    "where": where_clause,
                    "outFields": ARCGIS_PUBLIC_PARCEL_OUT_FIELDS,
                    "returnGeometry": "false",
                    "resultRecordCount": str(max(50, max_results_int * 5)),
                    "geometry": json.dumps({"x": sx, "y": sy, "spatialReference": {"wkid": 2903}}),
                    "geometryType": "esriGeometryPoint",
                    "inSR": "2903",
                    "spatialRel": "esriSpatialRelIntersects",
                    "distance": str(float(radius_miles) * 5280.0),
                    "units": "esriSRUnit_Foot",
                }
                try:
                    response = await client.post(f"{ARCGIS_PUBLIC_PARCEL_LAYER_URL}/query", data=params, headers=_arcgis_request_headers())
                    data = await _read_json_or_text(response)
                except Exception as exc:
                    last_error = str(exc)
                    continue

                if response.status_code >= 400:
                    last_error = {"http_status": response.status_code, "response": data}
                    continue
                if isinstance(data, dict) and data.get("error"):
                    last_error = data.get("error")
                    continue

                features = data.get("features", []) if isinstance(data, dict) else []
                peers: List[Dict[str, Any]] = []
                seen_keys = set()
                subject_upc = str(subject.get("upc") or subject.get("txt_upc") or "").strip()
                subject_oid = str(subject.get("object_id") or subject.get("oid") or "").strip()

                for feature in features:
                    peer = _normalize_arcgis_parcel(feature.get("attributes", {}) or {})
                    peer_upc = str(peer.get("upc") or peer.get("txt_upc") or "").strip()
                    peer_oid = str(peer.get("object_id") or peer.get("oid") or "").strip()
                    if subject_upc and peer_upc and peer_upc == subject_upc:
                        continue
                    if subject_oid and peer_oid and peer_oid == subject_oid:
                        continue
                    key = peer_upc or peer_oid or peer.get("situs_address")
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    peer.update(_arcgis_candidate_peer_score(subject, peer))
                    peers.append(peer)

                if peers:
                    peers.sort(key=lambda item: (item.get("candidate_score") or 0, -1 * (item.get("distance_miles") or 999)), reverse=True)
                    selected = peers[:max_results_int]
                    return {
                        "status": "completed",
                        "answer": (
                            f"Found {len(selected)} ArcGIS candidate parcel peer(s) within {radius_miles:g} mile(s) "
                            f"using stage '{stage_name}'. These are not final comparable sales because ArcGIS does not provide living area or verified sale fields."
                        ),
                        "task_id": "",
                        "project_id": "",
                        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
                        "layer_url": ARCGIS_PUBLIC_PARCEL_LAYER_URL,
                        "subject_lookup": subject_lookup,
                        "subject": subject,
                        "radius_miles": radius_miles,
                        "query_stage": stage_name,
                        "where": where_clause,
                        "count": len(selected),
                        "peers": selected,
                    }

    if last_error:
        return {
            "status": "failed",
            "answer": f"ArcGIS candidate peer search failed or returned an error: {_safe_json_dumps(last_error, 1200)}",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": subject,
            "peers": [],
        }

    return {
        "status": "empty",
        "answer": "No ArcGIS candidate parcel peers were found in the configured search radii.",
        "task_id": "",
        "project_id": "",
        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
        "subject_lookup": subject_lookup,
        "subject": subject,
        "peers": [],
    }


def _format_arcgis_candidate_peers_text(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    lines = [
        answer or "ArcGIS candidate peer search did not return usable results.",
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: ArcGIS-only candidate parcel peers. These are not final comparable sales because the public GIS layer does not include living area, verified sale date, or verified sale price.",
        "Use: Enrich with HomeHarvest/public aggregator data and verify final use in iasWorld/CAMA, MLS, deed/sales records, or another office-approved source.",
        "",
    ]

    subject = result.get("subject") or {}
    if isinstance(subject, dict) and subject:
        lines.extend(
            [
                "Subject Anchor",
                f"   Situs: {subject.get('situs_address') or ''}",
                f"   UPC/PIN: {subject.get('upc') or ''} / {subject.get('pin') or ''}",
                f"   Class/LUC/Style: {subject.get('property_class') or ''} / {subject.get('land_use_code') or ''} {subject.get('land_use_description') or ''} / {subject.get('style') or ''}",
                f"   Built/Acres: {subject.get('year_built') or ''} / {_fmt_arcgis_number(subject.get('acreage'))}",
                "",
            ]
        )

    if status == "completed":
        for idx, item in enumerate(result.get("peers") or [], start=1):
            reasons = "; ".join(item.get("match_reasons") or [])
            lines.extend(
                [
                    f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                    f"   Candidate Score: {item.get('candidate_score')}/100 | Distance: {_fmt_arcgis_number(item.get('distance_miles'))} mi",
                    f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                    f"   Class/LUC/Style: {item.get('property_class') or ''} / {item.get('land_use_code') or ''} {item.get('land_use_description') or ''} / {item.get('style') or ''}",
                    f"   Built/Acres: {item.get('year_built') or ''} / {_fmt_arcgis_number(item.get('acreage'))}",
                    f"   Reasons: {reasons}",
                    "   Missing for final comp use: living/building sqft, verified sale date, verified sale price.",
                    "",
                ]
            )

    return "\n".join(lines).strip()


def _request_needs_arcgis_candidate_peers(prompt_text: str) -> bool:
    """True for comp/sold-sale requests where ArcGIS peer candidates can help triage."""
    text = (prompt_text or "").lower()
    terms = [
        "comp", "comps", "comparable", "comparables", "candidate comps",
        "similar properties", "nearby sales", "nearby sale", "recent sales",
        "sold properties", "sold property", "sold homes", "sales nearby",
        "market support", "market value support",
    ]
    return _has_any_word_or_phrase(text, terms)


def _format_arcgis_candidate_peers_for_homeharvest(result: Dict[str, Any]) -> str:
    """Compact ArcGIS peer block for the Context Expert/HomeHarvest prompt."""
    lines = [
        "PUBLIC ARCGIS CANDIDATE PARCEL PEERS:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Candidate peer/triage support only; these are not final comparable sales.",
        "- Limitation: Public GIS does not include living/building square footage, verified sale date, or verified sale price.",
        "- Required next step: use HomeHarvest/public aggregator data and/or iasWorld/CAMA/MLS/deed records to verify living area and sale facts before final comp use.",
        f"- Peer search status: {result.get('status') or 'unknown'}",
        f"- Peer search answer: {result.get('answer') or ''}",
    ]

    if result.get("status") == "completed":
        lines.extend(
            [
                f"- Radius used: {result.get('radius_miles')} mile(s)",
                f"- Query stage used: {result.get('query_stage') or ''}",
                f"- Candidate count: {result.get('count') or len(result.get('peers') or [])}",
            ]
        )
        for idx, item in enumerate((result.get("peers") or [])[:10], start=1):
            reasons = "; ".join(item.get("match_reasons") or [])
            lines.append(
                f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                f"Score {item.get('candidate_score')} | Distance {item.get('distance_miles')} mi | "
                f"Class {item.get('property_class') or ''} | LUC {item.get('land_use_code') or ''} | "
                f"Style {item.get('style') or ''} | Built {item.get('year_built') or ''} | "
                f"Acres {item.get('acreage') if item.get('acreage') is not None else ''} | Reasons: {reasons}"
            )
    else:
        subject_lookup = result.get("subject_lookup") or {}
        if isinstance(subject_lookup, dict) and subject_lookup.get("answer"):
            lines.append(f"- Subject lookup note: {subject_lookup.get('answer')}")

    lines.extend(
        [
            "",
            "ARCGIS PEER USE RULES FOR HOMEHARVEST:",
            "- Try the candidate peer addresses as HomeHarvest sold-property searches when the staff request asks for comps/sales.",
            "- Keep any HomeHarvest subject-property facts separate from ArcGIS subject identity/assessment context.",
            "- Exclude candidate peer rows that remain missing living sqft, verified sold date, or verified sold price.",
            "- Never convert ArcGIS assessed/taxable/exemption/list/estimate values into sale prices.",
        ]
    )
    return "\n".join(lines).strip()



def _extract_arcgis_search_text_from_prompt(prompt_text: str) -> str:
    """
    Pull the best address/UPC search value out of an A.C.E.S/HomeHarvest prompt.
    Prefer labeled address blocks, then the first street-address pattern, then UPC.
    """
    text = str(prompt_text or "")

    for label in ["Address/search area:", "Address:", "Situs:", "Subject address:"]:
        block = _extract_label_block(
            text,
            label,
            ["Staff request:", "Instructions:", "Uploaded record transcription:", "HOMEHARVEST ACTION RULE:", "COMP SEARCH QUALITY RULES:"],
        )
        if block:
            candidate = block.strip().splitlines()[0].strip(" -")
            if candidate:
                return candidate

    compact = _compact_digits(text)
    if re.fullmatch(r"[0-9]{12,30}", compact):
        return compact

    street_suffix = (
        r"ct|court|dr|drive|rd|road|st|street|ave|avenue|ln|lane|way|"
        r"blvd|boulevard|pl|place|cir|circle|trl|trail|ter|terrace|hwy|highway|pkwy|parkway"
    )
    match = re.search(
        rf"\b\d{{1,6}}\s+[A-Za-z0-9 .'-]{{2,80}}?\s+(?:{street_suffix})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return re.sub(r"\s+", " ", match.group(0)).strip(" ,")

    stripped = text.strip()
    if stripped and len(stripped) <= 160:
        return stripped
    return ""


def _format_arcgis_context_for_homeharvest(result: Dict[str, Any], search_text: str) -> str:
    """Create a compact prompt block that CustomGPT/HomeHarvest can use as subject context."""
    status = str(result.get("status") or "")
    lines = [
        "PUBLIC ARCGIS PARCEL CONTEXT:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Subject parcel/GIS context only; verify final assessment details in iasWorld.",
        "- Do not treat GIS attributes as verified sale prices, exemption approvals/status, tax status, or a certified record.",
        f"- Search used: {search_text}",
        f"- GIS lookup status: {status or 'unknown'}",
    ]

    if status == "completed":
        results = result.get("results") or []
        lines.append(f"- Match count: {len(results)}")
        if len(results) == 1:
            item = results[0]
            values = item.get("assessment_values") or {}
            exemptions = item.get("exemptions") or {}
            coords = item.get("coordinates") or {}
            lines.extend(
                [
                    "- Subject anchor: exact/single public GIS match.",
                    f"- UPC: {item.get('upc') or ''}",
                    f"- PIN: {item.get('pin') or ''}",
                    f"- PID/TID: {item.get('pid') or ''} / {item.get('tid') or ''}",
                    f"- Tax year: {item.get('tax_year') or ''}",
                    f"- Situs address: {item.get('situs_address') or ''}",
                    f"- Owner: {item.get('owner') or ''}",
                    f"- Owner address: {item.get('owner_address') or ''}",
                    f"- Tax district: {item.get('tax_district') or ''}",
                    f"- Legal description: {item.get('legal_description') or ''}",
                    f"- Document number: {item.get('document_number') or ''}",
                    f"- Roll type: {item.get('roll_type') or ''}",
                    f"- Valuation class: {item.get('valuation_class') or ''}",
                    f"- Property class: {item.get('property_class') or ''}",
                    f"- Land use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                    f"- Class description: {item.get('class_description') or ''}",
                    f"- Style: {item.get('style') or ''}",
                    f"- Year built: {item.get('year_built') or ''}",
                    f"- Acreage: {item.get('acreage') if item.get('acreage') is not None else ''}",
                    f"- Land value: {values.get('land_value', '')}",
                    f"- Improvement value: {values.get('improvement_value', '')}",
                    f"- Total value: {values.get('total_value', '')}",
                    f"- Total taxable: {values.get('total_taxable', '')}",
                    f"- Net taxable: {values.get('net_taxable', '')}",
                    f"- Public GIS exemption amount fields shown: HOH {exemptions.get('head_of_household', '')}; Veteran {exemptions.get('veteran', '')}; Other {exemptions.get('other', '')}; Total {exemptions.get('total', '')}",
                    f"- Coordinates: X {coords.get('x', '')}; Y {coords.get('y', '')}",
                    f"- OBJECTID: {item.get('object_id') or ''}",
                ]
            )
        else:
            lines.append("- Subject anchor: multiple public GIS candidates; ask/choose carefully and do not assume one is correct.")
            for idx, item in enumerate(results[:5], start=1):
                lines.append(
                    f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                    f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                    f"Class {item.get('valuation_class') or ''}/{item.get('property_class') or ''} | "
                    f"LUC {item.get('land_use_code') or ''} | "
                    f"Built {item.get('year_built') or ''} | "
                    f"Acreage {item.get('acreage') if item.get('acreage') is not None else ''}"
                )
        where = result.get("where")
        mode = result.get("query_mode")
        if where:
            lines.append(f"- ArcGIS query mode: {mode}; where: {where}")
    else:
        lines.extend(
            [
                f"- ArcGIS answer: {result.get('answer') or 'No usable GIS context.'}",
                "- Continue with HomeHarvest if the staff request requires market/listing/comps data, but state that GIS context was not found.",
            ]
        )

    lines.extend(
        [
            "",
            "GIS + HOMEHARVEST INSTRUCTIONS:",
            "- Use the GIS parcel as the subject identity/geography/assessment anchor when a single match is present.",
            "- Also search HomeHarvest/public aggregator data for the subject address when available; keep those unofficial subject market/characteristic fields separate from ArcGIS fields.",
            "- For HomeHarvest comps, search around the GIS situs address and prefer residential results consistent with property class, valuation class, land use, year built, style, acreage, tax district, and location when available.",
            "- If PUBLIC ARCGIS CANDIDATE PARCEL PEERS is present, use those addresses as peer-search leads only; do not treat them as final comps until HomeHarvest/CAMA/MLS/deed data confirms living sqft, sold date, and sold price.",
            "- Never use GIS assessment values, taxable values, exemption amount fields, AVMs, Zestimates, estimates, or list prices as sale prices, comp prices, exemption approvals, or tax/legal determinations.",
            "- Return the GIS subject context first, then HomeHarvest subject facts if found, then unofficial HomeHarvest/public-aggregator candidate results.",
            "- Clearly label HomeHarvest results as unofficial public-aggregator candidates, not verified sales or final appraisal comps.",
            "- Do not mention an interactive map, file manager, generated file, download, attachment, report, or exported view unless the current tool response includes an actual generated_files item or downloadable link.",
        ]
    )
    return "\n".join(lines).strip()

async def _enrich_homeharvest_prompt_with_arcgis(prompt_text: str) -> str:
    """Prepend public ArcGIS parcel context to HomeHarvest/address/comps prompts."""
    text = str(prompt_text or "")
    if not ENRICH_HOMEHARVEST_WITH_ARCGIS:
        return text
    if "PUBLIC ARCGIS PARCEL CONTEXT:" in text:
        return text

    search_text = _extract_arcgis_search_text_from_prompt(text)
    if not search_text:
        return text + "\n\nPUBLIC ARCGIS PARCEL CONTEXT:\n- ArcGIS pre-check was not run because no address or UPC could be extracted."

    try:
        gis_result = await _arcgis_public_parcel_lookup_result(search_text=search_text, max_results=5, return_geometry=False)
        context_block = _format_arcgis_context_for_homeharvest(gis_result, search_text)
        peer_block = ""
        if _request_needs_arcgis_candidate_peers(text):
            peer_result = await _arcgis_public_candidate_peers_result(search_text=search_text, max_results=10)
            peer_block = _format_arcgis_candidate_peers_for_homeharvest(peer_result)
        _log(
            "ArcGIS pre-check for HomeHarvest",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
            candidate_peers=bool(peer_block),
        )
        return text + "\n\n" + context_block + (("\n\n" + peer_block) if peer_block else "")
    except Exception as exc:
        _log("ArcGIS pre-check failed", error=str(exc))
        return (
            text
            + "\n\nPUBLIC ARCGIS PARCEL CONTEXT:\n"
            + f"- ArcGIS pre-check failed before HomeHarvest submission: {exc}\n"
            + "- Continue with HomeHarvest if the request requires public aggregator/comps data and disclose that GIS pre-check failed."
        )


def _format_arcgis_context_for_report_generation(
    result: Dict[str, Any],
    search_text: str,
    include_homeharvest: bool = False,
) -> str:
    """Create ArcGIS subject context plus report-generation instructions for Context Expert."""
    status = str(result.get("status") or "")
    mode = (
        "MODE: ADDRESS REPORT + HOMEHARVEST MARKET SUPPORT"
        if include_homeharvest
        else "MODE: ADDRESS REPORT GENERATION"
    )
    lines = [
        mode,
        "",
        "PUBLIC ARCGIS PARCEL CONTEXT:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Subject parcel/GIS context only; verify final assessment details in iasWorld.",
        "- Do not treat GIS attributes as verified sale prices, tax status, exemption approvals/status, or a certified record.",
        f"- Search used: {search_text}",
        f"- GIS lookup status: {status or 'unknown'}",
    ]

    if status == "completed":
        results = result.get("results") or []
        lines.append(f"- Match count: {len(results)}")
        if len(results) == 1:
            item = results[0]
            values = item.get("assessment_values") or {}
            exemptions = item.get("exemptions") or {}
            coords = item.get("coordinates") or {}
            lines.extend(
                [
                    "- Subject anchor: exact/single public GIS match.",
                    f"- UPC: {item.get('upc') or ''}",
                    f"- PIN: {item.get('pin') or ''}",
                    f"- PID/TID: {item.get('pid') or ''} / {item.get('tid') or ''}",
                    f"- Tax year: {item.get('tax_year') or ''}",
                    f"- Situs address: {item.get('situs_address') or ''}",
                    f"- Owner: {item.get('owner') or ''}",
                    f"- Owner address: {item.get('owner_address') or ''}",
                    f"- Tax district: {item.get('tax_district') or ''}",
                    f"- Legal description: {item.get('legal_description') or ''}",
                    f"- Document number: {item.get('document_number') or ''}",
                    f"- Roll type: {item.get('roll_type') or ''}",
                    f"- Valuation class: {item.get('valuation_class') or ''}",
                    f"- Property class: {item.get('property_class') or ''}",
                    f"- Land use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                    f"- Class description: {item.get('class_description') or ''}",
                    f"- Style: {item.get('style') or ''}",
                    f"- Year built: {item.get('year_built') or ''}",
                    f"- Acreage: {item.get('acreage') if item.get('acreage') is not None else ''}",
                    f"- Land value: {values.get('land_value', '')}",
                    f"- Improvement value: {values.get('improvement_value', '')}",
                    f"- Total value: {values.get('total_value', '')}",
                    f"- Total taxable: {values.get('total_taxable', '')}",
                    f"- Net taxable: {values.get('net_taxable', '')}",
                    f"- Public GIS exemption amount fields shown: HOH {exemptions.get('head_of_household', '')}; Veteran {exemptions.get('veteran', '')}; Other {exemptions.get('other', '')}; Total {exemptions.get('total', '')}",
                    f"- Coordinates: X {coords.get('x', '')}; Y {coords.get('y', '')}",
                    f"- OBJECTID: {item.get('object_id') or ''}",
                ]
            )
        else:
            lines.append("- Subject anchor: multiple public GIS candidates; do not assume one is correct.")
            for idx, item in enumerate(results[:5], start=1):
                lines.append(
                    f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                    f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                    f"Class {item.get('valuation_class') or ''}/{item.get('property_class') or ''} | "
                    f"LUC {item.get('land_use_code') or ''} | "
                    f"Built {item.get('year_built') or ''} | "
                    f"Acreage {item.get('acreage') if item.get('acreage') is not None else ''}"
                )
    else:
        lines.extend(
            [
                f"- ArcGIS answer: {result.get('answer') or 'No usable GIS context.'}",
                "- Continue with Context Expert if the staff request requires report/file generation, but disclose that GIS context was not found.",
            ]
        )

    lines.extend(
        [
            "",
            "REPORT GENERATION INSTRUCTIONS:",
            "- Preserve the staff request exactly and create a staff-readable property/report response from the available context.",
            "- Include a Public GIS Parcel Context section using the ArcGIS context above.",
            "- If the Context Expert can create a downloadable artifact/file for this request, generate it.",
            "- Do not invent missing official fields, values, sales, comps, ownership conclusions, exemption approvals, tax status, or legal/appraisal conclusions.",
            "- Label ArcGIS as public GIS context only and require iasWorld verification before final use.",
            "- If no downloadable artifact is created, return the text report and do not claim that a file was generated.",
        ]
    )

    if include_homeharvest:
        lines.extend(
            [
                "",
                "HOMEHARVEST REPORT INSTRUCTIONS:",
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support.",
                "- Include a HomeHarvest/Public Aggregator Market Support section in both the text answer and any generated report file.",
                "- Search HomeHarvest/public aggregator data for the subject address itself when available, then search around the ArcGIS situs/subject anchor when a single ArcGIS match is present.",
                "- Keep ArcGIS subject identity/assessment context separate from unofficial HomeHarvest subject facts such as sqft, beds, baths, sale/listing fields, or market characteristics.",
                "- If ArcGIS candidate parcel peers are provided, use those addresses as sold-property search leads only and verify living sqft, sold date, and sold price before final comp use.",
                "- Prefer residential sales/listings consistent with property class, valuation class, land use, year built, acreage, tax district, and location when available.",
                "- Label HomeHarvest rows as unofficial public-aggregator data, not verified MLS, iasWorld/CAMA, legal, tax, valuation, exemption, ownership, or sale data.",
                "- Do not use AVMs, assessed values, taxable values, exemption amounts, estimates, or list/public-aggregator prices as verified sale prices.",
                "- If HomeHarvest returns no usable rows, include that limitation instead of inventing market data.",
            ]
        )

    return "\n".join(lines).strip()


async def _enrich_report_generation_prompt_with_arcgis(
    prompt_text: str,
    include_homeharvest: bool = False,
) -> str:
    """Prepend public ArcGIS parcel context for address/parcel report-generation tasks."""
    text = str(prompt_text or "").strip()
    if "REPORT GENERATION INSTRUCTIONS:" in text and "PUBLIC ARCGIS PARCEL CONTEXT:" in text:
        if include_homeharvest and "HOMEHARVEST REPORT INSTRUCTIONS:" not in text:
            return text + (
                "\n\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support.\n"
                "- Include HomeHarvest/Public Aggregator Market Support in the text answer and any generated report file.\n"
                "- Label HomeHarvest data as unofficial and not verified MLS, iasWorld/CAMA, legal, tax, valuation, exemption, ownership, or sale data."
            )
        return text

    mode = (
        "MODE: ADDRESS REPORT + HOMEHARVEST MARKET SUPPORT"
        if include_homeharvest
        else "MODE: REPORT GENERATION"
    )

    search_text = _extract_arcgis_search_text_from_prompt(text)
    if not search_text:
        extra = ""
        if include_homeharvest:
            extra = (
                "\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support if the request contains enough location context.\n"
                "- Label HomeHarvest data as unofficial and not verified sales or official assessment data.\n"
            )
        return (
            f"{mode}\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Create the requested staff-readable report/file if supported by Context Expert.\n"
            "- No ArcGIS pre-check was run because no address or UPC could be extracted.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions.\n"
            f"{extra}"
        )

    try:
        gis_result = await _arcgis_public_parcel_lookup_result(search_text=search_text, max_results=5, return_geometry=False)
        context_block = _format_arcgis_context_for_report_generation(
            gis_result,
            search_text,
            include_homeharvest=include_homeharvest,
        )
        peer_block = ""
        if include_homeharvest and _request_needs_arcgis_candidate_peers(text):
            peer_result = await _arcgis_public_candidate_peers_result(search_text=search_text, max_results=10)
            peer_block = _format_arcgis_candidate_peers_for_homeharvest(peer_result)
        _log(
            "ArcGIS pre-check for report generation",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
            include_homeharvest=include_homeharvest,
            candidate_peers=bool(peer_block),
        )
        return f"{context_block}{((chr(10) + chr(10) + peer_block) if peer_block else '')}\n\nSTAFF REQUEST:\n{text}"
    except Exception as exc:
        _log("ArcGIS report-generation pre-check failed", error=str(exc))
        extra = ""
        if include_homeharvest:
            extra = (
                "\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support if possible.\n"
                "- Label HomeHarvest data as unofficial and not verified sales or official assessment data.\n"
            )
        return (
            f"{mode}\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "PUBLIC ARCGIS PARCEL CONTEXT:\n"
            f"- ArcGIS pre-check failed before Context Expert submission: {exc}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Continue with Context Expert report/file generation if supported.\n"
            "- Disclose that GIS pre-check failed.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions.\n"
            f"{extra}"
        )


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def root_route(request):
    return JSONResponse(
        {
            "status": "ok",
            "service": "aces-mcp-server",
            "health": "/health",
            "mcp": "/mcp",
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse(
        {
            "status": "healthy",
            "service": "aces-mcp-server",
            "mcp_endpoint": "/mcp",
            "rest_routes": [
                "/start-lookup",
                "/check-pending-task",
                "/agent-call",
                "/smart-task",
                "/arcgis-parcel-lookup",
                "/arcgis-parcel-map",
                "/arcgis-candidate-peers",
                "/context-expert-file/{token}",
            ],
            "assessment_project_id": ASSESSMENT_PROJECT_ID,
            "smart_tasks_project_id": SMART_TASKS_PROJECT_ID,
            "smart_tasks_poll_seconds": SMART_TASKS_POLL_SECONDS,
            "smart_tasks_agent_capability": SMART_TASKS_AGENT_CAPABILITY,
            "smart_tasks_response_source": SMART_TASKS_RESPONSE_SOURCE,
            "homeharvest_action_id": HOMEHARVEST_ACTION_ID,
            "arcgis_public_parcel_layer_url": ARCGIS_PUBLIC_PARCEL_LAYER_URL,
            "enrich_homeharvest_with_arcgis": ENRICH_HOMEHARVEST_WITH_ARCGIS,
            "stateless_http": os.getenv("FASTMCP_STATELESS_HTTP", ""),
            "json_response": os.getenv("FASTMCP_JSON_RESPONSE", ""),
            "task_cache_file": TASK_CACHE_FILE,
            "task_cache_count": len(TASK_CACHE),
            "task_cache_debug_enabled": bool(ACES_ADMIN_TOKEN),
            "default_poll_seconds": DEFAULT_POLL_SECONDS,
            "homeharvest_poll_seconds": HOMEHARVEST_POLL_SECONDS,
            "report_generation_poll_seconds": REPORT_GENERATION_POLL_SECONDS,
            "duplicate_prompt_reuse": True,
            "semantic_duplicate_prompt_reuse": True,
            "cached_final_answers": True,
            "fresh_homeharvest_lookups": FRESH_HOMEHARVEST_LOOKUPS,
            "reuse_completed_homeharvest_answers": REUSE_COMPLETED_HOMEHARVEST_ANSWERS,
            "reuse_running_homeharvest_tasks": REUSE_RUNNING_HOMEHARVEST_TASKS,
            "context_expert_file_links_enabled": CONTEXT_EXPERT_FILE_LINKS_ENABLED,
            "public_base_url": PUBLIC_BASE_URL,
            "file_link_ttl_seconds": FILE_LINK_TTL_SECONDS,
            "file_link_cache_count": len(FILE_LINK_CACHE),
            "tools": [
                "Community_Educator",
                "Assessment_Context_Expert",
                "Clear_Expectations",
                "Compliance_Expert",
                "Smart_Tasks",
                "Check_CustomGPT_Task",
                "ArcGIS_Public_Parcel_Lookup",
                "ArcGIS_Public_Parcel_Map",
                "ArcGIS_Public_Candidate_Peers",
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


@mcp.custom_route("/context-expert-file/{token}", methods=["GET"])
async def context_expert_file_download_route(request):
    """
    Public, unguessable download route for Context Expert generated artifacts.

    The token maps to CustomGPT project/task/message/file metadata stored in the
    local Render cache. Staff see this safe Render link in Copilot/Teams; the
    CustomGPT API token stays server-side.
    """
    token = str(request.path_params.get("token") or "").strip()
    if not token:
        return JSONResponse({"status": "failed", "answer": "Missing download token."}, status_code=400)

    meta = FILE_LINK_CACHE.get(token)
    if not isinstance(meta, dict):
        return JSONResponse({"status": "failed", "answer": "Download link not found or the server restarted."}, status_code=404)

    if time.time() > float(meta.get("expires_at", 0) or 0):
        FILE_LINK_CACHE.pop(token, None)
        _save_file_link_cache(FILE_LINK_CACHE)
        return JSONResponse({"status": "failed", "answer": "Download link expired. Ask A.C.E.S. to regenerate or rerun the Context Expert task."}, status_code=410)

    project_id = str(meta.get("project_id") or "").strip()
    task_id = str(meta.get("task_id") or "").strip()
    message_id = str(meta.get("message_id") or "").strip()
    file_id = str(meta.get("file_id") or "").strip()
    file_name = _safe_download_filename(str(meta.get("file_name") or f"context_expert_file_{file_id}"))

    if not all([project_id, task_id, message_id, file_id]):
        return JSONResponse({"status": "failed", "answer": "Download link metadata is incomplete."}, status_code=500)

    if not CUSTOMGPT_API_TOKEN:
        return JSONResponse({"status": "failed", "answer": "Server is missing CUSTOMGPT_API_TOKEN."}, status_code=500)

    url = (
        f"{CUSTOMGPT_BASE}/projects/{project_id}"
        f"/tasks/{task_id}/messages/{message_id}/files/{file_id}/download"
    )
    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "*/*"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=30, pool=15), follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:
        _log("context expert file download failed", error=str(exc), project_id=project_id, task_id=task_id, file_id=file_id)
        return JSONResponse({"status": "failed", "answer": f"Could not contact CustomGPT for the generated file: {exc}"}, status_code=502)

    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if location:
            return RedirectResponse(location)
        return JSONResponse({"status": "failed", "answer": "CustomGPT returned a redirect without a location."}, status_code=502)

    if response.status_code == 200:
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )

    body_preview = response.text[:1200] if response.text else ""
    _log("context expert file download bad status", http_status=response.status_code, body_preview=body_preview, file_id=file_id)
    return JSONResponse(
        {
            "status": "failed",
            "answer": "CustomGPT could not return the generated file. It may have expired; rerun the Context Expert task and download immediately.",
            "http_status": response.status_code,
            "response_preview": body_preview,
        },
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


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


@mcp.custom_route("/arcgis-parcel-lookup", methods=["GET", "POST"])
async def arcgis_parcel_lookup_route(request):
    """
    Fast read-only REST wrapper for the public BernCo ArcGIS parcel layer.

    POST /arcgis-parcel-lookup
    Headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
      Content-Type: application/json
    Body:
      {"searchText": "2 Lauren Taylor Ct Tijeras NM", "maxResults": 10}

    Also supports GET:
      /arcgis-parcel-lookup?searchText=2%20Lauren%20Taylor%20Ct%20Tijeras%20NM&maxResults=10
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: Dict[str, Any] = {}
    else:
        body = await _request_json_or_empty(request)

    search_text = str(
        body.get("searchText")
        or body.get("search_text")
        or body.get("promptText")
        or body.get("prompt_text")
        or request.query_params.get("searchText")
        or request.query_params.get("search_text")
        or request.query_params.get("promptText")
        or ""
    ).strip()

    max_results_raw = (
        body.get("maxResults")
        or body.get("max_results")
        or request.query_params.get("maxResults")
        or request.query_params.get("max_results")
        or ARCGIS_PUBLIC_PARCEL_MAX_RESULTS
    )
    return_geometry_raw = str(
        body.get("returnGeometry")
        or body.get("return_geometry")
        or request.query_params.get("returnGeometry")
        or request.query_params.get("return_geometry")
        or "false"
    ).strip().lower()
    return_geometry = return_geometry_raw in {"1", "true", "yes", "on"}

    result = await _arcgis_public_parcel_lookup_result(
        search_text=search_text,
        max_results=max_results_raw,
        return_geometry=return_geometry,
    )
    return JSONResponse(result)



@mcp.custom_route("/arcgis-parcel-map", methods=["GET", "POST"])
async def arcgis_parcel_map_route(request):
    """
    Fast read-only REST wrapper that returns BernCo Assessor map links and
    Google Maps routing links for a public parcel/address/UPC.

    POST /arcgis-parcel-map
    Headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
      Content-Type: application/json
    Body:
      {"searchText": "2 Lauren Taylor Ct Tijeras NM", "maxResults": 5}

    Also supports GET:
      /arcgis-parcel-map?searchText=2%20Lauren%20Taylor%20Ct%20Tijeras%20NM&maxResults=5
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: Dict[str, Any] = {}
    else:
        body = await _request_json_or_empty(request)

    search_text = str(
        body.get("searchText")
        or body.get("search_text")
        or body.get("promptText")
        or body.get("prompt_text")
        or request.query_params.get("searchText")
        or request.query_params.get("search_text")
        or request.query_params.get("promptText")
        or ""
    ).strip()

    max_results_raw = (
        body.get("maxResults")
        or body.get("max_results")
        or request.query_params.get("maxResults")
        or request.query_params.get("max_results")
        or 5
    )

    gis_result = await _arcgis_public_parcel_lookup_result(
        search_text=search_text,
        max_results=max_results_raw,
        return_geometry=False,
    )
    answer = _format_arcgis_map_routing_text(gis_result)

    return JSONResponse(
        {
            "status": "completed" if gis_result.get("status") in {"completed", "empty"} else "failed",
            "answer": answer,
            "task_id": "",
            "project_id": ASSESSMENT_PROJECT_ID,
            "source": gis_result.get("source", "Bernalillo County Assessor Parcels public ArcGIS layer"),
            "layer_url": gis_result.get("layer_url", ARCGIS_PUBLIC_PARCEL_LAYER_URL),
            "query_mode": gis_result.get("query_mode", ""),
            "where": gis_result.get("where", ""),
            "count": gis_result.get("count", 0),
            "results": gis_result.get("results", []),
        }
    )


@mcp.custom_route("/arcgis-candidate-peers", methods=["GET", "POST"])
async def arcgis_candidate_peers_route(request):
    """
    REST wrapper for ArcGIS-only candidate parcel peers.

    These are peer-search leads for comps workflows, not final comparable sales.
    The output must be enriched with HomeHarvest/CAMA/MLS/deed data before final use.

    POST /arcgis-candidate-peers
    Headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
      Content-Type: application/json
    Body:
      {"searchText": "2 Lauren Taylor Ct Tijeras NM", "maxResults": 10}
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: Dict[str, Any] = {}
    else:
        body = await _request_json_or_empty(request)

    search_text = str(
        body.get("searchText")
        or body.get("search_text")
        or body.get("promptText")
        or body.get("prompt_text")
        or request.query_params.get("searchText")
        or request.query_params.get("search_text")
        or request.query_params.get("promptText")
        or ""
    ).strip()

    max_results_raw = (
        body.get("maxResults")
        or body.get("max_results")
        or request.query_params.get("maxResults")
        or request.query_params.get("max_results")
        or 10
    )

    result = await _arcgis_public_candidate_peers_result(
        search_text=search_text,
        max_results=max_results_raw,
    )
    result["answer"] = _format_arcgis_candidate_peers_text(result)
    result["project_id"] = ASSESSMENT_PROJECT_ID
    return JSONResponse(result)



@mcp.custom_route("/start-lookup", methods=["POST"])
async def start_lookup_route(request):
    """
    REST wrapper for Power Automate/Copilot Studio.

    Plain address/parcel lookups and map/directions requests return a fast ArcGIS-only response.
    Requests for comps, sales, listings, market support, HomeHarvest, public
    aggregator data, reports, exports, PDFs, files, or downloads submit the
    full prompt to Assessment_Context_Expert, with ArcGIS context when available.

    Returns:
      {"status":"completed|still_processing|failed|task_not_found",
       "answer":"...",
       "task_id":"...",
       "project_id":"94006"}
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    body = await _request_json_or_empty(request)
    prompt_text = str(body.get("promptText") or body.get("prompt_text") or "").strip()

    if not prompt_text:
        return JSONResponse(
            {
                "status": "failed",
                "answer": "Missing promptText.",
                "task_id": "",
                "project_id": ASSESSMENT_PROJECT_ID,
            }
        )

    try:
        # Report/file/PDF/export generation must go to Context Expert. Do this
        # before the ArcGIS-only fast path so "generate a report on [address]"
        # does not get reduced to a plain parcel lookup.
        if _request_needs_report_generation(prompt_text):
            wants_homeharvest = _should_enable_homeharvest(prompt_text)
            report_prompt = await _enrich_report_generation_prompt_with_arcgis(
                prompt_text,
                include_homeharvest=wants_homeharvest,
            )
            if wants_homeharvest:
                report_prompt = _enrich_homeharvest_prompt(report_prompt)
            action_id = HOMEHARVEST_ACTION_ID if wants_homeharvest else None
            poll_seconds = HOMEHARVEST_POLL_SECONDS if action_id else REPORT_GENERATION_POLL_SECONDS
            raw_result = await _call_customgpt_task(
                ASSESSMENT_PROJECT_ID,
                report_prompt,
                "Assessment_Context_Expert",
                action_id=action_id,
                poll_seconds=poll_seconds,
            )
            normalized = _normalize_aces_result(raw_result, project_id=ASSESSMENT_PROJECT_ID)
            return JSONResponse(normalized)

        # Fast path: map/location/directions requests should return direct
        # BernCo Assessor map and Google Maps links without spawning CustomGPT.
        if _should_use_arcgis_map_lookup(prompt_text):
            search_text = _extract_arcgis_search_text_from_prompt(prompt_text) or prompt_text
            gis_result = await _arcgis_public_parcel_lookup_result(
                search_text=search_text,
                max_results=5,
                return_geometry=False,
            )
            answer = _format_arcgis_map_routing_text(gis_result)
            status = "completed" if gis_result.get("status") in {"completed", "empty"} else "failed"
            return JSONResponse(
                {
                    "status": status,
                    "answer": answer,
                    "task_id": "",
                    "project_id": ASSESSMENT_PROJECT_ID,
                }
            )

        # Fast path: plain address/parcel lookup should not spawn a long
        # HomeHarvest/CustomGPT task or return comps unless staff asked for comps,
        # sales, listings, market support, HomeHarvest, public aggregator data,
        # or report/file generation.
        if _should_use_arcgis_only_lookup(prompt_text):
            search_text = _extract_arcgis_search_text_from_prompt(prompt_text) or prompt_text
            gis_result = await _arcgis_public_parcel_lookup_result(
                search_text=search_text,
                max_results=ARCGIS_PUBLIC_PARCEL_MAX_RESULTS,
                return_geometry=False,
            )
            answer = _format_arcgis_parcel_lookup_text(gis_result)
            status = "completed" if gis_result.get("status") in {"completed", "empty"} else "failed"
            return JSONResponse(
                {
                    "status": status,
                    "answer": answer,
                    "task_id": "",
                    "project_id": ASSESSMENT_PROJECT_ID,
                }
            )

        action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(prompt_text) else None
        poll_seconds = HOMEHARVEST_POLL_SECONDS if action_id else DEFAULT_POLL_SECONDS
        raw_result = await _call_customgpt_task(
            ASSESSMENT_PROJECT_ID,
            prompt_text,
            "Assessment_Context_Expert",
            action_id=action_id,
            poll_seconds=poll_seconds,
        )
        normalized = _normalize_aces_result(raw_result, project_id=ASSESSMENT_PROJECT_ID)
        return JSONResponse(normalized)
    except Exception as exc:
        _log("start_lookup_route exception", error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"The lookup did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": ASSESSMENT_PROJECT_ID,
            }
        )


@mcp.custom_route("/check-pending-task", methods=["GET", "POST"])
async def check_pending_task_route(request):
    """
    Normalized REST wrapper for polling an existing CustomGPT task.

    POST /check-pending-task
    Body:
      {"task_id":"...", "project_id":"94006"}

    Also supports GET:
      /check-pending-task?project_id=94006&task_id=...
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: Dict[str, Any] = {}
    else:
        body = await _request_json_or_empty(request)

    project_id = str(
        body.get("project_id")
        or body.get("projectId")
        or request.query_params.get("project_id")
        or request.query_params.get("projectId")
        or ASSESSMENT_PROJECT_ID
    ).strip()
    task_id = str(
        body.get("task_id")
        or body.get("taskId")
        or request.query_params.get("task_id")
        or request.query_params.get("taskId")
        or ""
    ).strip()

    if not task_id:
        return JSONResponse(
            {
                "status": "failed",
                "answer": "Missing task_id.",
                "task_id": "",
                "project_id": project_id,
            }
        )

    try:
        raw_result = await _check_customgpt_task_result(project_id=project_id, task_id=task_id)
        normalized = _normalize_aces_result(raw_result, project_id=project_id, task_id=task_id)
        return JSONResponse(normalized)
    except Exception as exc:
        _log("check_pending_task_route exception", project_id=project_id, task_id=task_id, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"The pending lookup did not return usable results. Error: {exc}",
                "task_id": task_id,
                "project_id": project_id,
            }
        )


@mcp.custom_route("/smart-task", methods=["POST"])
async def smart_task_route(request):
    """
    Normalized REST wrapper for CustomGPT project 9262 Smart Tasks.

    POST /smart-task
    Headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
      Content-Type: application/json
    Body:
      {"promptText": "Analyze this data and create a downloadable report."}
    Optional:
      {"project_id": "9262"}

    Returns:
      {"status":"completed|still_processing|failed|task_not_found",
       "answer":"...",
       "task_id":"...",
       "project_id":"9262"}
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    body = await _request_json_or_empty(request)
    prompt_text = str(body.get("promptText") or body.get("prompt_text") or "").strip()
    project_id = str(
        body.get("project_id")
        or body.get("projectId")
        or SMART_TASKS_PROJECT_ID
    ).strip()

    if not prompt_text:
        return JSONResponse(
            {
                "status": "failed",
                "answer": "Missing promptText.",
                "task_id": "",
                "project_id": project_id,
            }
        )

    try:
        raw_result = await _call_customgpt_task(
            project_id,
            prompt_text,
            "Smart_Tasks",
            action_id=None,
            poll_seconds=SMART_TASKS_POLL_SECONDS,
            agent_capability=SMART_TASKS_AGENT_CAPABILITY,
            response_source=SMART_TASKS_RESPONSE_SOURCE,
        )
        return JSONResponse(_normalize_aces_result(raw_result, project_id=project_id))
    except Exception as exc:
        _log("smart_task_route exception", project_id=project_id, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"Smart_Tasks did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": project_id,
            }
        )


@mcp.custom_route("/agent-call", methods=["POST"])
async def agent_call_route(request):
    """
    Optional normalized REST wrapper for A.C.E.S. specialist agents.

    Body:
      {"agent":"Community_Educator|Clear_Expectations|Compliance_Expert|Assessment_Context_Expert|Smart_Tasks",
       "promptText":"..."}

    Assessment_Context_Expert, Compliance_Expert, and Smart_Tasks use Plan & Act task mode.
    For Assessment_Context_Expert address/comps work, prefer /start-lookup.
    For project 9262 Smart Tasks work, prefer /smart-task.
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    body = await _request_json_or_empty(request)
    agent = str(body.get("agent") or body.get("tool") or "").strip()
    prompt_text = str(body.get("promptText") or body.get("prompt_text") or "").strip()

    if not prompt_text:
        return JSONResponse({"status": "failed", "answer": "Missing promptText.", "task_id": "", "project_id": ""})

    if agent in {"ArcGIS_Public_Parcel_Lookup", "ArcGIS_Public_Parcel", "ArcGIS", "ArcGIS_Parcel_Lookup"}:
        result = await _arcgis_public_parcel_lookup_result(prompt_text)
        return JSONResponse(result)

    if agent in {"ArcGIS_Public_Parcel_Map", "ArcGIS_Parcel_Map", "ArcGIS_Map", "Parcel_Map"}:
        result = await _arcgis_public_parcel_lookup_result(prompt_text, max_results=5, return_geometry=False)
        answer = _format_arcgis_map_routing_text(result)
        return JSONResponse(
            {
                "status": "completed" if result.get("status") in {"completed", "empty"} else "failed",
                "answer": answer,
                "task_id": "",
                "project_id": ASSESSMENT_PROJECT_ID,
                "source": result.get("source", "Bernalillo County Assessor Parcels public ArcGIS layer"),
                "results": result.get("results", []),
            }
        )

    agent_map = {
        "Community_Educator": (COMMUNITY_PROJECT_ID, "Community_Educator", None, DEFAULT_POLL_SECONDS),
        "Clear_Expectations": (CLEAR_PROJECT_ID, "Clear_Expectations", None, DEFAULT_POLL_SECONDS),
        "Compliance_Expert": (COMPLIANCE_PROJECT_ID, "Compliance_Expert", None, DEFAULT_POLL_SECONDS),
        "Smart_Tasks": (SMART_TASKS_PROJECT_ID, "Smart_Tasks", None, SMART_TASKS_POLL_SECONDS),
        "Assessment_Context_Expert": (
            ASSESSMENT_PROJECT_ID,
            "Assessment_Context_Expert",
            HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(prompt_text) else None,
            HOMEHARVEST_POLL_SECONDS if _should_enable_homeharvest(prompt_text) else DEFAULT_POLL_SECONDS,
        ),
    }

    if agent not in agent_map:
        return JSONResponse(
            {
                "status": "failed",
                "answer": "Invalid agent. Use Community_Educator, Assessment_Context_Expert, Clear_Expectations, Compliance_Expert, Smart_Tasks, ArcGIS_Public_Parcel_Lookup, ArcGIS_Public_Parcel_Map, or ArcGIS_Public_Candidate_Peers.",
                "task_id": "",
                "project_id": "",
            }
        )

    project_id, tool_name, action_id, poll_seconds = agent_map[agent]
    try:
        plan_act_tools = {"Assessment_Context_Expert", "Compliance_Expert", "Smart_Tasks"}

        if tool_name in plan_act_tools:
            raw_result = await _call_customgpt_task(
                project_id,
                prompt_text,
                tool_name,
                action_id=action_id,
                poll_seconds=poll_seconds,
                agent_capability=SMART_TASKS_AGENT_CAPABILITY if tool_name == "Smart_Tasks" else None,
                response_source=SMART_TASKS_RESPONSE_SOURCE if tool_name == "Smart_Tasks" else "openai_content",
            )
        else:
            raw_result = await _call_customgpt_conversation(project_id, prompt_text, tool_name)

        return JSONResponse(_normalize_aces_result(raw_result, project_id=project_id))
    except Exception as exc:
        _log("agent_call_route exception", agent=agent, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"{agent} did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": str(project_id or ""),
            }
        )




def _require_config(project_id: str, tool_name: str) -> Optional[str]:
    if not CUSTOMGPT_API_TOKEN:
        return "Missing CUSTOMGPT_API_TOKEN on Render."
    if not project_id:
        return f"Missing project id for {tool_name}. Set it in Render Environment."
    return None


def _request_needs_homeharvest(prompt_text: str) -> bool:
    """
    True when the user asks for HomeHarvest/public aggregator market support:
    comps, sales, listings, sold properties, market data, or similar external
    public-aggregator context. Plain address and map/directions lookups stay ArcGIS-only.
    """
    text = (prompt_text or "").lower()

    explicit_mode_or_source = [
        "mode: address / homeharvest lookup",
        "mode: record + homeharvest comp support",
        "mode: address report + homeharvest market support",
        "homeharvest",
        "home harvest",
        "public aggregator",
        "public-aggregator",
        "aggregator data",
        "external market data",
    ]
    if _has_any_word_or_phrase(text, explicit_mode_or_source):
        return True

    market_words = [
        "comp",
        "comps",
        "comparable",
        "comparables",
        "similar properties",
        "nearby sales",
        "nearby sale",
        "recent sales",
        "sales nearby",
        "sold properties",
        "sold property",
        "sold homes",
        "sale price",
        "sales price",
        "sale date",
        "listing data",
        "listings",
        "active listing",
        "pending listing",
        "for sale",
        "market support",
        "market data",
        "market activity",
        "market value support",
        "candidate comps",
    ]

    return _has_any_word_or_phrase(text, market_words)


def _request_needs_report_generation(prompt_text: str) -> bool:
    """
    True when staff asks for a generated report/file/PDF/export/download. These
    requests must go to Context Expert, not the ArcGIS-only fast path.
    """
    text = (prompt_text or "").lower()

    explicit_modes = [
        "mode: address report generation",
        "mode: address report + homeharvest market support",
        "mode: report generation",
        "report generation",
        "file generation",
    ]
    if any(mode in text for mode in explicit_modes):
        return True

    report_or_file_phrases = [
        "generate a report",
        "generate report",
        "create a report",
        "create report",
        "make a report",
        "make report",
        "prepare a report",
        "write a report",
        "report on",
        "property report",
        "parcel report",
        "owner report",
        "staff report",
        "generate a file",
        "generate file",
        "create a file",
        "create file",
        "downloadable file",
        "generated file",
        "download link",
        "download links",
        "download the report",
        "download report",
        "export report",
        "export a report",
        "export to pdf",
        "create pdf",
        "generate pdf",
        "pdf report",
        "attachment",
        "artifact",
    ]

    return any(phrase in text for phrase in report_or_file_phrases)


def _request_needs_map_routing(prompt_text: str) -> bool:
    """True when staff asks to map, locate, route to, or get directions to a parcel/address."""
    text = (prompt_text or "").lower()
    map_terms = [
        "map",
        "maps",
        "google maps",
        "google map",
        "directions",
        "direction",
        "route",
        "routing",
        "navigate",
        "navigation",
        "locate",
        "location",
        "open on map",
        "show on map",
        "show me on map",
        "assessor map",
        "bernco map",
    ]
    return _has_any_word_or_phrase(text, map_terms)


def _should_use_arcgis_map_lookup(prompt_text: str) -> bool:
    """Use map fast path only for map/location/direction requests, not comps/reports."""
    return (
        _looks_like_arcgis_lookup(prompt_text)
        and _request_needs_map_routing(prompt_text)
        and not _request_needs_homeharvest(prompt_text)
        and not _request_needs_report_generation(prompt_text)
    )


def _looks_like_arcgis_lookup(prompt_text: str) -> bool:
    """Detect a plain address/UPC/property lookup that can be answered by ArcGIS.

    Important: do not require a perfect command phrase like "look up".
    Copilot/user text can contain typos such as "ook up", or a user may paste
    only an address/UPC. If a street address, UPC, PIN, or situs-like value can
    be extracted and the request does not explicitly ask for comps/sales/listings
    HomeHarvest, the lookup should stay on the fast ArcGIS-only path.
    """
    text = (prompt_text or "").lower()
    search_text = _extract_arcgis_search_text_from_prompt(prompt_text)
    if not search_text:
        return False

    compact = _compact_digits(search_text)
    if re.fullmatch(r"[0-9]{12,30}", compact):
        return True

    # Any extractable street-address/situs pattern should be treated as a
    # parcel lookup unless the separate HomeHarvest detector sees comp/sale
    # language. This handles ZIP+4 and minor typos in the command phrase.
    if _normalize_arcgis_situs_from_user_text(search_text):
        return True

    lookup_words = [
        "look up", "lookup", "ook up", "search", "find", "parcel",
        "property", "situs", "address", "owner", "upc", "pin"
    ]
    return any(word in text for word in lookup_words)


def _should_use_arcgis_only_lookup(prompt_text: str) -> bool:
    """Use ArcGIS-only fast path for plain parcel/address lookup."""
    return (
        _looks_like_arcgis_lookup(prompt_text)
        and not _request_needs_homeharvest(prompt_text)
        and not _request_needs_report_generation(prompt_text)
    )


def _should_enable_homeharvest(prompt_text: str) -> bool:
    """
    Enable HomeHarvest only for explicit HomeHarvest/comps/sales/listing/market
    work. Plain address lookup is handled by the ArcGIS-only fast path.
    """
    return _request_needs_homeharvest(prompt_text)

def _enrich_homeharvest_prompt(prompt_text: str) -> str:
    """Add action-use guidance without overriding an existing MODE block."""
    text = prompt_text or ""
    if "homeharvest action rule" in text.lower():
        return text

    date_from, date_to = _past_years_date_range(10)

    action_rule = (
    "\n\nHOMEHARVEST ACTION RULE:\n"
    "- Use the enabled HomeHarvest custom action when the request is an address, nearby sale, or comp lookup.\n"
    "- Do not answer from memory, previous runs, cached examples, or stale conversation context. Run the action for this request.\n"
    "- Prefer operation homeharvestSearchProperties.\n"
    "- Use POST /properties/search.\n"
    "- For address/comps work, search the subject address itself first, then search sold/listing candidates around the subject.\n"
    "- Return staff-readable numbered cards, not raw JSON.\n"
    "- For report-generation requests, include the HomeHarvest/Public Aggregator Market Support section in the report text and any generated file.\n"
    "- A no-result response is not a tool failure.\n"
    "\n"
    "GIS + HOMEHARVEST RULES:\n"
    "- If a PUBLIC ARCGIS PARCEL CONTEXT block is present, use it as the subject identity/geography/assessment anchor.\n"
    "- Also run/use HomeHarvest for the subject address when available; keep HomeHarvest subject fields separate and label them unofficial.\n"
    "- If a PUBLIC ARCGIS CANDIDATE PARCEL PEERS block is present, use those addresses as peer leads for HomeHarvest sold-property enrichment.\n"
    "- Return the GIS subject context first, HomeHarvest subject facts second if found, then HomeHarvest/public-aggregator candidate results.\n"
    "- Do not treat GIS data as a verified sale, tax status, exemption approval/status, or certified record.\n"
    "- Verify final parcel/account details in iasWorld before relying on them.\n"
    "\n"
    "COMP SEARCH QUALITY RULES:\n"
    "- For comp requests, use listing_type=sold or sold status when supported.\n"
    f"- For 'past 10 years', use date_from={date_from} and date_to={date_to} unless the user gives a different date range.\n"
    "- Exclude land, lots, mobile homes, manufactured homes, rentals, active listings, pending listings, and rows with missing price, missing date, missing sqft, or missing residential characteristics.\n"
    "- Do not return exactly 10 unless 10 usable residential candidates are found.\n"
    "- If fewer than 10 usable residential candidates are found, return only the usable candidates and clearly say how many were found.\n"
    "- Do not pad the list with land, missing-data rows, atypical low-price rows, or poor matches.\n"
    "- Prefer similar residential properties by property type, living area, beds, baths, lot size, year built, and proximity; when ArcGIS lacks living area, use ArcGIS only for peer triage and require HomeHarvest/CAMA/MLS enrichment.\n"
    "- Sort by comp similarity first, not newest first.\n"
    "- Label results as unofficial public-aggregator candidate comps, not verified sales.\n"
    "- If the source only returns list/public aggregator prices, say they are not verified sold prices.\n"
    "- Do not mention an interactive map, file manager, generated file, download, attachment, report, or exported view unless the current tool response includes an actual generated_files item or downloadable link.\n"
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
    if "mode: address report generation" in text or "mode: report generation" in text or _request_needs_report_generation(prompt_text):
        return "Report/file generation task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
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


def _safe_download_filename(value: str) -> str:
    """Return a safe filename for Content-Disposition."""
    name = str(value or "context_expert_file").strip()
    name = name.replace("\\", "_").replace("/", "_").replace('"', "'")
    name = re.sub(r"[\r\n\x00-\x1f]+", "_", name).strip(" .")
    return name[:180] or "context_expert_file"


def _markdown_link_label(value: str) -> str:
    label = str(value or "generated file").strip()
    label = label.replace("[", "\\[").replace("]", "\\]")
    return label or "generated file"


def _extract_file_list_from_payload(payload: Any) -> List[Dict[str, Any]]:
    """Find file objects in the common CustomGPT files response envelopes."""
    found: List[Dict[str, Any]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # File objects usually have an id plus a name/type/content_type.
                    has_file_shape = bool(
                        item.get("id") or item.get("file_id") or item.get("uuid") or item.get("artifact_id")
                    ) and bool(
                        item.get("name") or item.get("file_name") or item.get("filename") or item.get("title") or item.get("type")
                    )
                    if has_file_shape:
                        found.append(item)
                    else:
                        visit(item, depth + 1)
            return
        if isinstance(value, dict):
            for key in ("files", "items", "results", "artifacts", "data"):
                if key in value:
                    visit(value.get(key), depth + 1)

    visit(payload)

    # De-dupe by best available id.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in found:
        file_id = _extract_customgpt_file_id(item)
        key = file_id or json.dumps(item, sort_keys=True, default=str)[:200]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _extract_customgpt_file_id(file_obj: Dict[str, Any]) -> str:
    for key in ("id", "file_id", "fileId", "uuid", "artifact_id", "artifactId"):
        value = file_obj.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _extract_customgpt_file_name(file_obj: Dict[str, Any]) -> str:
    for key in ("name", "file_name", "fileName", "filename", "original_name", "title"):
        value = file_obj.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    file_id = _extract_customgpt_file_id(file_obj)
    return f"context_expert_artifact_{file_id}" if file_id else "context_expert_artifact"


def _is_generated_customgpt_artifact(file_obj: Dict[str, Any]) -> bool:
    text_fields = " ".join(
        str(file_obj.get(key) or "").lower()
        for key in ("type", "kind", "category", "source", "storage_type", "origin")
    )
    if any(word in text_fields for word in ("artifact", "generated", "output")):
        return True
    if file_obj.get("is_artifact") is True or file_obj.get("generated") is True or file_obj.get("is_generated") is True:
        return True
    if file_obj.get("artifact_id") or file_obj.get("artifactId"):
        return True
    return False


async def _create_context_expert_artifact_links(
    client: httpx.AsyncClient,
    project_id: str,
    task_id: str,
    message_id: str,
    headers: Dict[str, str],
) -> List[str]:
    """List CustomGPT generated artifacts and create staff-safe Render download links."""
    if not CONTEXT_EXPERT_FILE_LINKS_ENABLED:
        return []
    if not PUBLIC_BASE_URL:
        _log("context expert file links skipped - missing PUBLIC_BASE_URL")
        return []
    if not message_id:
        return []

    url = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}/messages/{message_id}/files"
    try:
        response = await client.get(url, headers=headers)
        payload = await _read_json_or_text(response)
    except Exception as exc:
        _log("context expert files list failed", project_id=project_id, task_id=task_id, message_id=message_id, error=str(exc))
        return []

    if response.status_code >= 400:
        _log(
            "context expert files list bad status",
            project_id=project_id,
            task_id=task_id,
            message_id=message_id,
            http_status=response.status_code,
            response_preview=_safe_json_dumps(payload, 1200),
        )
        return []

    file_objects = _extract_file_list_from_payload(payload)
    links: List[str] = []
    now = time.time()

    for file_obj in file_objects:
        if not _is_generated_customgpt_artifact(file_obj):
            continue

        file_id = _extract_customgpt_file_id(file_obj)
        if not file_id:
            continue

        file_name = _safe_download_filename(_extract_customgpt_file_name(file_obj))
        token = secrets.token_urlsafe(32)
        FILE_LINK_CACHE[token] = {
            "project_id": str(project_id),
            "task_id": str(task_id),
            "message_id": str(message_id),
            "file_id": str(file_id),
            "file_name": file_name,
            "created_at": _now_iso(),
            "expires_at": now + max(300, FILE_LINK_TTL_SECONDS),
            "customgpt_file_type": file_obj.get("type") or file_obj.get("kind") or "artifact",
        }

        links.append(f"- [{_markdown_link_label(file_name)}]({PUBLIC_BASE_URL}/context-expert-file/{token})")

    if links:
        _save_file_link_cache(FILE_LINK_CACHE)
        _log("context expert artifact links created", project_id=project_id, task_id=task_id, message_id=message_id, count=len(links))

    return links


async def _append_context_expert_artifact_links(
    client: httpx.AsyncClient,
    answer: str,
    project_id: str,
    task_id: str,
    message_id: Optional[str],
    headers: Dict[str, str],
) -> str:
    """Append markdown download links for generated artifacts, when present."""
    text = str(answer or "").strip()
    if not text or not message_id:
        return text
    if "context-expert-file/" in text or "Generated files:" in text:
        return text

    links = await _create_context_expert_artifact_links(
        client=client,
        project_id=project_id,
        task_id=task_id,
        message_id=str(message_id),
        headers=headers,
    )
    if not links:
        return text

    return text + "\n\nGenerated files:\n" + "\n".join(links)


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
            inline_answer = await _append_context_expert_artifact_links(client, inline_answer, project_id, task_id, str(message_id) if message_id else None, headers)
            _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status=latest_status, message_id=str(message_id) if message_id else None, progress_log=progress_log, answer=inline_answer)
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
                history_answer = await _append_context_expert_artifact_links(client, history_answer, project_id, task_id, str(message_id), headers)
                _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status="history_fallback", message_id=str(message_id), progress_log=progress_log, answer=history_answer)
                return history_answer

            _update_task_cache_by_task_id(project_id, task_id, "final_fetch_or_empty_failed", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, error=answer)
            return answer

        answer = await _append_context_expert_artifact_links(client, answer, project_id, task_id, str(message_id), headers)
        _update_task_cache_by_task_id(project_id, task_id, "answered", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, answer=answer)
        return answer



def _extract_session_id_from_conversation_response(data: Any) -> str:
    """Extract a CustomGPT conversation/session id from common response envelopes."""
    candidates: List[Any] = []
    if isinstance(data, dict):
        candidates.append(data)
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates.append(nested)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("session_id", "sessionId", "session", "id", "uuid"):
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


async def _call_customgpt_conversation(
    project_id: str,
    prompt_text: str,
    tool_name: str,
    response_source: str = "openai_content",
    agent_capability: Optional[str] = None,
) -> str:
    """
    Use the normal CustomGPT conversation API for non-Plan & Act agents.

    The /tasks endpoint creates Plan & Act tasks and requires use_planner_mode.
    Community_Educator and Clear_Expectations use conversations.
    Assessment_Context_Expert and Compliance_Expert use Plan & Act tasks.
    """
    config_error = _require_config(project_id, tool_name)
    if config_error:
        return config_error

    prompt_text = str(prompt_text or "").strip()
    if not prompt_text:
        return f"{tool_name} received an empty promptText."

    headers = {
        "Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    conversation_name = f"ACES|{tool_name}|{int(time.time())}|{_stable_hash(prompt_text)}"
    timeout = httpx.Timeout(connect=15, read=max(35, DEFAULT_POLL_SECONDS), write=35, pool=15)

    async with httpx.AsyncClient(timeout=timeout) as client:
        create = await client.post(
            f"{CUSTOMGPT_BASE}/projects/{project_id}/conversations",
            headers=headers,
            json={"name": conversation_name[:255]},
        )
        create_data = await _read_json_or_text(create)
        _log(
            "CustomGPT conversation create response",
            tool=tool_name,
            project=project_id,
            http_status=create.status_code,
            response_preview=_safe_json_dumps(create_data, 1200),
        )

        if create.status_code >= 400:
            return (
                f"{tool_name} conversation create failed.\n"
                f"HTTP status: {create.status_code}\n"
                f"Response: {_safe_json_dumps(create_data)}"
            )

        session_id = _extract_session_id_from_conversation_response(create_data)
        if not session_id:
            return (
                f"{tool_name} conversation create did not return a session id.\n"
                f"Response: {_safe_json_dumps(create_data)}"
            )

        payload: Dict[str, Any] = {
            "prompt": prompt_text,
            "response_source": response_source,
        }
        if agent_capability:
            payload["agent_capability"] = agent_capability

        message = await client.post(
            f"{CUSTOMGPT_BASE}/projects/{project_id}/conversations/{session_id}/messages",
            headers=headers,
            json=payload,
        )
        message_data = await _read_json_or_text(message)
        _log(
            "CustomGPT conversation message response",
            tool=tool_name,
            project=project_id,
            session_id=session_id,
            http_status=message.status_code,
            response_preview=_safe_json_dumps(message_data, 1200),
        )

        if message.status_code >= 400:
            return (
                f"{tool_name} conversation message failed.\n"
                f"Project ID: {project_id}\n"
                f"Session ID: {session_id}\n"
                f"HTTP status: {message.status_code}\n"
                f"Response: {_safe_json_dumps(message_data)}"
            )

        answer = _extract_answer_from_message(message_data)
        if not answer:
            return (
                f"{tool_name} conversation response was empty.\n"
                f"Project ID: {project_id}\n"
                f"Session ID: {session_id}\n"
                f"Response: {_safe_json_dumps(message_data)}"
            )

        return answer


def _is_homeharvest_lookup(tool_name: str, prompt_text: str, action_id: Optional[str]) -> bool:
    return tool_name == "Assessment_Context_Expert" and bool(action_id or _should_enable_homeharvest(prompt_text))


def _is_fresh_assessment_task(tool_name: str, prompt_text: str, action_id: Optional[str]) -> bool:
    """Run-specific tasks should not reuse completed cached answers."""
    if tool_name == "Smart_Tasks":
        return True

    return tool_name == "Assessment_Context_Expert" and (
        _is_homeharvest_lookup(tool_name, prompt_text, action_id)
        or _request_needs_report_generation(prompt_text)
    )


def _should_reuse_prompt_cache(tool_name: str, prompt_text: str, action_id: Optional[str]) -> bool:
    """
    Decide whether a direct specialist tool call may reuse a prompt-level cached task.

    Important: this controls only the initial tool call. Task IDs can still be
    checked through Check_CustomGPT_Task, and every submitted task is still saved.
    """
    if tool_name == "Smart_Tasks":
        return False

    if not _is_fresh_assessment_task(tool_name, prompt_text, action_id):
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
    agent_capability: Optional[str] = None,
    response_source: str = "openai_content",
) -> str:
    config_error = _require_config(project_id, tool_name)
    if config_error:
        return config_error

    prompt_text = str(prompt_text or "").strip()
    if not prompt_text:
        return f"{tool_name} received an empty promptText."

    if tool_name == "Assessment_Context_Expert" and _request_needs_report_generation(prompt_text):
        # Report/file/PDF/export generation should always get ArcGIS context when
        # an address or UPC is present. If the staff request also asks for
        # HomeHarvest/comps/sales/listings/market data, keep HomeHarvest enabled
        # and add report-specific market-support instructions.
        include_homeharvest = bool(action_id) or _should_enable_homeharvest(prompt_text)
        prompt_text = await _enrich_report_generation_prompt_with_arcgis(
            prompt_text,
            include_homeharvest=include_homeharvest,
        )
        if include_homeharvest and action_id:
            prompt_text = _enrich_homeharvest_prompt(prompt_text)
    elif tool_name == "Assessment_Context_Expert" and action_id:
        # ArcGIS runs first as a fast public parcel pre-check, then the combined
        # prompt is enriched with HomeHarvest instructions and submitted to CustomGPT.
        prompt_text = await _enrich_homeharvest_prompt_with_arcgis(prompt_text)
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

    if existing_task and _is_fresh_assessment_task(tool_name, prompt_text, action_id) and _should_reuse_existing_homeharvest_task(existing_task):
        existing_task_id = str(existing_task.get("task_id") or "").strip()
        if existing_task_id:
            _log("REUSING IN-FLIGHT ASSESSMENT TASK", tool=tool_name, project=project_id, task_id=existing_task_id)
            return await _check_customgpt_task_result(project_id=project_id, task_id=existing_task_id)

    if existing_task and not should_reuse_prompt_cache:
        _log("BYPASSING PROMPT CACHE - fresh HomeHarvest/Assessment lookup", tool=tool_name, project=project_id, previous_task_id=existing_task.get("task_id"))

    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "application/json"}

    if not should_reuse_prompt_cache:
        task_name = f"ACES|{tool_name}|fresh|{int(time.time())}|{_stable_hash(prompt_text)}"
    else:
        task_name = f"ACES|{tool_name}|{_stable_hash(prompt_text)}"

    _log("SUBMITTING NEW CUSTOMGPT TASK", tool=tool_name, project=project_id, task_name=task_name)

    capability = (agent_capability or "optimal-choice").strip()
    selected_response_source = (response_source or "openai_content").strip()

    multipart: Dict[str, Any] = {
        "name": (None, task_name),
        "prompt": (None, prompt_text),
        "response_source": (None, selected_response_source),
        "agent_capability": (None, capability),
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
            inline_answer = await _append_context_expert_artifact_links(client, inline_answer, project_id, task_id, str(message_id) if message_id else None, headers)
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
                history_answer = await _append_context_expert_artifact_links(client, history_answer, project_id, task_id, str(message_id), headers)
                _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status="history_fallback", message_id=str(message_id), progress_log=progress_log, answer=history_answer)
                return history_answer

            _remember_task(project_id, tool_name, prompt_text, task_id, "final_fetch_or_empty_failed", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, error=answer)
            return f"{tool_name} final retrieval failed.\nTask ID: {task_id}\nProject ID: {project_id}\nCache key: {cache_key}\n{answer}"

        answer = await _append_context_expert_artifact_links(client, answer, project_id, task_id, str(message_id), headers)
        _remember_task(project_id, tool_name, prompt_text, task_id, "answered", latest_status=latest_status, message_id=str(message_id), progress_log=progress_log, answer=answer)
        return answer


@mcp.tool
async def Community_Educator(promptText: str) -> str:
    """
    Use for public/taxpayer-facing process questions, exemptions, Notices of Value,
    protests, forms, deadlines, outreach, value freeze, and owner-facing explanations.
    Takes exactly one parameter: promptText.
    """
    return await _call_customgpt_conversation(COMMUNITY_PROJECT_ID, promptText, "Community_Educator")


@mcp.tool
async def Assessment_Context_Expert(promptText: str) -> str:
    """
    Use for address, situs, parcel/account, owner/property lookup, PRC, OD report,
    iasWorld export, property record card, ArcGIS public parcel pre-check, HomeHarvest, comps, sales, values,
    exemptions on a record, record interpretation, and assessment context.
    Takes exactly one parameter: promptText.
    """
    action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(promptText) else None
    needs_report = _request_needs_report_generation(promptText)
    poll_seconds = HOMEHARVEST_POLL_SECONDS if action_id else (REPORT_GENERATION_POLL_SECONDS if needs_report else DEFAULT_POLL_SECONDS)
    _log("Assessment_Context_Expert invoked", homeharvest_enabled=bool(action_id), report_generation=needs_report, action_id=action_id or "", poll_seconds=poll_seconds)
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
    return await _call_customgpt_conversation(CLEAR_PROJECT_ID, promptText, "Clear_Expectations")


@mcp.tool
async def Compliance_Expert(promptText: str) -> str:
    """
    Use for legal/statutory questions, NMSA, regulations, case law, AG opinions,
    statutory interpretation, protest standards, exemption basis, valuation authority,
    and legal risk. Takes exactly one parameter: promptText.

    Compliance_Expert uses Plan & Act mode, so it submits a CustomGPT task.
    It may return a Task ID / Project ID when still processing.
    """
    _log("Compliance_Expert invoked", poll_seconds=DEFAULT_POLL_SECONDS)
    return await _call_customgpt_task(
        COMPLIANCE_PROJECT_ID,
        promptText,
        "Compliance_Expert",
        action_id=None,
        poll_seconds=DEFAULT_POLL_SECONDS,
    )


@mcp.tool
async def Smart_Tasks(promptText: str) -> str:
    """
    Use for CustomGPT project 9262 Smart Tasks / Plan & Act work:
    reading files, creating files, code execution, data analysis, dashboards,
    exports, calculations, and multi-step work that requires Smart Tasks.

    Takes exactly one parameter: promptText.
    May return Task ID / Project ID if still processing.
    """
    _log(
        "Smart_Tasks invoked",
        project_id=SMART_TASKS_PROJECT_ID,
        poll_seconds=SMART_TASKS_POLL_SECONDS,
        agent_capability=SMART_TASKS_AGENT_CAPABILITY,
        response_source=SMART_TASKS_RESPONSE_SOURCE,
    )
    return await _call_customgpt_task(
        SMART_TASKS_PROJECT_ID,
        promptText,
        "Smart_Tasks",
        action_id=None,
        poll_seconds=SMART_TASKS_POLL_SECONDS,
        agent_capability=SMART_TASKS_AGENT_CAPABILITY,
        response_source=SMART_TASKS_RESPONSE_SOURCE,
    )


@mcp.tool
async def ArcGIS_Public_Parcel_Lookup(searchText: str, maxResults: int = 10) -> str:
    """
    Use for fast read-only public Bernalillo County ArcGIS parcel lookup by situs
    address, UPC/parcel ID, or owner text. Returns public GIS parcel context only:
    UPC, tax year, owner, owner address, situs address, legal description,
    valuation class, property class, and acreage. Not a certified assessment
    record and not a replacement for iasWorld verification.
    """
    result = await _arcgis_public_parcel_lookup_result(searchText, maxResults)
    return _format_arcgis_parcel_lookup_text(result)



@mcp.tool
async def ArcGIS_Public_Parcel_Map(searchText: str, maxResults: int = 5) -> str:
    """
    Use for map, locate, directions, route, Google Maps, or open-on-map requests
    for a Bernalillo County parcel/address/UPC. Returns a BernCo Assessor map
    link selected by OBJECTID and Google Maps search/directions links based on
    the public GIS situs address. Not a certified record or routing guarantee.
    """
    result = await _arcgis_public_parcel_lookup_result(searchText, maxResults, return_geometry=False)
    return _format_arcgis_map_routing_text(result)


@mcp.tool
async def ArcGIS_Public_Candidate_Peers(searchText: str, maxResults: int = 10) -> str:
    """
    Use for ArcGIS-only candidate parcel peers when staff asks for comp triage
    but the public GIS layer does not have living square footage or verified
    sale fields. Returns peer-search leads by public GIS similarity and distance;
    not final comparable sales and not a replacement for HomeHarvest/CAMA/MLS verification.
    """
    result = await _arcgis_public_candidate_peers_result(searchText, maxResults)
    return _format_arcgis_candidate_peers_text(result)


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
