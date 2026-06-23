import os
import json
import re
import asyncio
import hashlib
import time
import secrets
from typing import Optional, Any, Dict, List, Tuple

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
        "Exposes six tools: Community_Educator, Assessment_Context_Expert, "
        "Clear_Expectations, Compliance_Expert, Check_CustomGPT_Task, "
        "and ArcGIS_Public_Parcel_Lookup. "
        "The four CustomGPT specialist tools take exactly one promptText string and return plain text. "
        "Check_CustomGPT_Task takes projectId and taskId to retrieve a delayed task result. "
        "ArcGIS_Public_Parcel_Lookup performs a read-only public parcel lookup. Assessment_Context_Expert can pre-enrich HomeHarvest/address/comps requests with ArcGIS parcel context."
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

# Public Bernalillo County Assessor parcel layer.
# Default is the richer BernCo public MapServer layer. You can override this
# with ARCGIS_PUBLIC_PARCEL_LAYER_URL in Render if GIS publishes a new URL.
ARCGIS_PUBLIC_PARCEL_LAYER_URL = os.getenv(
    "ARCGIS_PUBLIC_PARCEL_LAYER_URL",
    "https://assessormap.bernco.gov/server/rest/services/GIS/Assessor_Parcels_Public/MapServer/0",
).rstrip("/")
ARCGIS_PUBLIC_PARCEL_MAX_RESULTS = int(os.getenv("ARCGIS_PUBLIC_PARCEL_MAX_RESULTS", "10"))

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
        wants_report = _request_needs_report_generation(raw)
        wants_lookup = any(word in norm for word in ["look up", "lookup", "property", "address", "homeharvest"])

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
        "not found",
        "404",
        "consumed",
        "expired",
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
                response = await client.post(f"{ARCGIS_PUBLIC_PARCEL_LAYER_URL}/query", data=params)
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
            "- Use the GIS parcel as the subject anchor when a single match is present.",
            "- For HomeHarvest comps, search around the GIS situs address and prefer residential results consistent with property class, valuation class, land use, year built, style, acreage, tax district, and location when available.",
            "- Never use GIS assessment values, taxable values, exemption amount fields, AVMs, Zestimates, estimates, or list prices as sale prices, comp prices, exemption approvals, or tax/legal determinations.",
            "- Return the GIS subject context first, then the unofficial HomeHarvest/public-aggregator results.",
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
        _log(
            "ArcGIS pre-check for HomeHarvest",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
        )
        return text + "\n\n" + context_block
    except Exception as exc:
        _log("ArcGIS pre-check failed", error=str(exc))
        return (
            text
            + "\n\nPUBLIC ARCGIS PARCEL CONTEXT:\n"
            + f"- ArcGIS pre-check failed before HomeHarvest submission: {exc}\n"
            + "- Continue with HomeHarvest if the request requires public aggregator/comps data and disclose that GIS pre-check failed."
        )


def _format_arcgis_context_for_report_generation(result: Dict[str, Any], search_text: str) -> str:
    """Create ArcGIS subject context plus report-generation instructions for Context Expert."""
    status = str(result.get("status") or "")
    lines = [
        "MODE: ADDRESS REPORT GENERATION",
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
            "- If the Context Expert can create a downloadable artifact/file for this request, generate it.",
            "- Do not invent missing official fields, values, sales, comps, ownership conclusions, exemption approvals, tax status, or legal/appraisal conclusions.",
            "- Label ArcGIS as public GIS context only and require iasWorld verification before final use.",
            "- If no downloadable artifact is created, return the text report and do not claim that a file was generated.",
        ]
    )
    return "\n".join(lines).strip()


