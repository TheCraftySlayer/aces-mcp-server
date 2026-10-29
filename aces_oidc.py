"""
County sign-in without App Service. On a county server there's no App Service
authentication in front of the app, so the server signs staff in with Microsoft Entra ID
itself: OpenID Connect, authorization code flow with PKCE, and a signed session cookie.
Staff get the same experience as on Azure: open /ui, sign in with the county account,
land in the chat. On App Service keep using App Service authentication
(ACES_ENTRA_SIGNIN); if both are configured, App Service wins and this stays off.

Settings (environment variables). All four of the first group are needed; with any of
them missing this sign-in is off and /ui and /chat need the access key as before.
  ACES_OIDC_TENANT_ID      the county's Entra tenant ID
  ACES_OIDC_CLIENT_ID      application (client) ID of the app registration
  ACES_OIDC_CLIENT_SECRET  a client secret for that app registration
  ACES_OIDC_REDIRECT_URI   https://<this server>/auth/callback, added to the app
                           registration as a Web redirect URI
  ACES_OIDC_AUTHORITY      default https://login.microsoftonline.com
  ACES_SESSION_HOURS       default 8. How long a sign-in lasts.

Who gets in is decided the same way for both kinds of sign-in, by ACES_ALLOWED_USERS
and ACES_ALLOWED_GROUPS (see aces_access.py). For groups, the app registration has to
put a groups claim in the ID token (Token configuration > Add groups claim; choose
"Groups assigned to the application" so a person in many groups doesn't overflow it).

Routes: /auth/login starts a sign-in, /auth/callback finishes it, /auth/logout ends the
A.C.E.S. session (it doesn't sign anyone out of Microsoft 365).
"""

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Iterable
from html import escape as html_escape
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from starlette.responses import RedirectResponse, Response

import aces_keys
from aces_common import _log

TENANT_ID = os.getenv("ACES_OIDC_TENANT_ID", "").strip()
CLIENT_ID = os.getenv("ACES_OIDC_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("ACES_OIDC_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv("ACES_OIDC_REDIRECT_URI", "").strip()
AUTHORITY = (os.getenv("ACES_OIDC_AUTHORITY", "").strip() or "https://login.microsoftonline.com").rstrip("/")
SESSION_HOURS = max(1.0, float(os.getenv("ACES_SESSION_HOURS", "8") or 8))

CONFIGURED = bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)
ACTIVE = False  # set by aces_access: configured, and App Service sign-in isn't in charge

LOGIN_PATH = "/auth/login"
CALLBACK_PATH = "/auth/callback"
LOGOUT_PATH = "/auth/logout"
SESSION_COOKIE = "aces_session"
LOGIN_COOKIE = "aces_login"
LOGIN_SECONDS = 600  # a sign-in has to finish within ten minutes of starting
JWKS_MAX_AGE_SECONDS = 24 * 3600
JWKS_MIN_REFRESH_SECONDS = 300
CLOCK_SKEW_SECONDS = 120

_SESSION_KEY = aces_keys.derive("session")
_LOGIN_KEY = aces_keys.derive("login")
_jwks: dict[str, Any] = {"keys": {}, "fetched": 0.0}


def _issuer() -> str:
    return f"{AUTHORITY}/{TENANT_ID}/v2.0"


def _endpoint(name: str) -> str:
    return f"{AUTHORITY}/{TENANT_ID}/oauth2/v2.0/{name}"


def _secure_cookies() -> bool:
    # Browsers drop Secure cookies on plain http, except for a local test on this PC.
    return not REDIRECT_URI.lower().startswith(("http://localhost", "http://127.0.0.1"))


