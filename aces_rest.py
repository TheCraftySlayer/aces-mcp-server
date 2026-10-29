"""
REST wrappers for Power Automate, and the admin debug routes. Every route here needs the
admin token except /context-expert-file/{token}, where the random, expiring token in the
link is the credential.
"""

import time
from typing import Any

import httpx
from starlette.responses import JSONResponse, RedirectResponse, Response

from aces_access import _require_admin_debug_auth, _require_rest_auth
from aces_arcgis import (
    ARCGIS_PUBLIC_PARCEL_LAYER_URL,
    ARCGIS_PUBLIC_PARCEL_MAX_RESULTS,
    _arcgis_public_candidate_peers_result,
    _arcgis_public_parcel_lookup_result,
    _enrich_report_generation_prompt_with_arcgis,
    _extract_arcgis_search_text_from_prompt,
    _format_arcgis_candidate_peers_text,
    _format_arcgis_map_routing_text,
    _format_arcgis_parcel_lookup_text,
)
from aces_common import _log
from aces_customgpt import (
    ASSESSMENT_PROJECT_ID,
    CLEAR_PROJECT_ID,
    COMMUNITY_PROJECT_ID,
    COMPLIANCE_PROJECT_ID,
    CUSTOMGPT_API_TOKEN,
    CUSTOMGPT_BASE,
    DEFAULT_POLL_SECONDS,
    FILE_LINK_CACHE,
    HOMEHARVEST_ACTION_ID,
    HOMEHARVEST_POLL_SECONDS,
    REPORT_GENERATION_POLL_SECONDS,
    SMART_TASKS_AGENT_CAPABILITY,
    SMART_TASKS_POLL_SECONDS,
    SMART_TASKS_PROJECT_ID,
    SMART_TASKS_RESPONSE_SOURCE,
    TASK_CACHE,
    _call_customgpt_conversation,
    _call_customgpt_task,
    _check_customgpt_task_result,
    _known_project_ids,
    _normalize_aces_result,
    _safe_download_filename,
    _save_file_link_cache,
    _save_task_cache,
    _valid_customgpt_id,
)
from aces_routing import (
    _enrich_homeharvest_prompt,
    _request_needs_report_generation,
    _should_enable_homeharvest,
    _should_use_arcgis_map_lookup,
    _should_use_arcgis_only_lookup,
)


async def _request_json_or_empty(request) -> dict[str, Any]:
    """Read JSON safely from Starlette request; return {} for empty/invalid JSON."""
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


async def task_cache_debug(request):
    """Admin-only view of the task cache: task IDs, prompt hashes and cached specialist answers."""
    if (denied := _require_admin_debug_auth(request)) is not None:
        return denied
    return JSONResponse({"count": len(TASK_CACHE), "tasks": TASK_CACHE})


async def task_cache_clear(request):
    """Admin-only route to clear the local task cache."""
    if (denied := _require_admin_debug_auth(request)) is not None:
        return denied

    count = len(TASK_CACHE)
    TASK_CACHE.clear()
    _save_task_cache(TASK_CACHE)
    _log("task cache cleared", count=count)
    return JSONResponse({"cleared": True, "previous_count": count, "count": len(TASK_CACHE)})


