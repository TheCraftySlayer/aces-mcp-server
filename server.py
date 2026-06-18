import os
import json
import asyncio
from typing import Optional

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
    stateless_http=True,
    json_response=True

)

CUSTOMGPT_API_TOKEN = os.getenv("CUSTOMGPT_API_TOKEN", "")

# Set these in Render Environment.
COMMUNITY_PROJECT_ID = os.getenv("COMMUNITY_PROJECT_ID", "")
ASSESSMENT_PROJECT_ID = os.getenv("ASSESSMENT_PROJECT_ID", "94006")
CLEAR_PROJECT_ID = os.getenv("CLEAR_PROJECT_ID", "")
COMPLIANCE_PROJECT_ID = os.getenv("COMPLIANCE_PROJECT_ID", "")

# HomeHarvest external API action ID inside project 94006.
HOMEHARVEST_ACTION_ID = os.getenv("HOMEHARVEST_ACTION_ID", "7")

CUSTOMGPT_BASE = "https://app.customgpt.ai/api/v1"


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "aces-mcp-server"})


def _require_config(project_id: str, tool_name: str) -> Optional[str]:
    if not CUSTOMGPT_API_TOKEN:
        return "Missing CUSTOMGPT_API_TOKEN on Render."
    if not project_id:
        return f"Missing project id for {tool_name}. Set it in Render Environment."
    return None


def _should_enable_homeharvest(prompt_text: str) -> bool:
    text = (prompt_text or "").lower()
    return (
        "mode: address / homeharvest lookup" in text
        or "mode: record + homeharvest comp support" in text
        or "homeharvest" in text
        or "nearby sales" in text
        or "comps" in text
        or "comparable" in text
        or "sold properties" in text
    )


async def _read_json_or_text(response: httpx.Response):
    text = response.text
    try:
        return response.json()
    except Exception:
        return {"raw": text}


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

    headers = {
        "Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}",
        "Accept": "application/json",
    }

    task_name = f"ACES|{tool_name}|{abs(hash(prompt_text))}"

    # CustomGPT task API creates a Plan & Act task and must be polled.
    # action_overrides must be JSON-encoded when sent as multipart form data.
    multipart = {
        "name": (None, task_name),
        "prompt": (None, prompt_text),
        "response_source": (None, "optimal-choice"),
        "agent_capability": (None, "optimal-choice"),
    }

    if action_id:
        multipart["action_overrides"] = (
            None,
            json.dumps({"enabled": [str(action_id)], "disabled": []}),
        )

    async with httpx.AsyncClient(timeout=30) as client:
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
                f"Response: {json.dumps(submit_data)[:3000]}"
            )

        task_id = (
            submit_data.get("data", {}).get("id")
            if isinstance(submit_data, dict)
            else None
        )

        if not task_id:
            return (
                f"{tool_name} task submit did not return a task id.\n"
                f"Response: {json.dumps(submit_data)[:3000]}"
            )

        base = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}"
        deadline = asyncio.get_event_loop().time() + poll_seconds
        message_id = None
        last_progress = []

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)

            poll = await client.get(base, headers=headers)
            poll_data = await _read_json_or_text(poll)

            if poll.status_code >= 400:
                return (
                    f"{tool_name} task poll failed.\n"
                    f"Task ID: {task_id}\n"
                    f"HTTP status: {poll.status_code}\n"
                    f"Response: {json.dumps(poll_data)[:3000]}"
                )

            data = poll_data.get("data", {}) if isinstance(poll_data, dict) else {}

            for ev in data.get("events", []) or []:
                ev_data = ev.get("data", {}) or {}
                if ev.get("type") == "error":
                    return (
                        f"{tool_name} task failed.\n"
                        f"Task ID: {task_id}\n"
                        f"Error: {ev_data.get('message', 'Unknown error')}"
                    )
                if ev_data.get("current_task"):
                    last_progress.append(ev_data.get("current_task"))

            if data.get("status") == "completed":
                result = data.get("result") or {}
                message_id = result.get("message_id")
                break

        if not message_id:
            if tool_name == "Assessment_Context_Expert":
                if "mode: address / homeharvest lookup" in prompt_text.lower():
                    return "Address lookup is still running. Check again in about a minute."
                if "mode: record + homeharvest comp support" in prompt_text.lower():
                    return "The record and comp-support analysis is still running. Check again in about a minute."

            return (
                f"{tool_name} is still running. Check again in about a minute.\n"
                f"Task ID: {task_id}"
            )

        final = await client.get(f"{base}/messages/{message_id}", headers=headers)
        final_data = await _read_json_or_text(final)

        if final.status_code >= 400:
            return (
                f"{tool_name} final message fetch failed.\n"
                f"Task ID: {task_id}\n"
                f"Message ID: {message_id}\n"
                f"HTTP status: {final.status_code}\n"
                f"Response: {json.dumps(final_data)[:3000]}"
            )

        msg = final_data.get("data", {}) if isinstance(final_data, dict) else {}
        answer = msg.get("response") or msg.get("openai_response") or ""

        if not answer:
            return (
                f"{tool_name} returned no final answer.\n"
                f"Task ID: {task_id}\n"
                f"Message ID: {message_id}"
            )

        return str(answer)


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
