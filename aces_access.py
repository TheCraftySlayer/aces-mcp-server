"""
Who may call what: the access key for /mcp and /chat, county sign-in (App Service
authentication on Azure, or the server's own Entra sign-in in aces_oidc.py on a county
server), and the admin token for the REST and debug routes.
"""

import base64
import json
import os
import secrets
from html import escape as html_escape
from typing import Any

from starlette.responses import JSONResponse, Response

import aces_oidc
from aces_common import _log

# Admin token for the REST wrapper routes and /task-cache, /check-task.
# Accepted only in the x-aces-admin-token header (never the URL, which ends up in access logs).
ACES_ADMIN_TOKEN = os.getenv("ACES_ADMIN_TOKEN", "").strip()

# API key required on /mcp. Separate from ACES_ADMIN_TOKEN so the Copilot
# connector key can't open the admin/REST routes, and each can rotate alone.
ACES_MCP_API_KEY = os.getenv("ACES_MCP_API_KEY", "").strip()


def _admin_authorized(request) -> bool:
    if not ACES_ADMIN_TOKEN:
        return False
    # Header only. A ?token= query parameter would be written into access logs.
    supplied_token = request.headers.get("x-aces-admin-token", "")
    # Constant-time comparison so response timing doesn't leak the token.
    return secrets.compare_digest(str(supplied_token).encode(), ACES_ADMIN_TOKEN.encode())


def _json_unauthorized(message: str = "Unauthorized. Supply the x-aces-admin-token header.") -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "answer": message,
            "task_id": "",
            "project_id": "",
        },
        status_code=401,
    )


def _json_not_configured(
    message: str = "Set ACES_ADMIN_TOKEN in the app settings to enable REST wrapper routes.",
) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "answer": message,
            "task_id": "",
            "project_id": "",
        },
        status_code=403,
    )


def _require_rest_auth(request) -> JSONResponse | None:
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


def _require_admin_debug_auth(request) -> JSONResponse | None:
    """Gate for the admin-only debug routes (task cache, check-task). None means go ahead."""
    if not ACES_ADMIN_TOKEN:
        return JSONResponse(
            {"enabled": False, "message": "Set ACES_ADMIN_TOKEN in the app settings to enable this debug route."},
            status_code=403,
        )
    if not _admin_authorized(request):
        return JSONResponse({"error": "Unauthorized. Supply the x-aces-admin-token header."}, status_code=401)
    return None


# ---------------------------------------------------------------------------
# County sign-in (App Service authentication with Microsoft Entra ID)
# ---------------------------------------------------------------------------
# When App Service authentication is on, the platform removes any X-MS-CLIENT-PRINCIPAL*
# headers a client sends and adds its own after a real Entra sign-in. Those headers are
# only trusted when both of these are true:
#   ACES_ENTRA_SIGNIN=true       (set by us, in the app settings)
#   WEBSITE_AUTH_ENABLED=True    (set by App Service itself when authentication is on)
# If authentication is ever switched off, WEBSITE_AUTH_ENABLED goes away and the server
# falls back to the access key only, so the headers can't be faked.
# Who gets in, for either kind of sign-in:
#   ACES_ALLOWED_GROUPS: optional comma-separated Entra group object IDs. Members get in.
#     This is the one to use for a staff-wide rollout (one security group, managed in Entra).
#     The app registration has to emit a groups claim for it to work.
#   ACES_ALLOWED_USERS: optional comma-separated sign-in names (e.g. anewe@bernco.gov).
# A person gets in if they're on either list. With both lists empty, anyone Entra signs in
# gets in, so leave that only for an app registration that requires user assignment.


def _entra_signin_requested(env: Any = os.environ) -> bool:
    return str(env.get("ACES_ENTRA_SIGNIN", "")).strip().lower() in {"1", "true", "yes", "on"}


def _entra_signin_active(env: Any = os.environ) -> bool:
    """Trust the sign-in headers only when we asked for sign-in AND App Service says it's on."""
    return _entra_signin_requested(env) and str(env.get("WEBSITE_AUTH_ENABLED", "")).strip().lower() == "true"


ENTRA_SIGNIN_REQUESTED = _entra_signin_requested()
ENTRA_SIGNIN_ACTIVE = _entra_signin_active()
ALLOWED_USERS = {u.strip().lower() for u in os.getenv("ACES_ALLOWED_USERS", "").split(",") if u.strip()}
ALLOWED_GROUPS = {g.strip().lower() for g in os.getenv("ACES_ALLOWED_GROUPS", "").split(",") if g.strip()}
ENTRA_LOGIN_URL = "/.auth/login/aad?post_login_redirect_uri=/ui"
ENTRA_LOGOUT_URL = "/.auth/logout?post_logout_redirect_uri=/ui"

# The server's own Entra sign-in, for a county server. App Service sign-in wins if both are set.
aces_oidc.ACTIVE = aces_oidc.CONFIGURED and not ENTRA_SIGNIN_ACTIVE
if aces_oidc.CONFIGURED and ENTRA_SIGNIN_ACTIVE:
    _log("ACES_OIDC_* settings ignored: App Service sign-in is on")


def signin_active() -> bool:
    """Is any county sign-in in charge of /ui and /chat?"""
    return ENTRA_SIGNIN_ACTIVE or aces_oidc.ACTIVE


