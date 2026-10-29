"""
CustomGPT specialist agents: settings, the task cache, submitting and polling tasks,
conversations, and download links for files a specialist generates.
"""

import asyncio
import json
import os
import re
import secrets
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from aces_arcgis import _enrich_homeharvest_prompt_with_arcgis, _enrich_report_generation_prompt_with_arcgis
from aces_common import (
    _error_preview,
    _extract_label_block,
    _has_any_word_or_phrase,
    _log,
    _now_iso,
    _read_json_or_text,
    _safe_json_dumps,
    _stable_hash,
)
from aces_routing import (
    _detect_assessment_status_message,
    _enrich_homeharvest_prompt,
    _request_needs_report_generation,
    _should_enable_homeharvest,
)

CUSTOMGPT_API_TOKEN = os.getenv("CUSTOMGPT_API_TOKEN", "").strip()

# Set these in the app settings (or the launcher, for local runs).
COMMUNITY_PROJECT_ID = os.getenv("COMMUNITY_PROJECT_ID", "").strip()
ASSESSMENT_PROJECT_ID = os.getenv("ASSESSMENT_PROJECT_ID", "94006").strip()
CLEAR_PROJECT_ID = os.getenv("CLEAR_PROJECT_ID", "").strip()
COMPLIANCE_PROJECT_ID = os.getenv("COMPLIANCE_PROJECT_ID", "").strip()

# CustomGPT project/agent with Plan & Act + Smart Tasks enabled.
SMART_TASKS_PROJECT_ID = os.getenv("SMART_TASKS_PROJECT_ID", "9262").strip()
SMART_TASKS_POLL_SECONDS = int(os.getenv("ACES_SMART_TASKS_POLL_SECONDS", "90"))
SMART_TASKS_AGENT_CAPABILITY = (
    os.getenv(
        "ACES_SMART_TASKS_AGENT_CAPABILITY",
        "complex-tasks",
    ).strip()
    or "complex-tasks"
)
SMART_TASKS_RESPONSE_SOURCE = (
    os.getenv(
        "ACES_SMART_TASKS_RESPONSE_SOURCE",
        "default",
    ).strip()
    or "default"
)

# HomeHarvest External API action ID inside CustomGPT project 94006.
HOMEHARVEST_ACTION_ID = os.getenv("HOMEHARVEST_ACTION_ID", "7").strip()

CUSTOMGPT_BASE = os.getenv("CUSTOMGPT_BASE", "https://app.customgpt.ai/api/v1").rstrip("/")

# The default is the system temp folder (/tmp in the container, %TEMP% on Windows), which
# doesn't survive a container restart; on Azure, point this at /home/... to keep it.
# Unfinished tasks always return Task ID + Project ID, so Check_CustomGPT_Task can poll
# CustomGPT directly even after the cache resets.
TASK_CACHE_FILE = os.getenv("TASK_CACHE_FILE", "").strip() or os.path.join(
    tempfile.gettempdir(), "aces_task_cache.json"
)
# How long a finished specialist answer is reused for the same question instead of asking
# the specialist again. Answers older than this are never reused, so a knowledge base
# update shows up within this window. 0 turns reuse off entirely (use it for evaluation
# runs, so every test question reaches the specialist).
ANSWER_CACHE_HOURS = float(os.getenv("ACES_ANSWER_CACHE_HOURS", "24"))
# Cache entries not touched for this many days are dropped when the cache is saved.
TASK_CACHE_KEEP_DAYS = float(os.getenv("ACES_TASK_CACHE_KEEP_DAYS", "7"))

