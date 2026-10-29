import os
import re
import secrets
from html import escape as html_escape
from typing import Any

# FastMCP reads these through settings/env for Streamable HTTP behavior.
os.environ.setdefault("FASTMCP_STATELESS_HTTP", "true")
os.environ.setdefault("FASTMCP_JSON_RESPONSE", "true")

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, RedirectResponse, Response

import aces_access
import aces_chat
import aces_facts
import aces_oidc
import aces_rest
import aces_sql_tools
from aces_access import (
    ACES_ADMIN_TOKEN,
    ACES_MCP_API_KEY,
    ALLOWED_GROUPS,
    ALLOWED_USERS,
    ENTRA_SIGNIN_ACTIVE,
    ENTRA_SIGNIN_REQUESTED,
    McpApiKeyMiddleware,
    _not_on_list_page,
    _require_admin_debug_auth,
    _require_rest_auth,
    _signed_in_user,
)
from aces_arcgis import (
    ARCGIS_PUBLIC_PARCEL_LAYER_URL,
    ENRICH_HOMEHARVEST_WITH_ARCGIS,
    _arcgis_public_candidate_peers_result,
    _arcgis_public_parcel_lookup_result,
    _format_arcgis_candidate_peers_text,
    _format_arcgis_map_routing_text,
    _format_arcgis_parcel_lookup_text,
    _normalize_arcgis_situs_from_user_text,
)
from aces_common import _log
from aces_customgpt import (
    ANSWER_CACHE_HOURS,
    ASSESSMENT_PROJECT_ID,
    CLEAR_PROJECT_ID,
    COMMUNITY_PROJECT_ID,
    COMPLIANCE_PROJECT_ID,
    CONTEXT_EXPERT_FILE_LINKS_ENABLED,
    DEFAULT_POLL_SECONDS,
    FILE_LINK_CACHE,
    FILE_LINK_TTL_SECONDS,
    FRESH_HOMEHARVEST_LOOKUPS,
    HOMEHARVEST_ACTION_ID,
    HOMEHARVEST_POLL_SECONDS,
    PUBLIC_BASE_URL,
    REPORT_GENERATION_POLL_SECONDS,
    REUSE_COMPLETED_HOMEHARVEST_ANSWERS,
    REUSE_RUNNING_HOMEHARVEST_TASKS,
    SMART_TASKS_AGENT_CAPABILITY,
    SMART_TASKS_POLL_SECONDS,
    SMART_TASKS_PROJECT_ID,
    SMART_TASKS_RESPONSE_SOURCE,
    TASK_CACHE,
    TASK_CACHE_FILE,
    TASK_CACHE_KEEP_DAYS,
    _call_customgpt_conversation,
    _call_customgpt_task,
    _check_customgpt_task_result,
    _normalize_aces_result,
)
from aces_routing import _request_needs_report_generation, _should_enable_homeharvest

# Describe the SQL data tools to the model only when they will actually register.
_SQL_TOOLS_NOTE = (
    f" Also exposes {len(aces_sql_tools.TOOL_NAMES)} read-only ACES data tools "
    f"({', '.join(aces_sql_tools.TOOL_NAMES)}) that query the ACES database through fixed stored "
    "procedures. ACES_Daily_Briefing combines today's forecast, the last open day's results, recent deed "
    "volume and upcoming calendar dates in one call."
    if aces_sql_tools.SQL_TOOLS_ENABLED and not aces_sql_tools._config_problem()
    else ""
)


mcp = FastMCP(
    "A.C.E.S. Specialist Tools",
    instructions=(
        "Internal Bernalillo County Assessor staff MCP server. "
        "Exposes these tools: Community_Educator, Assessment_Context_Expert, "
        "Clear_Expectations, Compliance_Expert, Smart_Tasks, Check_CustomGPT_Task, "
        "ArcGIS_Public_Parcel_Lookup, ArcGIS_Public_Parcel_Map, ArcGIS_Public_Candidate_Peers, "
        "UPC_Router_Link and ACES_Key_Facts. "
        "The five CustomGPT specialist tools take exactly one promptText string and return plain text. "
        "Community_Educator uses CustomGPT Plan & Act task mode because project 9262 has Plan & Act enabled. "
        "Smart_Tasks submits to CustomGPT project 9262 using Plan & Act task mode for Smart Tasks, code, file, dashboard, and multi-step work. "
        "Check_CustomGPT_Task takes projectId and taskId to retrieve a delayed task result. "
        "ArcGIS_Public_Parcel_Lookup performs a read-only public parcel lookup. "
        "ArcGIS_Public_Parcel_Map returns BernCo Assessor map links and Google Maps routing links. "
        "ArcGIS_Public_Candidate_Peers returns ArcGIS-only candidate parcel peers for comp triage when living area/sale data is unavailable in GIS. "
        "UPC_Router_Link takes a list of UPCs and returns a link that opens the field route planner with them filled in. "
        "Assessment_Context_Expert can pre-enrich HomeHarvest/address/comps requests with ArcGIS parcel context. "
        "ACES_Key_Facts returns staff-reviewed figures, deadlines and citations from a reviewed file."
    )
    + _SQL_TOOLS_NOTE,
)