async def context_expert_file_download_route(request):
    """
    Public, unguessable download route for Context Expert generated artifacts.

    The token maps to CustomGPT project/task/message/file metadata stored in the
    local file-link cache. Staff see a link to this server in Copilot/Teams; the
    CustomGPT API token stays server-side.
    """
    token = str(request.path_params.get("token") or "").strip()
    if not token:
        return JSONResponse({"status": "failed", "answer": "Missing download token."}, status_code=400)

    meta = FILE_LINK_CACHE.get(token)
    if not isinstance(meta, dict):
        return JSONResponse(
            {"status": "failed", "answer": "Download link not found or the server restarted."}, status_code=404
        )

    if time.time() > float(meta.get("expires_at", 0) or 0):
        FILE_LINK_CACHE.pop(token, None)
        _save_file_link_cache(FILE_LINK_CACHE)
        return JSONResponse(
            {
                "status": "failed",
                "answer": "Download link expired. Ask A.C.E.S. to regenerate or rerun the Context Expert task.",
            },
            status_code=410,
        )

    project_id = str(meta.get("project_id") or "").strip()
    task_id = str(meta.get("task_id") or "").strip()
    message_id = str(meta.get("message_id") or "").strip()
    file_id = str(meta.get("file_id") or "").strip()
    file_name = _safe_download_filename(str(meta.get("file_name") or f"context_expert_file_{file_id}"))

    if not all(_valid_customgpt_id(value) for value in (project_id, task_id, message_id, file_id)):
        return JSONResponse({"status": "failed", "answer": "Download link metadata is incomplete."}, status_code=500)
    if project_id not in _known_project_ids():
        return JSONResponse({"status": "failed", "answer": "Download link is for an unknown project."}, status_code=500)

    if not CUSTOMGPT_API_TOKEN:
        return JSONResponse({"status": "failed", "answer": "Server is missing CUSTOMGPT_API_TOKEN."}, status_code=500)

    url = f"{CUSTOMGPT_BASE}/projects/{project_id}/tasks/{task_id}/messages/{message_id}/files/{file_id}/download"
    headers = {"Authorization": f"Bearer {CUSTOMGPT_API_TOKEN}", "Accept": "*/*"}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=60, write=30, pool=15), follow_redirects=False
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        _log(
            "context expert file download failed",
            error=str(exc),
            project_id=project_id,
            task_id=task_id,
            file_id=file_id,
        )
        return JSONResponse(
            {"status": "failed", "answer": f"Could not contact CustomGPT for the generated file: {exc}"},
            status_code=502,
        )

    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if location:
            return RedirectResponse(location)
        return JSONResponse(
            {"status": "failed", "answer": "CustomGPT returned a redirect without a location."}, status_code=502
        )

    if response.status_code == 200:
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )

    body_preview = response.text[:1200] if response.text else ""
    _log(
        "context expert file download bad status",
        http_status=response.status_code,
        body_preview=body_preview,
        file_id=file_id,
    )
    return JSONResponse(
        {
            "status": "failed",
            "answer": "CustomGPT could not return the generated file. It may have expired; rerun the Context Expert task and download immediately.",
            "http_status": response.status_code,
            "response_preview": body_preview,
        },
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


async def check_task_route(request):
    if (denied := _require_admin_debug_auth(request)) is not None:
        return denied

    project_id = str(request.query_params.get("project_id") or ASSESSMENT_PROJECT_ID).strip()
    task_id = str(request.query_params.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "Missing task_id query parameter."}, status_code=400)

    result = await _check_customgpt_task_result(project_id=project_id, task_id=task_id)
    return JSONResponse({"project_id": project_id, "task_id": task_id, "result": result})