# ---------------------------------------------------------------------------
# Signed cookies
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pack(data: dict[str, Any], key: bytes) -> str:
    body = _b64(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{aces_keys.sign(key, body.encode('ascii'))}"


def unpack(value: str, key: bytes) -> dict[str, Any] | None:
    """The cookie's data if the signature checks out and it hasn't expired; otherwise None."""
    body, _, signature = str(value or "").partition(".")
    if not body or not aces_keys.verify(key, body.encode("ascii", "ignore"), signature):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        expires = float(data.get("e", 0))
    except (TypeError, ValueError):
        return None
    return data if expires > time.time() else None


def cookie_value(headers: dict[str, str], name: str) -> str:
    """One cookie from a Cookie header (header names lowercased)."""
    for part in (headers.get("cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip().strip('"')
    return ""


def session_user(headers: dict[str, str]) -> tuple[str, list[str]]:
    """(sign-in name, groups kept at sign-in) from a valid session cookie, or ("", [])."""
    data = unpack(cookie_value(headers, SESSION_COOKIE), _SESSION_KEY)
    if not data or not str(data.get("u", "")).strip():
        return "", []
    groups = data.get("g") if isinstance(data.get("g"), list) else []
    return str(data["u"]).strip(), [str(g) for g in groups]


def _set_cookie(response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
    response.set_cookie(
        name, value, max_age=max_age, path=path, secure=_secure_cookies(), httponly=True, samesite="lax"
    )


def _clear_cookie(response: Response, name: str, path: str = "/") -> None:
    response.delete_cookie(name, path=path, secure=_secure_cookies(), httponly=True, samesite="lax")


# ---------------------------------------------------------------------------
# Entra calls (separate functions so tests can stand in for Entra)
# ---------------------------------------------------------------------------


async def _fetch_jwks() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
        response = await client.get(f"{AUTHORITY}/{TENANT_ID}/discovery/v2.0/keys")
    response.raise_for_status()
    return response.json()


async def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as client:
        response = await client.post(
            _endpoint("token"),
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "scope": "openid profile email",
            },
        )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not isinstance(data, dict):
        detail = str(data.get("error_description") or data.get("error") or "") if isinstance(data, dict) else ""
        raise SignInError(f"Entra didn't accept the sign-in (HTTP {response.status_code}). {detail[:200]}".strip())
    return data


class SignInError(Exception):
    pass


async def _signing_key(kid: str) -> Any:
    now = time.time()
    keys = _jwks["keys"]
    stale = now - _jwks["fetched"] > JWKS_MAX_AGE_SECONDS
    if (kid not in keys or stale) and now - _jwks["fetched"] > JWKS_MIN_REFRESH_SECONDS:
        try:
            fetched = await _fetch_jwks()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignInError(f"Couldn't get Entra's signing keys ({exc.__class__.__name__}).") from exc
        keys = {}
        for jwk in fetched.get("keys") or []:
            if isinstance(jwk, dict) and jwk.get("kid") and jwk.get("kty") == "RSA":
                keys[jwk["kid"]] = jwt.PyJWK(jwk).key
        _jwks.update(keys=keys, fetched=now)
    if kid not in keys:
        raise SignInError("The sign-in token was signed with a key Entra doesn't list.")
    return keys[kid]


async def validate_id_token(id_token: str, nonce: str) -> dict[str, Any]:
    try:
        kid = str(jwt.get_unverified_header(id_token).get("kid") or "")
    except jwt.PyJWTError as exc:
        raise SignInError("The sign-in token couldn't be read.") from exc
    key = await _signing_key(kid)
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=_issuer(),
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise SignInError(f"The sign-in token didn't check out ({exc.__class__.__name__}).") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInError("The sign-in token wasn't issued for this sign-in.")
    if str(claims.get("tid", "")) != TENANT_ID:
        raise SignInError("The sign-in came from another organization's directory.")
    return claims


def user_from_claims(claims: dict[str, Any]) -> tuple[str, list[str], bool]:
    """(sign-in name, group IDs, whether the group list overflowed and was left out of the token)."""
    user = ""
    for name in ("preferred_username", "upn", "email"):
        if str(claims.get(name, "")).strip():
            user = str(claims[name]).strip()
            break
    groups = [str(g) for g in claims.get("groups") or [] if g]
    claim_names = claims.get("_claim_names") if isinstance(claims.get("_claim_names"), dict) else {}
    overflow = bool(claims.get("hasgroups")) or "groups" in claim_names
    return user, groups, overflow


# ---------------------------------------------------------------------------
# Pages and routes
# ---------------------------------------------------------------------------


def _page(title: str, paragraphs: Iterable[str], link_text: str, link_href: str, status: int) -> Response:
    body = "".join(f"<p>{html_escape(p)}</p>" for p in paragraphs)
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>A.C.E.S.</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#1D2B36'>"
        f"<h1>{html_escape(title)}</h1>{body}<p><a href='{html_escape(link_href)}'>{html_escape(link_text)}</a></p>"
        "</body></html>"
    )
    return Response(
        content=page,
        status_code=status,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _not_found() -> Response:
    return Response("Not found.", status_code=404, media_type="text/plain")


def register(
    mcp: Any,
    is_allowed: Callable[[str, Iterable[str]], bool],
    groups_to_keep: Callable[[Iterable[str]], list[str]],
    not_on_list_page: Callable[[str], Response],
) -> None:
    """Add /auth/login, /auth/callback and /auth/logout. They answer 404 unless this sign-in is on."""

    @mcp.custom_route(LOGIN_PATH, methods=["GET"])
    async def login_route(request):
        if not ACTIVE:
            return _not_found()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        query = urlencode(
            {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "response_mode": "query",
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(f"{_endpoint('authorize')}?{query}", status_code=302)
        login = pack({"s": state, "n": nonce, "v": verifier, "e": time.time() + LOGIN_SECONDS}, _LOGIN_KEY)
        _set_cookie(response, LOGIN_COOKIE, login, LOGIN_SECONDS, path="/auth")
        response.headers["Cache-Control"] = "no-store"
        return response

    @mcp.custom_route(CALLBACK_PATH, methods=["GET"])
    async def callback_route(request):
        if not ACTIVE:
            return _not_found()
        retry = ("Try signing in again", LOGIN_PATH)

        def failed(reason: str) -> Response:
            _log("sign-in failed", reason=reason[:200])
            response = _page("Sign-in didn't work", [reason], *retry, status=400)
            _clear_cookie(response, LOGIN_COOKIE, path="/auth")
            return response

        params = request.query_params
        if params.get("error"):
            return failed(f"Entra said: {params.get('error_description') or params.get('error')}"[:300])
        headers = {k.lower(): v for k, v in request.headers.items()}
        login = unpack(cookie_value(headers, LOGIN_COOKIE), _LOGIN_KEY)
        if not login:
            return failed("The sign-in took more than ten minutes, or started in another browser.")
        if not secrets.compare_digest(str(params.get("state", "")), str(login.get("s", ""))):
            return failed("The sign-in reply didn't match the sign-in this browser started.")
        code = str(params.get("code", ""))
        if not code:
            return failed("Entra's reply had no sign-in code.")
        try:
            tokens = await _exchange_code(code, str(login.get("v", "")))
            claims = await validate_id_token(str(tokens.get("id_token", "")), str(login.get("n", "")))
        except SignInError as exc:
            return failed(str(exc))

        user, groups, overflow = user_from_claims(claims)
        if not user:
            return failed("The sign-in token had no user name.")
        if overflow:
            _log("sign-in groups left out of the token", user=user, fix="emit only groups assigned to the application")
        if not is_allowed(user, groups):
            _log("sign-in refused - not on the access list", user=user)
            response = not_on_list_page(user)
            _clear_cookie(response, LOGIN_COOKIE, path="/auth")
            return response

        session = pack(
            {"u": user, "g": groups_to_keep(groups), "i": int(time.time()), "e": time.time() + SESSION_HOURS * 3600},
            _SESSION_KEY,
        )
        response = RedirectResponse("/ui", status_code=302)
        _set_cookie(response, SESSION_COOKIE, session, int(SESSION_HOURS * 3600))
        _clear_cookie(response, LOGIN_COOKIE, path="/auth")
        response.headers["Cache-Control"] = "no-store"
        _log("signed in", user=user)
        return response

    @mcp.custom_route(LOGOUT_PATH, methods=["GET"])
    async def logout_route(request):
        if not ACTIVE:
            return _not_found()
        user, _ = session_user({k.lower(): v for k, v in request.headers.items()})
        response = _page(
            "Signed out of A.C.E.S.",
            ["You're signed out of A.C.E.S. Your Microsoft 365 sign-in is unchanged."],
            "Sign in again",
            LOGIN_PATH,
            status=200,
        )
        _clear_cookie(response, SESSION_COOKIE)
        if user:
            _log("signed out", user=user)
        return response
