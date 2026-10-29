"""
County sign-in run by the server itself (aces_oidc.py), for a county server without App
Service. Entra is replaced by a test key pair; nothing here leaves this machine.
"""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from conftest import MCP_HEADERS
from cryptography.hazmat.primitives.asymmetric import rsa

import aces_access
import aces_oidc

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
TENANT, CLIENT = "tenant-1", "client-1"
AUTHORITY = "https://login.example"


def _key_pair(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update(kid=kid, use="sig")
    return private, jwk


ENTRA_KEY, ENTRA_JWK = _key_pair("entra-1")
OTHER_KEY, _ = _key_pair("entra-1")


@pytest.fixture
def entra(monkeypatch):
    """Turn on the server's own sign-in with a stand-in Entra. Returns a function to finish a sign-in."""
    for name, value in {
        "ACTIVE": True,
        "TENANT_ID": TENANT,
        "CLIENT_ID": CLIENT,
        "CLIENT_SECRET": "secret",
        "REDIRECT_URI": "https://aces.test/auth/callback",
        "AUTHORITY": AUTHORITY,
    }.items():
        monkeypatch.setattr(aces_oidc, name, value)
    monkeypatch.setattr(aces_oidc, "_jwks", {"keys": {}, "fetched": 0.0})
    monkeypatch.setattr(aces_access, "ALLOWED_USERS", {"anewe@bernco.gov"})
    monkeypatch.setattr(aces_access, "ALLOWED_GROUPS", {"group-staff"})

    async def fake_jwks():
        return {"keys": [ENTRA_JWK]}

    monkeypatch.setattr(aces_oidc, "_fetch_jwks", fake_jwks)
    return monkeypatch


def _cookie(response, name):
    for header in response.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key == name:
            return rest.split(";", 1)[0]
    return None


def _id_token(login_nonce, key=ENTRA_KEY, **overrides):
    now = int(time.time())
    claims = {
        "iss": f"{AUTHORITY}/{TENANT}/v2.0",
        "aud": CLIENT,
        "tid": TENANT,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "nonce": login_nonce,
        "preferred_username": "anewe@bernco.gov",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "entra-1"})


def _sign_in(client, monkeypatch, state=None, key=ENTRA_KEY, **claims):
    """Run /auth/login and /auth/callback. Returns the callback response."""
    start = client.get("/auth/login", follow_redirects=False)
    assert start.status_code == 302
    login_cookie = _cookie(start, aces_oidc.LOGIN_COOKIE)
    login = aces_oidc.unpack(login_cookie, aces_oidc._LOGIN_KEY)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["state"] == [login["s"]] and query["code_challenge_method"] == ["S256"]

    async def fake_exchange(code, verifier):
        assert code == "the-code" and verifier == login["v"]
        return {"id_token": _id_token(login["n"], key=key, **claims)}

    monkeypatch.setattr(aces_oidc, "_exchange_code", fake_exchange)
    return client.get(
        f"/auth/callback?code=the-code&state={state or login['s']}",
        headers={"cookie": f"{aces_oidc.LOGIN_COOKIE}={login_cookie}"},
        follow_redirects=False,
    )


def test_routes_are_off_unless_configured(client):
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 404


def test_signed_out_staff_are_sent_to_sign_in(client, entra):
    response = client.get("/ui", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/auth/login"


def test_sign_in_opens_the_chat_and_the_tools(client, entra):
    done = _sign_in(client, entra)
    assert done.status_code == 302 and done.headers["location"] == "/ui"
    session = _cookie(done, aces_oidc.SESSION_COOKIE)
    assert session and "Secure" in ";".join(done.headers.get_list("set-cookie"))
    cookie = {"cookie": f"{aces_oidc.SESSION_COOKIE}={session}"}

    page = client.get("/ui", headers=cookie, follow_redirects=False)
    assert page.status_code == 200
    assert 'content="anewe@bernco.gov"' in page.text
    assert 'content="/auth/logout"' in page.text
    assert client.post("/mcp", headers={**MCP_HEADERS, **cookie}, json=LIST_TOOLS).status_code == 200
    # Without the session, the key is still required.
    assert client.post("/mcp", headers=MCP_HEADERS, json=LIST_TOOLS).status_code == 401


def test_group_members_get_in_and_others_dont(client, entra):
    member = _sign_in(client, entra, preferred_username="someone@bernco.gov", groups=["GROUP-STAFF", "other"])
    assert member.status_code == 302
    data = aces_oidc.unpack(_cookie(member, aces_oidc.SESSION_COOKIE), aces_oidc._SESSION_KEY)
    assert data["g"] == ["group-staff"], "only the group that grants access is kept"

    stranger = _sign_in(client, entra, preferred_username="stranger@bernco.gov", groups=["other"])
    assert stranger.status_code == 403
    assert _cookie(stranger, aces_oidc.SESSION_COOKIE) is None


@pytest.mark.parametrize(
    "case",
    ["wrong state", "wrong nonce", "other tenant", "other audience", "expired", "wrong key", "no name"],
)
def test_bad_sign_ins_are_refused(client, entra, case):
    kwargs = {
        "wrong state": {"state": "not-the-state"},
        "wrong nonce": {"nonce": "another-sign-in"},
        "other tenant": {"tid": "tenant-2"},
        "other audience": {"aud": "someone-else"},
        "expired": {"iat": int(time.time()) - 7200, "nbf": None, "exp": int(time.time()) - 3600},
        "wrong key": {"key": OTHER_KEY},
        "no name": {"preferred_username": None},
    }[case]
    response = _sign_in(client, entra, **kwargs)
    assert response.status_code == 400, case
    assert _cookie(response, aces_oidc.SESSION_COOKIE) is None


def test_a_changed_or_expired_session_is_not_accepted(client, entra):
    session = _cookie(_sign_in(client, entra), aces_oidc.SESSION_COOKIE)
    body, _, signature = session.partition(".")
    data = aces_oidc.unpack(session, aces_oidc._SESSION_KEY)
    forged = aces_oidc.pack({**data, "u": "boss@bernco.gov"}, b"not-the-server-key")
    expired = aces_oidc.pack({**data, "e": time.time() - 1}, aces_oidc._SESSION_KEY)
    for value in (forged, expired, body + ".", f"{body}x.{signature}"):
        response = client.get("/ui", headers={"cookie": f"{aces_oidc.SESSION_COOKIE}={value}"}, follow_redirects=False)
        assert response.status_code == 302, value


def test_sign_out_ends_the_session(client, entra):
    session = _cookie(_sign_in(client, entra), aces_oidc.SESSION_COOKIE)
    response = client.get("/auth/logout", headers={"cookie": f"{aces_oidc.SESSION_COOKIE}={session}"})
    assert response.status_code == 200
    cleared = [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{aces_oidc.SESSION_COOKIE}=")]
    assert cleared and ("Max-Age=0" in cleared[0] or "expires=" in cleared[0].lower())


def test_app_service_sign_in_wins_when_both_are_on(entra):
    entra.setattr(aces_access, "ENTRA_SIGNIN_ACTIVE", True)
    assert aces_access.login_url().startswith("/.auth/login/aad")