# Generated Context Expert / CustomGPT artifact links.
# The server returns staff-safe download links that proxy through this server without
# exposing the CustomGPT API token to Copilot/Teams users. The links need this server's
# public address: ACES_PUBLIC_BASE_URL if set, otherwise the App Service host name
# (WEBSITE_HOSTNAME, set by Azure). With neither, file links are skipped.
CONTEXT_EXPERT_FILE_LINKS_ENABLED = os.getenv("ACES_CONTEXT_EXPERT_FILE_LINKS_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_WEBSITE_HOSTNAME = os.getenv("WEBSITE_HOSTNAME", "").strip()
PUBLIC_BASE_URL = (
    os.getenv("ACES_PUBLIC_BASE_URL", "").strip()
    or os.getenv("PUBLIC_BASE_URL", "").strip()
    or (f"https://{_WEBSITE_HOSTNAME}" if _WEBSITE_HOSTNAME else "")
).rstrip("/")
FILE_LINK_CACHE_FILE = os.getenv("ACES_FILE_LINK_CACHE_FILE", "").strip() or os.path.join(
    tempfile.gettempdir(), "aces_file_link_cache.json"
)
FILE_LINK_TTL_SECONDS = int(os.getenv("ACES_FILE_LINK_TTL_SECONDS", "86400"))

# CustomGPT tasks are async. These values keep MCP calls from holding open too long
# while still allowing quick tasks to finish in one response.
DEFAULT_POLL_SECONDS = int(os.getenv("ACES_DEFAULT_POLL_SECONDS", "60"))
HOMEHARVEST_POLL_SECONDS = int(os.getenv("ACES_HOMEHARVEST_POLL_SECONDS", "25"))
REPORT_GENERATION_POLL_SECONDS = int(os.getenv("ACES_REPORT_GENERATION_POLL_SECONDS", "25"))
# A specialist task is reported as stalled (a failure) when its progress steps haven't
# changed for TASK_STALL_MINUTES, or when it has run TASK_MAX_MINUTES in total. A stalled
# task isn't reused, and the agent stops offering to check it.
TASK_STALL_MINUTES = int(os.getenv("ACES_TASK_STALL_MINUTES", "4"))
TASK_MAX_MINUTES = int(os.getenv("ACES_TASK_MAX_MINUTES", "15"))
POLL_INTERVAL_SECONDS = float(os.getenv("ACES_POLL_INTERVAL_SECONDS", "3"))
MAX_CACHED_ANSWER_CHARS = int(os.getenv("ACES_MAX_CACHED_ANSWER_CHARS", "80000"))

# Fresh lookup behavior:
# Address / HomeHarvest / comps / listing work should usually create a fresh
# CustomGPT task. Otherwise Copilot can appear to call the MCP tool while the
# server returns an old cached answer in ~0.25 seconds.
FRESH_HOMEHARVEST_LOOKUPS = os.getenv("ACES_FRESH_HOMEHARVEST_LOOKUPS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# Set this to "true" only if you want exact repeated HomeHarvest prompts to reuse
# completed cached answers. The safer default for staff testing is false.
REUSE_COMPLETED_HOMEHARVEST_ANSWERS = os.getenv(
    "ACES_REUSE_COMPLETED_HOMEHARVEST_ANSWERS", "false"
).strip().lower() in {"1", "true", "yes", "on"}

# Optional: allow reuse of in-flight HomeHarvest tasks so repeated "check again"
# style calls do not spawn duplicate CustomGPT tasks while the first task is still running.
REUSE_RUNNING_HOMEHARVEST_TASKS = os.getenv("ACES_REUSE_RUNNING_HOMEHARVEST_TASKS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _trim_cached_answer(answer: str | None) -> str | None:
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
        # Whole words only, so "courthouse" stays "courthouse" and "planet" stays "planet".
        value = re.sub(rf"\b{re.escape(old)}\b", new, value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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


def _write_json_atomic(path: str, data: Any) -> None:
    """
    Write to a temporary file next to the target, then swap it in. A crash or a full disk
    mid-write leaves the previous file intact instead of a half-written one that the next
    start would throw away.
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _load_task_cache() -> dict[str, Any]:
    try:
        if os.path.exists(TASK_CACHE_FILE):
            with open(TASK_CACHE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
    except (OSError, ValueError) as exc:
        _log("task cache load failed, starting empty", file=TASK_CACHE_FILE, error=str(exc))
    return {}


def _prune_task_cache(cache: dict[str, Any]) -> int:
    """Drop entries not touched for TASK_CACHE_KEEP_DAYS (or with no readable time). Returns how many."""
    keep_minutes = TASK_CACHE_KEEP_DAYS * 24 * 60
    stale = []
    for key, value in cache.items():
        age = _minutes_since(value.get("updated_at") or value.get("created_at")) if isinstance(value, dict) else None
        if age is None or age > keep_minutes:
            stale.append(key)
    for key in stale:
        cache.pop(key, None)
    return len(stale)


def _save_task_cache(cache: dict[str, Any]) -> None:
    dropped = _prune_task_cache(cache)
    if dropped:
        _log("task cache pruned", dropped=dropped, kept=len(cache), keep_days=TASK_CACHE_KEEP_DAYS)
    try:
        _write_json_atomic(TASK_CACHE_FILE, cache)
    except (OSError, TypeError, ValueError) as exc:
        _log("task cache save failed", file=TASK_CACHE_FILE, error=str(exc))


TASK_CACHE: dict[str, Any] = _load_task_cache()


def _load_file_link_cache() -> dict[str, Any]:
    try:
        if os.path.exists(FILE_LINK_CACHE_FILE):
            with open(FILE_LINK_CACHE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                # Drop expired links on startup/load.
                now = time.time()
                return {
                    str(key): value
                    for key, value in loaded.items()
                    if isinstance(value, dict) and float(value.get("expires_at", 0) or 0) > now
                }
    except (OSError, TypeError, ValueError) as exc:
        _log("file link cache load failed, starting empty", file=FILE_LINK_CACHE_FILE, error=str(exc))
    return {}


def _save_file_link_cache(cache: dict[str, Any]) -> None:
    try:
        # Keep the JSON small by pruning expired entries before every save.
        now = time.time()
        expired = [
            key
            for key, value in cache.items()
            if not isinstance(value, dict) or float(value.get("expires_at", 0) or 0) <= now
        ]
        for key in expired:
            cache.pop(key, None)

        _write_json_atomic(FILE_LINK_CACHE_FILE, cache)
    except (OSError, TypeError, ValueError) as exc:
        _log("file link cache save failed", file=FILE_LINK_CACHE_FILE, error=str(exc))


FILE_LINK_CACHE: dict[str, Any] = _load_file_link_cache()


def _task_cache_key(project_id: str, tool_name: str, prompt_text: str) -> str:
    canonical = _canonical_prompt_for_cache(tool_name, prompt_text)
    return f"{project_id}|{tool_name}|{_stable_hash(canonical)}"


def _find_task_cache_entry(project_id: str, task_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for key, value in TASK_CACHE.items():
        if not isinstance(value, dict):
            continue
        if str(value.get("project_id", "")) == str(project_id) and str(value.get("task_id", "")) == str(task_id):
            return key, value
    return None, None


def _minutes_since(stamp: Any) -> float | None:
    """Minutes since a cache timestamp like 2026-10-15T15:12:01Z, or None if unreadable."""
    try:
        then = datetime.strptime(str(stamp or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - then).total_seconds() / 60.0


def _task_stall_reason(project_id: str, task_id: str) -> str:
    """Why a still-running task counts as stalled, or "" if it doesn't."""
    _, entry = _find_task_cache_entry(project_id, task_id)
    if not isinstance(entry, dict):
        return ""
    age = _minutes_since(entry.get("created_at"))
    idle = _minutes_since(entry.get("progress_changed_at") or entry.get("created_at"))
    if age is not None and age >= TASK_MAX_MINUTES:
        return f"it has run {int(age)} minutes without finishing"
    if idle is not None and idle >= TASK_STALL_MINUTES:
        return f"it has shown no new progress for {int(idle)} minutes"
    return ""


def _cached_answer_for_task(project_id: str, task_id: str) -> str:
    _, entry = _find_task_cache_entry(project_id, task_id)
    if isinstance(entry, dict):
        answer = str(entry.get("last_answer") or "").strip()
        if answer:
            return answer
    return ""


def _cache_entry_still_fresh(entry: dict[str, Any]) -> bool:
    """
    Whether a cached task may still stand in for asking the specialist again.

    A finished answer is reused for ANSWER_CACHE_HOURS after it came back. A task that
    is still running is reused only while it can still finish (TASK_MAX_MINUTES), so a
    repeated question doesn't start a second task. With ANSWER_CACHE_HOURS=0 nothing is
    reused. An entry with no readable time is treated as stale.
    """
    if ANSWER_CACHE_HOURS <= 0:
        return False
    if str(entry.get("last_answer") or "").strip() or entry.get("status") in {"completed", "answered"}:
        age = _minutes_since(entry.get("answered_at") or entry.get("updated_at"))
        return age is not None and age < ANSWER_CACHE_HOURS * 60
    age = _minutes_since(entry.get("created_at"))
    return age is not None and age < TASK_MAX_MINUTES


def _find_existing_prompt_task(project_id: str, tool_name: str, prompt_text: str) -> dict[str, Any] | None:
    """Return a still-fresh cached task for the same project/tool/semantic prompt."""
    key = _task_cache_key(project_id, tool_name, prompt_text)
    dedupe_hash = _stable_hash(_canonical_prompt_for_cache(tool_name, prompt_text))
    reusable_statuses = {"submitted", "polling", "still_running", "completed", "answered"}

    direct = TASK_CACHE.get(key)
    if isinstance(direct, dict) and _cache_entry_still_fresh(direct):
        task_id = str(direct.get("task_id") or "").strip()
        status = str(direct.get("status") or "").strip()
        if task_id and status in reusable_statuses:
            return direct

    for value in TASK_CACHE.values():
        if not isinstance(value, dict) or not _cache_entry_still_fresh(value):
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
    message_id: str | None = None,
    latest_status: str | None = None,
    progress_log: list[str] | None = None,
    error: str | None = None,
    answer: str | None = None,
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
        new_log = [str(x) for x in progress_log[-10:]]
        if new_log and new_log != existing.get("progress_log"):
            existing["progress_changed_at"] = _now_iso()
        existing["progress_log"] = new_log
    if error:
        existing["error"] = str(error)
    if answer is not None:
        existing["last_answer"] = _trim_cached_answer(answer)
        existing["answered_at"] = _now_iso()

    TASK_CACHE[key] = existing
    _save_task_cache(TASK_CACHE)
    _log("task cache saved", tool=tool_name, project=project_id, task_id=task_id, status=status, key=key)
    return key


def _update_task_cache_by_task_id(
    project_id: str,
    task_id: str,
    status: str,
    latest_status: str | None = None,
    message_id: str | None = None,
    progress_log: list[str] | None = None,
    error: str | None = None,
    answer: str | None = None,
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
        new_log = [str(x) for x in progress_log[-10:]]
        if new_log and new_log != existing.get("progress_log"):
            existing["progress_changed_at"] = _now_iso()
        existing["progress_log"] = new_log
    if error:
        existing["error"] = str(error)
    if answer is not None:
        existing["last_answer"] = _trim_cached_answer(answer)
        existing["answered_at"] = _now_iso()

    TASK_CACHE[key] = existing
    _save_task_cache(TASK_CACHE)
    _log("task cache updated", project=project_id, task_id=task_id, status=status, key=key)
    return key


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
) -> dict[str, str]:
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
        'http_status": 404',
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
        "unknown project id",
        "invalid task id",
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
        "conversation create failed",
        "conversation create did not return a session id",
        "conversation message failed",
        "conversation response was empty",
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


# Project and task IDs go into CustomGPT URLs that carry the server's API token, and some
# of them come from callers (Check_CustomGPT_Task, /check-pending-task, /smart-task).
# Only this server's own specialist projects are allowed, and IDs may hold only letters,
# digits, dashes and underscores, so a value like "9262/../../12345" can't reach
# another project or another API path.
_CUSTOMGPT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _known_project_ids() -> list[str]:
    """The CustomGPT projects this server is set up to call."""
    configured = (
        COMMUNITY_PROJECT_ID,
        ASSESSMENT_PROJECT_ID,
        CLEAR_PROJECT_ID,
        COMPLIANCE_PROJECT_ID,
        SMART_TASKS_PROJECT_ID,
    )
    return sorted({project for project in configured if project})


def _valid_customgpt_id(value: Any) -> bool:
    return bool(_CUSTOMGPT_ID_PATTERN.fullmatch(str(value or "")))


def _require_config(project_id: str, tool_name: str) -> str | None:
    if not CUSTOMGPT_API_TOKEN:
        return "Missing CUSTOMGPT_API_TOKEN on this server."
    if not project_id:
        return f"Missing project id for {tool_name}. Set it in the server's environment."
    if project_id not in _known_project_ids():
        return f"Unknown project ID for {tool_name}. Use one of: {', '.join(_known_project_ids())}."
    return None


def _extract_answer_from_message(final_data: Any) -> str:
    """Extract answer text from known CustomGPT response envelopes."""
    if not isinstance(final_data, dict):
        return ""

    candidates: list[Any] = []
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


def _extract_progress_log_from_task_data(data: dict[str, Any]) -> list[str]:
    progress_log: list[str] = []
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
    progress_log: list[str],
) -> str:
    if tool_name == "Assessment_Context_Expert":
        first_line = _detect_assessment_status_message(prompt_text)
    else:
        first_line = (
            f"{tool_name} task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
        )

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
    headers: dict[str, str],
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


def _extract_file_list_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Find file objects in the common CustomGPT files response envelopes."""
    found: list[dict[str, Any]] = []

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
                        item.get("name")
                        or item.get("file_name")
                        or item.get("filename")
                        or item.get("title")
                        or item.get("type")
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
    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in found:
        file_id = _extract_customgpt_file_id(item)
        key = file_id or json.dumps(item, sort_keys=True, default=str)[:200]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _extract_customgpt_file_id(file_obj: dict[str, Any]) -> str:
    for key in ("id", "file_id", "fileId", "uuid", "artifact_id", "artifactId"):
        value = file_obj.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _extract_customgpt_file_name(file_obj: dict[str, Any]) -> str:
    for key in ("name", "file_name", "fileName", "filename", "original_name", "title"):
        value = file_obj.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    file_id = _extract_customgpt_file_id(file_obj)
    return f"context_expert_artifact_{file_id}" if file_id else "context_expert_artifact"


def _is_generated_customgpt_artifact(file_obj: dict[str, Any]) -> bool:
    text_fields = " ".join(
        str(file_obj.get(key) or "").lower() for key in ("type", "kind", "category", "source", "storage_type", "origin")
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
    headers: dict[str, str],
) -> list[str]:
    """List CustomGPT generated artifacts and create staff-safe download links through this server."""
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
    except httpx.HTTPError as exc:
        _log(
            "context expert files list failed",
            project_id=project_id,
            task_id=task_id,
            message_id=message_id,
            error=str(exc),
        )
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
    links: list[str] = []
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
        _log(
            "context expert artifact links created",
            project_id=project_id,
            task_id=task_id,
            message_id=message_id,
            count=len(links),
        )

    return links


async def _append_context_expert_artifact_links(
    client: httpx.AsyncClient,
    answer: str,
    project_id: str,
    task_id: str,
    message_id: str | None,
    headers: dict[str, str],
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
    headers: dict[str, str],
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
    possible_lists: list[Any] = []
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
    if not _valid_customgpt_id(task_id):
        return "Invalid task ID. Task IDs are letters, digits, dashes and underscores only."

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
                _update_task_cache_by_task_id(
                    project_id, task_id, "answered", latest_status="history_fallback", answer=history_answer
                )
                return history_answer

            error_text = _safe_json_dumps(poll_data)
            _update_task_cache_by_task_id(
                project_id, task_id, "check_failed", latest_status="check_failed", error=error_text
            )
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
            # Record this check's progress first, so "no new progress" is measured correctly.
            _update_task_cache_by_task_id(
                project_id, task_id, "still_running", latest_status=latest_status, progress_log=progress_log
            )
            stall_reason = _task_stall_reason(project_id, task_id)
            if stall_reason:
                # Marked "stalled" so identical requests start a fresh task instead of reusing this one.
                _update_task_cache_by_task_id(project_id, task_id, "stalled", latest_status=latest_status)
                return (
                    f"Task failed: {stall_reason}, so it is treated as stalled.\n"
                    f"Project ID: {project_id}\n"
                    f"Task ID: {task_id}\n"
                    "The specialist agent didn't return an answer. Don't check this task again. "
                    "For facts about a parcel, use the ACES_NOV or ArcGIS tools, or check iasWorld; "
                    "otherwise ask again with a narrower question."
                )
            return (
                "Task is not complete yet.\n"
                f"Project ID: {project_id}\n"
                f"Task ID: {task_id}\n"
                f"Latest status: {latest_status}\n"
                f"Progress: {_safe_json_dumps(progress_log[-10:], 1000)}"
            )

        inline_answer = _extract_answer_from_message({"data": data}) or _extract_answer_from_message(data)
        if inline_answer:
            inline_answer = await _append_context_expert_artifact_links(
                client, inline_answer, project_id, task_id, str(message_id) if message_id else None, headers
            )
            _update_task_cache_by_task_id(
                project_id,
                task_id,
                "answered",
                latest_status=latest_status,
                message_id=str(message_id) if message_id else None,
                progress_log=progress_log,
                answer=inline_answer,
            )
            return inline_answer

        if not message_id:
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _update_task_cache_by_task_id(
                    project_id,
                    task_id,
                    "answered",
                    latest_status="history_fallback",
                    progress_log=progress_log,
                    answer=history_answer,
                )
                return history_answer

            _update_task_cache_by_task_id(
                project_id, task_id, "completed_no_message_id", latest_status=latest_status, progress_log=progress_log
            )
            return (
                "Task completed but no message_id was returned.\n"
                f"Project ID: {project_id}\n"
                f"Task ID: {task_id}\n"
                f"Response: {_safe_json_dumps(data)}"
            )

        answer = await _fetch_customgpt_final_message(client, project_id, task_id, str(message_id), headers)
        if answer.startswith("Final message fetch failed") or answer.startswith(
            "Task completed but the final answer was empty"
        ):
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                history_answer = await _append_context_expert_artifact_links(
                    client, history_answer, project_id, task_id, str(message_id), headers
                )
                _update_task_cache_by_task_id(
                    project_id,
                    task_id,
                    "answered",
                    latest_status="history_fallback",
                    message_id=str(message_id),
                    progress_log=progress_log,
                    answer=history_answer,
                )
                return history_answer

            _update_task_cache_by_task_id(
                project_id,
                task_id,
                "final_fetch_or_empty_failed",
                latest_status=latest_status,
                message_id=str(message_id),
                progress_log=progress_log,
                error=answer,
            )
            return answer

        answer = await _append_context_expert_artifact_links(
            client, answer, project_id, task_id, str(message_id), headers
        )
        _update_task_cache_by_task_id(
            project_id,
            task_id,
            "answered",
            latest_status=latest_status,
            message_id=str(message_id),
            progress_log=progress_log,
            answer=answer,
        )
        return answer


def _extract_session_id_from_conversation_response(data: Any) -> str:
    """Extract a CustomGPT conversation/session id from common response envelopes."""
    candidates: list[Any] = []
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
    agent_capability: str | None = None,
) -> str:
    """
    Use the normal CustomGPT conversation API for non-Plan & Act agents.

    The /tasks endpoint creates Plan & Act tasks and requires use_planner_mode.
    Clear_Expectations uses conversations.
    Community_Educator, Assessment_Context_Expert, and Compliance_Expert use Plan & Act tasks.
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
            response_preview=_error_preview(create.status_code, create_data),
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

        payload: dict[str, Any] = {
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
            response_preview=_error_preview(message.status_code, message_data),
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


def _is_homeharvest_lookup(tool_name: str, prompt_text: str, action_id: str | None) -> bool:
    return tool_name == "Assessment_Context_Expert" and bool(action_id or _should_enable_homeharvest(prompt_text))


def _is_fresh_assessment_task(tool_name: str, prompt_text: str, action_id: str | None) -> bool:
    """Run-specific tasks should not reuse completed cached answers."""
    if tool_name == "Smart_Tasks":
        return True

    return tool_name == "Assessment_Context_Expert" and (
        _is_homeharvest_lookup(tool_name, prompt_text, action_id) or _request_needs_report_generation(prompt_text)
    )


def _should_reuse_prompt_cache(tool_name: str, prompt_text: str, action_id: str | None) -> bool:
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


def _should_reuse_existing_homeharvest_task(existing_task: dict[str, Any]) -> bool:
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
    action_id: str | None = None,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    agent_capability: str | None = None,
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

    # Log size and hash only. Staff prompts can contain taxpayer details.
    _log(
        "specialist tool received prompt",
        tool=tool_name,
        project=project_id,
        action_id=action_id or "",
        prompt_chars=len(prompt_text),
        prompt_hash=_stable_hash(prompt_text),
    )

    should_reuse_prompt_cache = _should_reuse_prompt_cache(tool_name, prompt_text, action_id)

    # Duplicate-proofing: non-HomeHarvest tools can reuse cached answers.
    # HomeHarvest/address/comps lookups default to fresh submissions so repeated
    # Copilot tests actually reach CustomGPT/Context Expert instead of returning
    # an old last_answer from the task cache.
    existing_task = _find_existing_prompt_task(project_id, tool_name, prompt_text)
    if existing_task and should_reuse_prompt_cache:
        existing_answer = str(existing_task.get("last_answer") or "").strip()
        if existing_answer:
            _log(
                "CACHE HIT - returning cached answer without CustomGPT submit",
                tool=tool_name,
                project=project_id,
                task_id=existing_task.get("task_id"),
            )
            return existing_answer
        existing_task_id = str(existing_task.get("task_id") or "").strip()
        if existing_task_id:
            _log("CACHE HIT - rechecking existing task", tool=tool_name, project=project_id, task_id=existing_task_id)
            return await _check_customgpt_task_result(project_id=project_id, task_id=existing_task_id)

    if (
        existing_task
        and _is_fresh_assessment_task(tool_name, prompt_text, action_id)
        and _should_reuse_existing_homeharvest_task(existing_task)
    ):
        existing_task_id = str(existing_task.get("task_id") or "").strip()
        if existing_task_id:
            _log("REUSING IN-FLIGHT ASSESSMENT TASK", tool=tool_name, project=project_id, task_id=existing_task_id)
            return await _check_customgpt_task_result(project_id=project_id, task_id=existing_task_id)

    if existing_task and not should_reuse_prompt_cache:
        _log(
            "BYPASSING PROMPT CACHE - fresh HomeHarvest/Assessment lookup",
            tool=tool_name,
            project=project_id,
            previous_task_id=existing_task.get("task_id"),
        )

    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "application/json"}

    if not should_reuse_prompt_cache:
        task_name = f"ACES|{tool_name}|fresh|{int(time.time())}|{_stable_hash(prompt_text)}"
    else:
        task_name = f"ACES|{tool_name}|{_stable_hash(prompt_text)}"

    _log("SUBMITTING NEW CUSTOMGPT TASK", tool=tool_name, project=project_id, task_name=task_name)

    capability = (agent_capability or "optimal-choice").strip()
    selected_response_source = (response_source or "openai_content").strip()

    multipart: dict[str, Any] = {
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
        _log(
            "CustomGPT submit response",
            tool=tool_name,
            project=project_id,
            http_status=submit.status_code,
            response_preview=_error_preview(submit.status_code, submit_data),
        )

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
        progress_log: list[str] = []
        completed_data: dict[str, Any] = {}

        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            poll = await client.get(base, headers=headers)
            poll_data = await _read_json_or_text(poll)

            if poll.status_code >= 400:
                error_text = _safe_json_dumps(poll_data)
                _remember_task(
                    project_id,
                    tool_name,
                    prompt_text,
                    task_id,
                    "poll_failed",
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
                        project_id,
                        tool_name,
                        prompt_text,
                        task_id,
                        "task_failed",
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
                project_id,
                tool_name,
                prompt_text,
                task_id,
                "polling",
                latest_status=latest_status,
                progress_log=progress_log,
            )

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
            _remember_task(
                project_id,
                tool_name,
                prompt_text,
                task_id,
                "still_running",
                latest_status=latest_status,
                progress_log=progress_log,
            )
            return _format_still_running_response(
                project_id, tool_name, prompt_text, task_id, cache_key, latest_status, progress_log
            )

        inline_answer = _extract_answer_from_message({"data": completed_data}) or _extract_answer_from_message(
            completed_data
        )
        if inline_answer:
            inline_answer = await _append_context_expert_artifact_links(
                client, inline_answer, project_id, task_id, str(message_id) if message_id else None, headers
            )
            _remember_task(
                project_id,
                tool_name,
                prompt_text,
                task_id,
                "answered",
                latest_status=latest_status,
                message_id=str(message_id) if message_id else None,
                progress_log=progress_log,
                answer=inline_answer,
            )
            return inline_answer

        if not message_id:
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                _remember_task(
                    project_id,
                    tool_name,
                    prompt_text,
                    task_id,
                    "answered",
                    latest_status="history_fallback",
                    progress_log=progress_log,
                    answer=history_answer,
                )
                return history_answer

            _remember_task(
                project_id,
                tool_name,
                prompt_text,
                task_id,
                "completed_no_message_id",
                latest_status=latest_status,
                progress_log=progress_log,
                error=_safe_json_dumps(completed_data),
            )
            return (
                f"{tool_name} task completed but no message_id was returned.\n"
                f"Task ID: {task_id}\n"
                f"Project ID: {project_id}\n"
                f"Cache key: {cache_key}\n"
                f"Response: {_safe_json_dumps(completed_data)}"
            )

        answer = await _fetch_customgpt_final_message(client, project_id, task_id, str(message_id), headers)
        if answer.startswith("Final message fetch failed") or answer.startswith(
            "Task completed but the final answer was empty"
        ):
            history_answer = await _fetch_task_history_fallback(client, project_id, task_id, headers)
            if history_answer:
                history_answer = await _append_context_expert_artifact_links(
                    client, history_answer, project_id, task_id, str(message_id), headers
                )
                _remember_task(
                    project_id,
                    tool_name,
                    prompt_text,
                    task_id,
                    "answered",
                    latest_status="history_fallback",
                    message_id=str(message_id),
                    progress_log=progress_log,
                    answer=history_answer,
                )
                return history_answer

            _remember_task(
                project_id,
                tool_name,
                prompt_text,
                task_id,
                "final_fetch_or_empty_failed",
                latest_status=latest_status,
                message_id=str(message_id),
                progress_log=progress_log,
                error=answer,
            )
            return f"{tool_name} final retrieval failed.\nTask ID: {task_id}\nProject ID: {project_id}\nCache key: {cache_key}\n{answer}"

        answer = await _append_context_expert_artifact_links(
            client, answer, project_id, task_id, str(message_id), headers
        )
        _remember_task(
            project_id,
            tool_name,
            prompt_text,
            task_id,
            "answered",
            latest_status=latest_status,
            message_id=str(message_id),
            progress_log=progress_log,
            answer=answer,
        )
        return answer