async def _enrich_report_generation_prompt_with_arcgis(prompt_text: str) -> str:
    """Prepend public ArcGIS parcel context for address/parcel report-generation tasks."""
    text = str(prompt_text or "").strip()
    if "REPORT GENERATION INSTRUCTIONS:" in text and "PUBLIC ARCGIS PARCEL CONTEXT:" in text:
        return text

    search_text = _extract_arcgis_search_text_from_prompt(text)
    if not search_text:
        return (
            "MODE: REPORT GENERATION\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Create the requested staff-readable report/file if supported by Context Expert.\n"
            "- No ArcGIS pre-check was run because no address or UPC could be extracted.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions."
        )

    try:
        gis_result = await _arcgis_public_parcel_lookup_result(search_text=search_text, max_results=5, return_geometry=False)
        context_block = _format_arcgis_context_for_report_generation(gis_result, search_text)
        _log(
            "ArcGIS pre-check for report generation",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
        )
        return f"{context_block}\n\nSTAFF REQUEST:\n{text}"
    except Exception as exc:
        _log("ArcGIS report-generation pre-check failed", error=str(exc))
        return (
            "MODE: REPORT GENERATION\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "PUBLIC ARCGIS PARCEL CONTEXT:\n"
            f"- ArcGIS pre-check failed before Context Expert submission: {exc}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Continue with Context Expert report/file generation if supported.\n"
            "- Disclose that GIS pre-check failed.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions."
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
            "rest_routes": ["/start-lookup", "/check-pending-task", "/agent-call", "/arcgis-parcel-lookup", "/context-expert-file/{token}"],
            "assessment_project_id": ASSESSMENT_PROJECT_ID,
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
                "Check_CustomGPT_Task",
                "ArcGIS_Public_Parcel_Lookup",
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



@mcp.custom_route("/start-lookup", methods=["POST"])
async def start_lookup_route(request):
    """
    REST wrapper for Power Automate/Copilot Studio.

    Plain address/parcel lookups return a fast ArcGIS-only response.
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
            report_prompt = await _enrich_report_generation_prompt_with_arcgis(prompt_text)
            action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(prompt_text) else None
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


@mcp.custom_route("/agent-call", methods=["POST"])
async def agent_call_route(request):
    """
    Optional normalized REST wrapper for the non-lookup A.C.E.S. specialist agents.

    Body:
      {"agent":"Community_Educator|Clear_Expectations|Compliance_Expert|Assessment_Context_Expert",
       "promptText":"..."}

    For Assessment_Context_Expert address/comps work, prefer /start-lookup.
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

    agent_map = {
        "Community_Educator": (COMMUNITY_PROJECT_ID, "Community_Educator", None, DEFAULT_POLL_SECONDS),
        "Clear_Expectations": (CLEAR_PROJECT_ID, "Clear_Expectations", None, DEFAULT_POLL_SECONDS),
        "Compliance_Expert": (COMPLIANCE_PROJECT_ID, "Compliance_Expert", None, DEFAULT_POLL_SECONDS),
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
                "answer": "Invalid agent. Use Community_Educator, Assessment_Context_Expert, Clear_Expectations, Compliance_Expert, or ArcGIS_Public_Parcel_Lookup.",
                "task_id": "",
                "project_id": "",
            }
        )

    project_id, tool_name, action_id, poll_seconds = agent_map[agent]
    try:
        if tool_name == "Assessment_Context_Expert":
            raw_result = await _call_customgpt_task(
                project_id,
                prompt_text,
                tool_name,
                action_id=action_id,
                poll_seconds=poll_seconds,
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
    True only when the user asks for market/listing/sale/comp/public aggregator
    work. Plain address lookup should stay ArcGIS-only and return immediately.
    """
    text = (prompt_text or "").lower()

    explicit_mode = (
        "mode: address / homeharvest lookup" in text
        or "mode: record + homeharvest comp support" in text
        or "homeharvest" in text
        or "public aggregator" in text
    )

    comp_or_listing_words = [
        "comp",
        "comps",
        "comparable",
        "similar properties",
        "nearby sales",
        "nearby sale",
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
        "market value support",
        "candidate comps",
    ]

    return explicit_mode or any(word in text for word in comp_or_listing_words)




def _request_needs_report_generation(prompt_text: str) -> bool:
    """
    True when staff asks for a generated report/file/PDF/export/download. These
    requests must go to Context Expert, not the ArcGIS-only fast path.
    """
    text = (prompt_text or "").lower()

    explicit_modes = [
        "mode: address report generation",
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

    action_rule = (
    "\n\nHOMEHARVEST ACTION RULE:\n"
    "- Use the enabled HomeHarvest custom action when the request is an address, nearby sale, or comp lookup.\n"
    "- Do not answer from memory, previous runs, cached examples, or stale conversation context. Run the action for this request.\n"
    "- Prefer operation homeharvestSearchProperties.\n"
    "- Use POST /properties/search.\n"
    "- Return staff-readable numbered cards, not raw JSON.\n"
    "- A no-result response is not a tool failure.\n"
    "\n"
    "GIS + HOMEHARVEST RULES:\n"
    "- If a PUBLIC ARCGIS PARCEL CONTEXT block is present, use it as the subject parcel anchor.\n"
    "- Return the GIS subject context first, then HomeHarvest/public-aggregator candidate results.\n"
    "- Do not treat GIS data as a verified sale, tax status, exemption approval/status, or certified record.\n"
    "- Verify final parcel/account details in iasWorld before relying on them.\n"
    "\n"
    "COMP SEARCH QUALITY RULES:\n"
    "- For comp requests, use listing_type=sold or sold status when supported.\n"
    "- For 'past 10 years', use date_from=2016-06-22 and date_to=2026-06-22 unless the user gives a different date range.\n"
    "- Exclude land, lots, mobile homes, manufactured homes, rentals, active listings, pending listings, and rows with missing price, missing date, missing sqft, or missing residential characteristics.\n"
    "- Do not return exactly 10 unless 10 usable residential candidates are found.\n"
    "- If fewer than 10 usable residential candidates are found, return only the usable candidates and clearly say how many were found.\n"
    "- Do not pad the list with land, missing-data rows, atypical low-price rows, or poor matches.\n"
    "- Prefer similar residential properties by property type, living area, beds, baths, lot size, year built, and proximity.\n"
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
    Community_Educator, Clear_Expectations, and Compliance_Expert should use
    conversations unless those projects are explicitly converted to Plan & Act.
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
    """HomeHarvest and report/file generation should not reuse completed cached answers."""
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
) -> str:
    config_error = _require_config(project_id, tool_name)
    if config_error:
        return config_error

    prompt_text = str(prompt_text or "").strip()
    if not prompt_text:
        return f"{tool_name} received an empty promptText."

    if tool_name == "Assessment_Context_Expert" and action_id:
        # ArcGIS runs first as a fast public parcel pre-check, then the combined
        # prompt is enriched with HomeHarvest instructions and submitted to CustomGPT.
        prompt_text = await _enrich_homeharvest_prompt_with_arcgis(prompt_text)
        prompt_text = _enrich_homeharvest_prompt(prompt_text)
    elif tool_name == "Assessment_Context_Expert" and _request_needs_report_generation(prompt_text):
        # Report/file/PDF/export generation should still get GIS context when an
        # address or UPC is present, but should not force HomeHarvest unless the
        # staff request also asks for comps/sales/listings/market data.
        prompt_text = await _enrich_report_generation_prompt_with_arcgis(prompt_text)

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
    """
    return await _call_customgpt_conversation(COMPLIANCE_PROJECT_ID, promptText, "Compliance_Expert")


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