# Reviewed key facts (limits, deadlines, citations) from aces_facts.json.
aces_facts.register(mcp)

# Assessor Property Route Planner (UPC Router). UPC_Router_Link builds
# "<url>#upcs=<UPC>,<UPC>" links; the planner fills its UPC box from the hash.
UPC_ROUTER_URL = os.getenv("UPC_ROUTER_URL", "").strip() or "https://thecraftyslayer.github.io/upcrouter/"
UPC_ROUTER_MAX_UPCS = 150  # longer lists should be split into separate routes
UPC_ROUTER_ROAD_TIME_LIMIT = 44  # the planner's road-time ordering limit; above this it orders by proximity


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def root_route(request):
    # People arrive here from the M365 Apps tile (the sign-in app registration opens
    # the site root), so browsers go to the chat. Health probes, Always On pings and
    # scripts don't ask for HTML and still get the JSON below.
    if "text/html" in request.headers.get("accept", "").lower():
        return RedirectResponse("/ui", status_code=302)
    return JSONResponse(
        {
            "status": "ok",
            "service": "aces-mcp-server",
            "health": "/health",
            "mcp": "/mcp",
            "chat": "/chat",
            "ui": "/ui",
        }
    )


# Staff chat page (/ui). The page itself holds no secrets. With county sign-in on, the
# browser's sign-in session authorizes /chat. Without it, the page asks for the MCP
# access key (the local launcher passes it in the URL fragment) and keeps it in that
# browser tab only.
WEB_UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")


