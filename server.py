import os
import json
import asyncio
import hashlib
from typing import Optional, Any, Dict

# FastMCP now wants these as environment settings or HTTP-layer settings,
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
        "Exposes four specialist tools: Community_Educator, "
        "Assessment_Context_Expert, Clear_Expectations, and Compliance_Expert. "
        "Each tool takes exactly one promptText string and returns plain text."
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

CUSTOMGPT_BASE = "https://app.customgpt.ai/api/v1"


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
        }
    )


def _require_config(project_id: str, tool_name: str) -> Optional[str]:
    if not CUSTOMGPT_API_TOKEN:
        return "Missing CUSTOMGPT_API_TOKEN on Render."

    if not project_id:
        return f"Missing project id for {tool_name}. Set it in Render Environment."

    return None


def _stable_hash(text: str) -> str:
    value = text or ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _should_enable_homeharvest(prompt_text: str) -> bool:
    text = (prompt_text or "").lower()

    return (
        "mode: address / homeharvest lookup" in text
        or "mode: record + homeharvest comp support" in text
        or "homeharvest" in text
        or "nearby sales" in text
        or "nearby sale" in text
        or "comps" in text
        or "comparable" in text
        or "sold properties" in text
        or "public aggregator" in text
    )


def _detect_assessment_status_message(prompt_text: str) -> str:
    text = (prompt_text or "").lower()

    if "mode: address / homeharvest lookup" in text:
        return "Address lookup is still running. Check again in about a minute."

    if "mode: record + homeharvest comp support" in text:
        return "The record and comp-support analysis is still running. Check again in about a minute."

    return "The analysis is still running. Check again in about a minute."


def _safe_json_dumps(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)

    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"

    return text


async def _read_json_or_text(response: httpx.Response) -> Any:
    text = response.text or ""

    try:
        return response.json()
    except Exception:
        return {"raw": text}


def _extract_answer_from_message(final_data: Any) -> str:
    if not isinstance(final_data, dict):
        return ""

    data = final_data.get("data") or {}

    if isinstance(data, dict):
        answer = (
            data.get("response")
            or data.get("openai_response")
            or data.get("agent_answer")
            or data.get("message")
            or ""
        )

        if answer:
            return str(answer)

    # Some APIs wrap messages differently.
    answer = (
        final_data.get("response")
        or final_data.get("openai_response")
        or final_data.get("agent_answer")
        or final_data.get("message")
        or ""
    )

    return str(answer or "")


async def _call_customgpt_task(
    project_id: str,
    prompt_text: str,
    tool_name: str,
    action_id: Optional[str] = None,
    poll_seconds: int = 55,
) -> str:
    config_error = _require_config(project_id, tool_name)
    if config_error:
        return config_error

    prompt_text = str(prompt_text or "").strip()

    if not prompt_text:
        return f"{tool_name} received an empty promptText."

    headers = {
        "Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}",
        "Accept": "application/json",
    }

    task_name = f"ACES|{tool_name}|{_stable_hash(prompt_text)}"

    # CustomGPT Tasks API uses multipart/form-data.
    # response_source must be openai_content.
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

        base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + poll_seconds

        message_id = None
        latest_status = ""
        progress_log = []

        while loop.time() < deadline:
            await asyncio.sleep(3)

            poll = await client.get(base, headers=headers)
            poll_data = await _read_json_or_text(poll)

            if poll.status_code >= 400:
                return (
                    f"{tool_name} task poll failed.\n"
                    f"Task ID: {task_id}\n"
                    f"HTTP status: {poll.status_code}\n"
                    f"Response: {_safe_json_dumps(poll_data)}"
                )

            data = poll_data.get("data", {}) if isinstance(poll_data, dict) else {}
            latest_status = str(data.get("status") or "")

            events = data.get("events", []) or []

            for ev in events:
                ev = ev or {}
                ev_data = ev.get("data", {}) or {}

                if ev.get("type") == "error":
                    return (
                        f"{tool_name} task failed.\n"
                        f"Task ID: {task_id}\n"
                        f"Error: {ev_data.get('message', 'Unknown error')}"
                    )

                if ev_data.get("current_task"):
                    progress_log.append(str(ev_data.get("current_task")))

            if latest_status == "completed":
                result = data.get("result") or {}
                message_id = result.get("message_id")
                break

        if not message_id:
            if tool_name == "Assessment_Context_Expert":
                return _detect_assessment_status_message(prompt_text)

            return (
                f"{tool_name} is still running. Check again in about a minute.\n"
                f"Task ID: {task_id}\n"
                f"Latest status: {latest_status}\n"
                f"Progress: {_safe_json_dumps(progress_log, 1000)}"
            )

        final = await client.get(f"{base}/messages/{message_id}", headers=headers)
        final_data = await _read_json_or_text(final)

        if final.status_code >= 400:
            return (
                f"{tool_name} final message fetch failed.\n"
                f"Task ID: {task_id}\n"
                f"Message ID: {message_id}\n"
                f"HTTP status: {final.status_code}\n"
                f"Response: {_safe_json_dumps(final_data)}"
            )

        answer = _extract_answer_from_message(final_data)

        if not answer:
            return (
                f"{tool_name} returned no final answer.\n"
                f"Task ID: {task_id}\n"
                f"Message ID: {message_id}\n"
                f"Final response: {_safe_json_dumps(final_data)}"
            )

        return answer


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

    return await _call_customgpt_task(
        ASSESSMENT_PROJECT_ID,
        promptText,
        "Assessment_Context_Expert",
        action_id=action_id,
    )


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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
