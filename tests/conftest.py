"""
Shared setup for the tests. Settings are dummy values, so nothing here reaches SQL,
CustomGPT, Azure OpenAI or Anthropic. They must be set before server.py is imported,
because the modules read their settings at import time.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_KEY = "test-mcp-key"
TEST_ADMIN_TOKEN = "test-admin-token"

os.environ.update(
    {
        "ACES_BUILD_TAG": "pytest",
        "ACES_MCP_API_KEY": TEST_KEY,
        "ACES_ADMIN_TOKEN": TEST_ADMIN_TOKEN,
        "ACES_SERVER_SECRET": "test-server-secret-not-a-real-one-0123456789",
        "ACES_ENTRA_SIGNIN": "false",
        "ACES_SQL_TOOLS_ENABLED": "true",
        "ACES_SQL_SERVER": "sql.invalid",
        "ACES_SQL_AUTH": "ActiveDirectoryMsi",
        "ACES_SQL_TIMEOUT_SECONDS": "2",
        "ACES_SQL_RETRY_ATTEMPTS": "1",
        "ACES_CHAT_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "test-not-a-real-key",
        "ACES_ANTHROPIC_BASE_URL": "https://anthropic.invalid",
        "CUSTOMGPT_API_TOKEN": "test-not-a-real-token",
        "CUSTOMGPT_BASE": "https://customgpt.invalid/api/v1",
        "COMMUNITY_PROJECT_ID": "9262",
        "CLEAR_PROJECT_ID": "49501",
        "COMPLIANCE_PROJECT_ID": "37400",
        "UPC_ROUTER_URL": "https://example.invalid/upcrouter/",
        "TASK_CACHE_FILE": os.path.join(tempfile.mkdtemp(prefix="aces-test-"), "task_cache.json"),
        "ACES_FILE_LINK_CACHE_FILE": os.path.join(tempfile.mkdtemp(prefix="aces-test-"), "file_links.json"),
    }
)
for name in (
    "ACES_PUBLIC_BASE_URL",
    "PUBLIC_BASE_URL",
    "WEBSITE_HOSTNAME",
    "WEBSITE_AUTH_ENABLED",
    "AZURE_OPENAI_ENDPOINT",
):
    os.environ.pop(name, None)

import server  # noqa: E402  (must come after the settings above)

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def client():
    from starlette.testclient import TestClient

    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def rpc(client):
    """Send one JSON-RPC request to /mcp. Returns (status_code, parsed body or text)."""

    def call(method, params=None, key=TEST_KEY):
        headers = dict(MCP_HEADERS)
        if key is not None:
            headers["x-aces-mcp-key"] = key
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        response = client.post("/mcp", headers=headers, json=body)
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return response.status_code, (response.json() if is_json else response.text)

    return call


@pytest.fixture
def call_tool(rpc):
    """Call one MCP tool. Returns (text, is_error)."""

    def call(name, **arguments):
        status, body = rpc("tools/call", {"name": name, "arguments": arguments})
        assert status == 200, body
        result = body["result"]
        text = "\n".join(part.get("text", "") for part in result.get("content", []))
        return text, bool(result.get("isError"))

    return call