@mcp.custom_route("/ui", methods=["GET"])
async def web_ui_route(request):
    user = ""
    if aces_access.signin_active():
        user, allowed = _signed_in_user({k.lower(): v for k, v in request.headers.items()})
        if not user:
            return RedirectResponse(aces_access.login_url(), status_code=302)
        if not allowed:
            _log("UI blocked - signed-in user not on the access list", user=user)
            return _not_on_list_page(user)
    try:
        with open(WEB_UI_FILE, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return JSONResponse(
            {"error": "web_ui.html is missing. Put it in the same folder as server.py."}, status_code=404
        )

    nonce = secrets.token_urlsafe(16)
    html = html.replace("__CSP_NONCE__", nonce)
    html = html.replace("__ACES_USER__", html_escape(user, quote=True))
    html = html.replace("__ACES_LOGIN_URL__", html_escape(aces_access.login_url(), quote=True))
    html = html.replace("__ACES_LOGOUT_URL__", html_escape(aces_access.logout_url(), quote=True))
    csp = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


# The chat page's two typefaces, served from fonts/ (SIL Open Font License; the license
# files are in the same folder) so the page loads nothing from outside sites. Only these
# names are served; nothing from the request is ever joined onto a path.
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_FILES = frozenset(
    {
        "public-sans-latin.woff2",
        "public-sans-latin-ext.woff2",
        "ibm-plex-mono-400-latin.woff2",
        "ibm-plex-mono-400-latin-ext.woff2",
        "ibm-plex-mono-500-latin.woff2",
        "ibm-plex-mono-500-latin-ext.woff2",
    }
)


@mcp.custom_route("/fonts/{name}", methods=["GET"])
async def font_route(request):
    name = request.path_params.get("name", "")
    if name not in FONT_FILES:
        return JSONResponse({"error": "Not found."}, status_code=404)
    try:
        with open(os.path.join(FONTS_DIR, name), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return Response(
        content=data,
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=2592000", "X-Content-Type-Options": "nosniff"},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """
    Open on purpose: App Service health checks, deploy.ps1, the local launcher and the
    chat page call it without credentials. It says only that the server is up, which
    build it runs and whether chat is on (the chat page needs that). Everything else is
    on /health/details behind the admin token.
    """
    return JSONResponse(
        {
            "status": "healthy",
            "service": "aces-mcp-server",
            "build": os.getenv("ACES_BUILD_TAG", "dev"),
            "chat_enabled": aces_chat.CHAT_ENABLED,
        }
    )


@mcp.custom_route("/health/details", methods=["GET"])
async def health_details(request):
    """Admin-only: settings in effect, cache sizes, and the tools actually registered."""
    if (denied := _require_admin_debug_auth(request)) is not None:
        return denied
    return JSONResponse(
        {
            "status": "healthy",
            "service": "aces-mcp-server",
            "build": os.getenv("ACES_BUILD_TAG", "dev"),
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
            ]
            + (["/data-tool"] if aces_sql_tools.REGISTERED else []),
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
            "answer_cache_hours": ANSWER_CACHE_HOURS,
            "task_cache_keep_days": TASK_CACHE_KEEP_DAYS,
            "task_cache_debug_enabled": bool(ACES_ADMIN_TOKEN),
            "mcp_api_key_configured": bool(ACES_MCP_API_KEY),
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
            "upc_router_url": UPC_ROUTER_URL,
            "file_link_ttl_seconds": FILE_LINK_TTL_SECONDS,
            "file_link_cache_count": len(FILE_LINK_CACHE),
            "sql_tools_enabled": aces_sql_tools.SQL_TOOLS_ENABLED,
            "sql_tools_registered": aces_sql_tools.REGISTERED,
            "chat_enabled": aces_chat.CHAT_ENABLED,
            "chat_provider": aces_chat.CHAT_PROVIDER,
            "entra_signin_requested": ENTRA_SIGNIN_REQUESTED,
            "entra_signin_active": ENTRA_SIGNIN_ACTIVE,
            "server_signin_active": aces_oidc.ACTIVE,
            "server_signin_configured": aces_oidc.CONFIGURED,
            "allowed_users_count": len(ALLOWED_USERS),
            "allowed_groups_count": len(ALLOWED_GROUPS),
            "chat_model": aces_chat.CHAT_MODEL if aces_chat.CHAT_ENABLED else "",
            "chat_workspace_set": bool(aces_chat.ANTHROPIC_WORKSPACE_ID),
            # Read from the server itself, so this list can't drift from what callers see.
            "tools": sorted(tool.name for tool in await mcp.list_tools()),
        }
    )


def _specialist_result(text: str) -> str:
    """
    The specialist helpers return plain text for everything, including setup
    problems and failed tasks. Raise those as ToolError so MCP clients (and the
    chat) see isError: true instead of treating the message as an answer.
    Only the first line is checked, because failure messages the server writes
    always lead with the problem, while a real answer may mention a failed task
    further down. Still-running notices are not errors.
    """
    body = str(text or "").strip()
    if not body:
        raise ToolError("The specialist agent returned an empty answer.")
    first_line = body.splitlines()[0]
    status = _normalize_aces_result(first_line).get("status")
    if status in {"failed", "task_not_found"}:
        raise ToolError(body)
    return body


@mcp.tool
async def Community_Educator(promptText: str) -> str:
    """
    Use for public/taxpayer-facing process questions, exemptions, Notices of Value,
    protests, forms, deadlines, outreach, value freeze, and owner-facing explanations.
    Takes exactly one parameter: promptText.
    """
    _log("Community_Educator invoked", poll_seconds=DEFAULT_POLL_SECONDS)
    return _specialist_result(
        await _call_customgpt_task(
            COMMUNITY_PROJECT_ID,
            promptText,
            "Community_Educator",
            action_id=None,
            poll_seconds=DEFAULT_POLL_SECONDS,
        )
    )


@mcp.tool
async def Assessment_Context_Expert(promptText: str) -> str:
    """
    Slow specialist agent: often takes a minute or more and may return a Task ID.
    Use it only for what the data tools can't give: record interpretation and
    assessment context, a property record card, OD report or iasWorld export, or
    comps, sales, listings and HomeHarvest market data when staff ask for them.
    For basic facts about a specific parcel (UPC, address, owner, class, values,
    notice details), use ACES_NOV_Search, ACES_NOV_Parcel or
    ArcGIS_Public_Parcel_Lookup instead; they answer in about a second.
    A.C.E.S. tools don't have approved exemption records. The public GIS layer
    may show exemption flags, but those aren't approvals. If staff ask which
    exemptions a parcel has, don't call this tool; tell them to check the
    exemptions tab in iasWorld.
    Takes exactly one parameter: promptText.
    """
    action_id = HOMEHARVEST_ACTION_ID if _should_enable_homeharvest(promptText) else None
    needs_report = _request_needs_report_generation(promptText)
    poll_seconds = (
        HOMEHARVEST_POLL_SECONDS
        if action_id
        else (REPORT_GENERATION_POLL_SECONDS if needs_report else DEFAULT_POLL_SECONDS)
    )
    _log(
        "Assessment_Context_Expert invoked",
        homeharvest_enabled=bool(action_id),
        report_generation=needs_report,
        action_id=action_id or "",
        poll_seconds=poll_seconds,
    )
    return _specialist_result(
        await _call_customgpt_task(
            ASSESSMENT_PROJECT_ID,
            promptText,
            "Assessment_Context_Expert",
            action_id=action_id,
            poll_seconds=poll_seconds,
        )
    )


@mcp.tool
async def Clear_Expectations(promptText: str) -> str:
    """
    Use for staff/HR/training, roles, onboarding, IAAO/USPAP, internal policy,
    expectations, benefits, and development questions. Takes exactly one parameter.
    """
    return _specialist_result(await _call_customgpt_conversation(CLEAR_PROJECT_ID, promptText, "Clear_Expectations"))


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
    return _specialist_result(
        await _call_customgpt_task(
            COMPLIANCE_PROJECT_ID,
            promptText,
            "Compliance_Expert",
            action_id=None,
            poll_seconds=DEFAULT_POLL_SECONDS,
        )
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
    return _specialist_result(
        await _call_customgpt_task(
            SMART_TASKS_PROJECT_ID,
            promptText,
            "Smart_Tasks",
            action_id=None,
            poll_seconds=SMART_TASKS_POLL_SECONDS,
            agent_capability=SMART_TASKS_AGENT_CAPABILITY,
            response_source=SMART_TASKS_RESPONSE_SOURCE,
        )
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
    return _specialist_result(
        await _check_customgpt_task_result(project_id=str(projectId).strip(), task_id=str(taskId).strip())
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _upc_router_parse(value: Any) -> dict[str, list[str]]:
    """
    Sort a pasted UPC list into what the route planner can take. It routes by
    parcel, so only 18-digit real property UPCs work. A 20-character account is
    routed by its 18-digit parcel UPC; 6-digit MH/PP accounts have no parcel.
    """
    out: dict[str, list[str]] = {"upcs": [], "trimmed": [], "mh_pp": [], "invalid": [], "duplicates": []}
    seen = set()
    # Split on commas, semicolons and line breaks only, so "1-001-049-..." and
    # space-grouped UPCs stay whole.
    for raw in re.split(r"[,;\r\n]+", str(value or "")):
        raw = raw.strip()
        if not raw:
            continue
        compact = re.sub(r"[\s\-.]", "", raw).upper()
        if re.fullmatch(r"\d{18}[0-9A-Z]{2}", compact):
            out["trimmed"].append(compact)
            compact = compact[:18]
        elif re.fullmatch(r"\d{6}", compact):
            out["mh_pp"].append(compact)
            continue
        elif not re.fullmatch(r"\d{18}", compact):
            out["invalid"].append(raw[:40])
            continue
        if compact in seen:
            out["duplicates"].append(compact)
            continue
        seen.add(compact)
        out["upcs"].append(compact)
    return out


@mcp.tool
async def UPC_Router_Link(upcs: str) -> str:
    """
    Use when staff want a field route, a route plan, or to visit a list of
    parcels. Returns a link that opens the Assessor Property Route Planner and
    builds the route for those UPCs. Takes one string of UPCs
    separated by commas or line breaks. Only 18-digit real property UPCs can be
    routed (a 20-character account uses its first 18 digits); 6-digit
    manufactured home and personal property accounts are skipped. Does not look
    anything up; for addresses, get the UPCs from ArcGIS_Public_Parcel_Lookup or
    ACES_NOV_Search first.
    """
    parsed = _upc_router_parse(upcs)
    found = parsed["upcs"]
    problems = []
    if parsed["mh_pp"]:
        problems.append(
            f"Skipped {_count(len(parsed['mh_pp']), '6-digit account', '6-digit accounts')} (manufactured home or personal property; "
            "they have no parcel to route to): " + ", ".join(parsed["mh_pp"][:10])
        )
    if parsed["invalid"]:
        problems.append(
            f"Skipped {_count(len(parsed['invalid']), "entry that isn't a UPC", "entries that aren't UPCs")}: "
            + ", ".join(parsed["invalid"][:10])
        )
    if not found:
        raise ToolError(
            "No routable UPCs. The route planner needs 18-digit real property UPCs."
            + (" " + " ".join(problems) if problems else "")
        )
    if len(found) > UPC_ROUTER_MAX_UPCS:
        raise ToolError(
            f"{len(found)} UPCs is more than one link should carry (limit {UPC_ROUTER_MAX_UPCS}). "
            "Split the list into separate routes, for example by area or day."
        )

    url = f"{UPC_ROUTER_URL}#upcs={','.join(found)}"
    _log("UPC_Router_Link invoked", upcs=len(found), skipped=len(parsed["mh_pp"]) + len(parsed["invalid"]))

    lines = [f"Route planner link for {_count(len(found), 'parcel', 'parcels')}:", url, ""]
    lines.append(
        "Opens the Assessor Property Route Planner and builds the route, with the map. If public map and "
        "routing services aren't allowed yet in that browser session, it asks once first. If a route is "
        "already open there, it fills the UPC box and asks before replacing that route."
    )
    if len(found) > UPC_ROUTER_ROAD_TIME_LIMIT:
        lines.append(
            f"More than {UPC_ROUTER_ROAD_TIME_LIMIT} stops: the planner will order them by proximity "
            "instead of drive time. Splitting into smaller routes gives better ordering."
        )
    if parsed["trimmed"]:
        lines.append(
            _count(len(parsed["trimmed"]), "20-character account was", "20-character accounts were")
            + " routed by the 18-digit parcel UPC."
        )
    if parsed["duplicates"]:
        lines.append(f"Removed {_count(len(parsed['duplicates']), 'duplicate', 'duplicates')}.")
    lines.extend(problems)
    return "\n".join(lines)


# Read-only ACES data tools (ai_api procedures). Off unless ACES_SQL_TOOLS_ENABLED=true.
aces_sql_tools.register_sql_tools(
    mcp,
    log=_log,
    require_rest_auth=_require_rest_auth,
    normalize_address=_normalize_arcgis_situs_from_user_text,
)

# Power Automate REST wrappers and admin debug routes (aces_rest.py).
aces_rest.register(mcp)

# County sign-in run by this server (/auth/*), for a county server without App Service. Off unless configured.
aces_access.register_signin(mcp)

# One-conversation chat over these tools (Azure OpenAI or the Anthropic API; see aces_chat.py). Off until a provider is configured.
aces_chat.register_chat(mcp, log=_log)


# Module-level app so "uvicorn server:app" works as well as "python server.py".
app = mcp.http_app(middleware=[Middleware(McpApiKeyMiddleware)])


if __name__ == "__main__":
    import uvicorn

    if not ACES_MCP_API_KEY:
        _log("WARNING: ACES_MCP_API_KEY is not set - /mcp will refuse all requests")
    port = int(os.getenv("PORT", "10000"))
    # HOST=127.0.0.1 keeps a local run reachable only from this PC. Containers need the 0.0.0.0 default.
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"  # noqa: S104  the container default; local runs set 127.0.0.1
    # HTTPS straight from this process, for a county server with nothing in front of it.
    # PEM files; see docs/OPERATIONS.md for getting them out of a county certificate.
    tls_cert = os.getenv("ACES_TLS_CERT_FILE", "").strip()
    tls_key = os.getenv("ACES_TLS_KEY_FILE", "").strip()
    tls = {}
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            raise SystemExit("Set both ACES_TLS_CERT_FILE and ACES_TLS_KEY_FILE, or neither.")
        tls = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
        if os.getenv("ACES_TLS_KEY_PASSWORD"):
            tls["ssl_keyfile_password"] = os.getenv("ACES_TLS_KEY_PASSWORD")
        _log("serving HTTPS", cert=tls_cert)
    if aces_oidc.ACTIVE:
        _log("county sign-in by this server", redirect_uri=aces_oidc.REDIRECT_URI)
    uvicorn.run(app, host=host, port=port, **tls)