async def arcgis_parcel_lookup_route(request):
    """
    Fast read-only REST wrapper for the public BernCo ArcGIS parcel layer.

    POST /arcgis-parcel-lookup
    Headers:
      x-aces-admin-token: <ACES_ADMIN_TOKEN>
      Content-Type: application/json
    Body:
      {"searchText": "2 Lauren Taylor Ct Tijeras NM", "maxResults": 10}

    Also supports GET (token still goes in the header):
      /arcgis-parcel-lookup?searchText=2%20Lauren%20Taylor%20Ct%20Tijeras%20NM&maxResults=10
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: dict[str, Any] = {}
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
    return_geometry_raw = (
        str(
            body.get("returnGeometry")
            or body.get("return_geometry")
            or request.query_params.get("returnGeometry")
            or request.query_params.get("return_geometry")
            or "false"
        )
        .strip()
        .lower()
    )
    return_geometry = return_geometry_raw in {"1", "true", "yes", "on"}

    result = await _arcgis_public_parcel_lookup_result(
        search_text=search_text,
        max_results=max_results_raw,
        return_geometry=return_geometry,
    )
    return JSONResponse(result)


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

    Also supports GET (token still goes in the header):
      /arcgis-parcel-map?searchText=2%20Lauren%20Taylor%20Ct%20Tijeras%20NM&maxResults=5
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: dict[str, Any] = {}
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
        body: dict[str, Any] = {}
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
    except Exception as exc:  # noqa: BLE001  Power Automate expects a JSON answer even when something unexpected fails
        _log("start_lookup_route exception", error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"The lookup did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": ASSESSMENT_PROJECT_ID,
            }
        )


async def check_pending_task_route(request):
    """
    Normalized REST wrapper for polling an existing CustomGPT task.

    POST /check-pending-task
    Body:
      {"task_id":"...", "project_id":"94006"}

    Also supports GET (token still goes in the header):
      /check-pending-task?project_id=94006&task_id=...
    """
    auth_response = _require_rest_auth(request)
    if auth_response:
        return auth_response

    if request.method == "GET":
        body: dict[str, Any] = {}
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
    except Exception as exc:  # noqa: BLE001  Power Automate expects a JSON answer even when something unexpected fails
        _log("check_pending_task_route exception", project_id=project_id, task_id=task_id, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"The pending lookup did not return usable results. Error: {exc}",
                "task_id": task_id,
                "project_id": project_id,
            }
        )


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
    project_id = str(body.get("project_id") or body.get("projectId") or SMART_TASKS_PROJECT_ID).strip()

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
    except Exception as exc:  # noqa: BLE001  Power Automate expects a JSON answer even when something unexpected fails
        _log("smart_task_route exception", project_id=project_id, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"Smart_Tasks did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": project_id,
            }
        )


async def agent_call_route(request):
    """
    Optional normalized REST wrapper for A.C.E.S. specialist agents.

    Body:
      {"agent":"Community_Educator|Clear_Expectations|Compliance_Expert|Assessment_Context_Expert|Smart_Tasks|
                ArcGIS_Public_Parcel_Lookup|ArcGIS_Public_Parcel_Map|ArcGIS_Public_Candidate_Peers",
       "promptText":"..."}

    ArcGIS agents return status completed|failed plus arcgis_status with the raw
    ArcGIS result (completed, empty, needs_subject_selection, failed).

    Community_Educator, Assessment_Context_Expert, Compliance_Expert, and Smart_Tasks use Plan & Act task mode.
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

    arcgis_lookup_names = {"ArcGIS_Public_Parcel_Lookup", "ArcGIS_Public_Parcel", "ArcGIS", "ArcGIS_Parcel_Lookup"}
    arcgis_map_names = {"ArcGIS_Public_Parcel_Map", "ArcGIS_Parcel_Map", "ArcGIS_Map", "Parcel_Map"}
    arcgis_peer_names = {"ArcGIS_Public_Candidate_Peers", "ArcGIS_Candidate_Peers", "Candidate_Peers"}

    if agent in arcgis_lookup_names | arcgis_map_names | arcgis_peer_names:
        # Pull the address/UPC out of a sentence, same as /start-lookup.
        search_text = _extract_arcgis_search_text_from_prompt(prompt_text) or prompt_text

        try:
            if agent in arcgis_peer_names:
                max_results = body.get("maxResults") or body.get("max_results") or 10
                result = await _arcgis_public_candidate_peers_result(search_text, max_results=max_results)
                answer = _format_arcgis_candidate_peers_text(result)
                extra = {"subject": result.get("subject"), "peers": result.get("peers", [])}
            else:
                is_map = agent in arcgis_map_names
                result = await _arcgis_public_parcel_lookup_result(
                    search_text,
                    max_results=5 if is_map else ARCGIS_PUBLIC_PARCEL_MAX_RESULTS,
                    return_geometry=False,
                )
                answer = (
                    _format_arcgis_map_routing_text(result) if is_map else _format_arcgis_parcel_lookup_text(result)
                )
                extra = {"results": result.get("results", [])}
        except Exception as exc:  # noqa: BLE001  Power Automate expects a JSON answer even when something unexpected fails
            _log("agent_call_route arcgis exception", agent=agent, error=str(exc))
            return JSONResponse(
                {
                    "status": "failed",
                    "answer": f"{agent} did not return usable results. Error: {exc}",
                    "task_id": "",
                    "project_id": ASSESSMENT_PROJECT_ID,
                }
            )

        # Map ArcGIS statuses onto the Power Automate contract.
        # empty and needs_subject_selection are answered lookups, not failures;
        # the answer text explains them. The raw status stays in arcgis_status.
        raw_status = str(result.get("status") or "")
        status = "failed" if raw_status == "failed" else "completed"

        return JSONResponse(
            {
                "status": status,
                "answer": answer,
                "task_id": "",
                "project_id": ASSESSMENT_PROJECT_ID,
                "source": result.get("source", "Bernalillo County Assessor Parcels public ArcGIS layer"),
                "arcgis_status": raw_status,
                **extra,
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
        plan_act_tools = {"Community_Educator", "Assessment_Context_Expert", "Compliance_Expert", "Smart_Tasks"}

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
    except Exception as exc:  # noqa: BLE001  Power Automate expects a JSON answer even when something unexpected fails
        _log("agent_call_route exception", agent=agent, error=str(exc))
        return JSONResponse(
            {
                "status": "failed",
                "answer": f"{agent} did not return usable results. Error: {exc}",
                "task_id": "",
                "project_id": str(project_id or ""),
            }
        )


def register(mcp) -> None:
    """Add the REST wrapper and admin routes to the server."""
    mcp.custom_route("/task-cache", methods=["GET"])(task_cache_debug)
    mcp.custom_route("/task-cache/clear", methods=["POST"])(task_cache_clear)
    mcp.custom_route("/context-expert-file/{token}", methods=["GET"])(context_expert_file_download_route)
    mcp.custom_route("/check-task", methods=["GET"])(check_task_route)
    mcp.custom_route("/arcgis-parcel-lookup", methods=["GET", "POST"])(arcgis_parcel_lookup_route)
    mcp.custom_route("/arcgis-parcel-map", methods=["GET", "POST"])(arcgis_parcel_map_route)
    mcp.custom_route("/arcgis-candidate-peers", methods=["GET", "POST"])(arcgis_candidate_peers_route)
    mcp.custom_route("/start-lookup", methods=["POST"])(start_lookup_route)
    mcp.custom_route("/check-pending-task", methods=["GET", "POST"])(check_pending_task_route)
    mcp.custom_route("/smart-task", methods=["POST"])(smart_task_route)
    mcp.custom_route("/agent-call", methods=["POST"])(agent_call_route)