def login_url() -> str:
    return ENTRA_LOGIN_URL if ENTRA_SIGNIN_ACTIVE else aces_oidc.LOGIN_PATH


def logout_url() -> str:
    return ENTRA_LOGOUT_URL if ENTRA_SIGNIN_ACTIVE else aces_oidc.LOGOUT_PATH


def _is_allowed(user: str, groups: Any = ()) -> bool:
    if not ALLOWED_USERS and not ALLOWED_GROUPS:
        return True
    if user.strip().lower() in ALLOWED_USERS:
        return True
    return bool(ALLOWED_GROUPS & {str(g).strip().lower() for g in groups or ()})


def _groups_to_keep(groups: Any) -> list[str]:
    """Only the groups that grant access go into a session cookie, so it stays small."""
    return sorted({str(g).strip().lower() for g in groups or ()} & ALLOWED_GROUPS)


def _principal(headers: dict[str, str]) -> tuple[str, list[str]]:
    """(sign-in name, group IDs) from the App Service headers (keys lowercased)."""
    raw = (headers.get("x-ms-client-principal") or "").strip()
    data: Any = None
    if raw:
        try:
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
        except ValueError:  # bad base64, bad UTF-8 or bad JSON
            data = None
    claims: dict[str, str] = {}
    groups: list[str] = []
    if isinstance(data, dict):
        for claim in data.get("claims") or []:
            if isinstance(claim, dict):
                typ, val = str(claim.get("typ", "")), str(claim.get("val", ""))
                claims.setdefault(typ, val)
                if typ == "groups" and val:
                    groups.append(val)
    name = (headers.get("x-ms-client-principal-name") or "").strip()
    if name:
        return name, groups
    if not isinstance(data, dict):
        return "", []
    for key in (
        "preferred_username",
        "upn",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
        "email",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        data.get("name_typ"),
        "name",
    ):
        if key and claims.get(key, "").strip():
            return claims[key].strip(), groups
    return "", []


def _principal_name(headers: dict[str, str]) -> str:
    """Sign-in name from the App Service headers (keys lowercased)."""
    return _principal(headers)[0]


def _signed_in_user(headers: dict[str, str]) -> tuple[str, bool]:
    """(sign-in name, allowed). The name is empty when sign-in isn't active or nobody is signed in."""
    if ENTRA_SIGNIN_ACTIVE:
        user, groups = _principal(headers)
    elif aces_oidc.ACTIVE:
        user, groups = aces_oidc.session_user(headers)
    else:
        return "", False
    if not user:
        return "", False
    return user, _is_allowed(user, groups)


def _not_on_list_page(user: str) -> Response:
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>A.C.E.S.</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#1D2B36'>"
        "<h1>A.C.E.S.</h1>"
        f"<p>You're signed in as <strong>{html_escape(user)}</strong>, but this account isn't on the A.C.E.S. access list.</p>"
        "<p>Ask the A.C.E.S. administrator to add you.</p>"
        f"<p><a href='{logout_url()}'>Sign out</a></p></body></html>"
    )
    return Response(
        content=page,
        status_code=403,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# /mcp API key (Copilot Studio, CustomGPT and local runs; staff browsers use county sign-in)
# ---------------------------------------------------------------------------


def _requires_mcp_key(path: str) -> bool:
    """/mcp and /chat both run tools, so both need the MCP access key or a county sign-in."""
    return path == "/mcp" or path.startswith("/mcp/") or path == "/chat"


class McpApiKeyMiddleware:
    """
    Require the API key on /mcp and /chat. Other routes are unaffected: / and
    /health stay open for health probes, /ui is a static page, REST routes
    keep their own admin token, and /context-expert-file links stay
    unguessable-token links for staff.

    Fails closed: if ACES_MCP_API_KEY is not set, every protected request gets 503.
    Accepts "x-aces-mcp-key: <key>" or "Authorization: Bearer <key>".
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not _requires_mcp_key(path):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        # County sign-in: a browser with a valid sign-in session gets in without the key.
        user, allowed = _signed_in_user(headers)
        if user:
            if not allowed:
                _log("Request blocked - signed-in user not on the access list", path=path, user=user)
                response = JSONResponse(
                    {
                        "error": "Your account isn't on the A.C.E.S. access list. Ask the A.C.E.S. administrator to add you."
                    },
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["aces_user"] = user
            await self.app(scope, receive, send)
            return

        if not ACES_MCP_API_KEY:
            response = JSONResponse({"error": "MCP endpoint is not configured. Set ACES_MCP_API_KEY."}, status_code=503)
            await response(scope, receive, send)
            return

        supplied = headers.get("x-aces-mcp-key", "").strip()
        if not supplied:
            auth = headers.get("authorization", "").strip()
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()

        if not supplied or not secrets.compare_digest(supplied.encode(), ACES_MCP_API_KEY.encode()):
            _log("MCP request rejected - missing or wrong API key", path=path)
            response = JSONResponse({"error": "Unauthorized."}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# /auth/login, /auth/callback and /auth/logout for the server's own sign-in (off unless configured).
def register_signin(mcp: Any) -> None:
    aces_oidc.register(mcp, _is_allowed, _groups_to_keep, _not_on_list_page)
