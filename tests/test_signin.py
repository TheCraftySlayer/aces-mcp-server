"""
County sign-in (App Service authentication). The X-MS-CLIENT-PRINCIPAL* headers are
trusted only when sign-in is requested AND App Service reports authentication is on,
because only then does the platform strip copies a client sends.
"""

import base64
import json

import pytest
from conftest import MCP_HEADERS, TEST_KEY

import server

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.fixture
def signin_on(monkeypatch):
    monkeypatch.setattr(server, "ENTRA_SIGNIN_ACTIVE", True)
    monkeypatch.setattr(server, "ALLOWED_USERS", {"anewe@bernco.gov"})


def _as(user: str) -> dict[str, str]:
    return {**MCP_HEADERS, "X-MS-CLIENT-PRINCIPAL-NAME": user}


def test_headers_are_trusted_only_when_app_service_auth_is_on():
    assert server._entra_signin_active({"ACES_ENTRA_SIGNIN": "true", "WEBSITE_AUTH_ENABLED": "True"})
    # Asked for, but App Service authentication is off: a client could send the headers itself.
    assert not server._entra_signin_active({"ACES_ENTRA_SIGNIN": "true"})
    assert not server._entra_signin_active({"ACES_ENTRA_SIGNIN": "true", "WEBSITE_AUTH_ENABLED": "false"})
    # App Service authentication is on, but this server wasn't told to use it.
    assert not server._entra_signin_active({"WEBSITE_AUTH_ENABLED": "True"})


def test_forged_headers_are_ignored_when_signin_is_off(client):
    assert client.post("/mcp", headers=_as("anewe@bernco.gov"), json=LIST_TOOLS).status_code == 401
    chat = client.post("/chat", headers=_as("anewe@bernco.gov"), json={"messages": []})
    assert chat.status_code == 401


def test_a_listed_user_gets_in_without_the_key(client, signin_on):
    assert client.post("/mcp", headers=_as("anewe@bernco.gov"), json=LIST_TOOLS).status_code == 200
    # The list match ignores case.
    assert client.post("/mcp", headers=_as("ANewe@BernCo.gov"), json=LIST_TOOLS).status_code == 200


def test_an_unlisted_user_is_refused_even_with_the_key(client, signin_on):
    headers = {**_as("someone@bernco.gov"), "x-aces-mcp-key": TEST_KEY}
    assert client.post("/mcp", headers=headers, json=LIST_TOOLS).status_code == 403
    assert client.post("/chat", headers=headers, json={"messages": []}).status_code == 403


def test_with_no_signin_the_key_still_works(client, signin_on):
    # Copilot Studio and CustomGPT call with the key and no sign-in.
    assert client.post("/mcp", headers=MCP_HEADERS, json=LIST_TOOLS).status_code == 401
    keyed = {**MCP_HEADERS, "x-aces-mcp-key": TEST_KEY}
    assert client.post("/mcp", headers=keyed, json=LIST_TOOLS).status_code == 200


def test_ui_sends_signed_out_users_to_signin(client, signin_on):
    response = client.get("/ui", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/.auth/login/aad")


def test_ui_refusal_page_escapes_the_name(client, signin_on):
    response = client.get("/ui", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "<script>x</script>@bernco.gov"})
    assert response.status_code == 403
    assert "<script>x</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_principal_claims_are_read_and_bad_ones_ignored():
    def encoded(value) -> str:
        return base64.b64encode(json.dumps(value).encode()).decode()

    claims = {"claims": [{"typ": "preferred_username", "val": "anewe@bernco.gov"}]}
    assert server._principal_name({"x-ms-client-principal": encoded(claims)}) == "anewe@bernco.gov"
    assert server._principal_name({"x-ms-client-principal": "not base64!"}) == ""
    assert server._principal_name({"x-ms-client-principal": encoded(["a list", "not a dict"])}) == ""
    assert server._principal_name({}) == ""
