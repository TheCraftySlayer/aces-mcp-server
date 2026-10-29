"""The MCP surface and HTTP routes: what Copilot Studio, CustomGPT and the browser see."""

from conftest import TEST_ADMIN_TOKEN

import server

EXPECTED_TOOLS = {
    # CustomGPT specialists
    "Community_Educator",
    "Assessment_Context_Expert",
    "Clear_Expectations",
    "Compliance_Expert",
    "Smart_Tasks",
    "Check_CustomGPT_Task",
    # Public parcel data and links
    "ArcGIS_Public_Parcel_Lookup",
    "ArcGIS_Public_Parcel_Map",
    "ArcGIS_Public_Candidate_Peers",
    "UPC_Router_Link",
    # Reviewed facts file
    "ACES_Key_Facts",
    # Read-only ACES database tools (ai_api procedures)
    "ACES_Office_Volume",
    "ACES_Forecast",
    "ACES_Forecast_Accuracy",
    "ACES_NOV_Parcel",
    "ACES_NOV_Search",
    "ACES_NOV_Coverage",
    "ACES_Deed_Volume",
    "ACES_Assessment_Calendar",
    "ACES_Daily_Briefing",
    "ACES_Season_Outlook",
}


def test_lists_every_tool(rpc):
    status, body = rpc("tools/list")
    assert status == 200
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    # Build_Chart is offered only inside /chat, never over MCP.
    assert "Build_Chart" not in names


def test_every_tool_has_a_description(rpc):
    _, body = rpc("tools/list")
    for tool in body["result"]["tools"]:
        assert len(tool.get("description") or "") > 40, tool["name"]


def test_instructions_name_every_tool():
    for name in EXPECTED_TOOLS:
        assert name in server.mcp.instructions, name


def test_key_facts_tells_every_caller_to_drop_specialist_text_on_open_points(rpc):
    # Copilot Studio gets this description and the tool output, not the chat page's instructions.
    _, body = rpc("tools/list")
    facts = next(t for t in body["result"]["tools"] if t["name"] == "ACES_Key_Facts")
    assert "leave out anything a specialist says on an OPEN point" in facts["description"]
    assert "answer from it without calling a specialist" in facts["description"]


def test_mcp_needs_the_access_key(rpc):
    assert rpc("tools/list", key=None)[0] == 401
    assert rpc("tools/list", key="wrong-key")[0] == 401


def test_chat_needs_the_access_key(client):
    response = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 401


def test_health_is_open_and_says_only_whether_the_server_is_up(client):
    health = client.get("/health").json()
    # Nothing about settings, projects, users, files or routes. The chat page needs chat_enabled.
    assert health == {"status": "healthy", "service": "aces-mcp-server", "build": "pytest", "chat_enabled": True}


def test_health_details_need_the_admin_token(client):
    assert client.get("/health/details").status_code == 401
    assert client.get("/health/details", headers={"x-aces-admin-token": "wrong"}).status_code == 401


def test_health_details_report_what_is_on(client):
    details = client.get("/health/details", headers={"x-aces-admin-token": TEST_ADMIN_TOKEN}).json()
    assert details["build"] == "pytest"
    assert details["sql_tools_registered"] is True
    assert details["mcp_api_key_configured"] is True
    # The tool list comes from the server's own registry.
    assert set(details["tools"]) == EXPECTED_TOOLS
    # Download links are skipped rather than pointed at a retired host.
    assert details["public_base_url"] == ""


def test_root_sends_browsers_to_the_chat(client):
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui"
    assert client.get("/").json()["service"] == "aces-mcp-server"


def test_ui_page_has_a_fresh_csp_nonce(client):
    first, second = client.get("/ui"), client.get("/ui")
    assert first.status_code == 200
    csp = first.headers["content-security-policy"]
    assert "script-src 'nonce-" in csp
    assert "__CSP_NONCE__" not in first.text
    assert first.headers["content-security-policy"] != second.headers["content-security-policy"]
    assert first.headers["cache-control"] == "no-store"


def test_admin_debug_routes_need_the_admin_token(client):
    assert client.get("/task-cache").status_code == 401
    assert client.get("/task-cache", headers={"x-aces-admin-token": "wrong"}).status_code == 401
    # Query-string tokens are ignored on purpose (they end up in access logs).
    assert client.get(f"/task-cache?token={TEST_ADMIN_TOKEN}").status_code == 401
    ok = client.get("/task-cache", headers={"x-aces-admin-token": TEST_ADMIN_TOKEN})
    assert ok.status_code == 200
    assert "count" in ok.json()


def test_rest_wrapper_routes_need_the_admin_token(client):
    assert client.post("/agent-call", json={"promptText": "hi"}).status_code == 401


def test_upc_router_link_cleans_and_dedupes(call_tool):
    text, is_error = call_tool(
        "UPC_Router_Link", upcs="101205704525130303, 101205704525130303\n1-012-057-045-251-30305"
    )
    assert not is_error
    assert "#upcs=101205704525130303,101205704525130305" in text


def test_specialist_without_a_prompt_is_an_error(rpc):
    _, body = rpc("tools/call", {"name": "Compliance_Expert", "arguments": {}})
    assert body["result"]["isError"] is True


def test_sql_tools_check_input_before_touching_the_database(call_tool):
    text, is_error = call_tool("ACES_Office_Volume", startDate="not-a-date", endDate="2026-09-30", grain="day")
    assert is_error
    assert "YYYY-MM-DD" in text
    text, is_error = call_tool("ACES_NOV_Parcel", upc="123 Main St")
    assert is_error
    assert "ACES_NOV_Search" in text


def test_rest_routes_treat_a_body_that_isnt_json_as_empty(client):
    response = client.post(
        "/smart-task",
        headers={"x-aces-admin-token": TEST_ADMIN_TOKEN, "Content-Type": "application/json"},
        content=b"not json {",
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "Missing promptText."


def test_every_rest_route_except_file_links_needs_the_admin_token(client):
    for method, path in [
        ("GET", "/task-cache"),
        ("POST", "/task-cache/clear"),
        ("GET", "/check-task?task_id=1"),
        ("GET", "/arcgis-parcel-lookup?searchText=x"),
        ("GET", "/arcgis-parcel-map?searchText=x"),
        ("GET", "/arcgis-candidate-peers?searchText=x"),
        ("POST", "/start-lookup"),
        ("POST", "/check-pending-task"),
        ("POST", "/smart-task"),
        ("POST", "/agent-call"),
        ("GET", "/health/details"),
    ]:
        assert client.request(method, path).status_code == 401, path
    # A download link's random token is its credential; an unknown one finds nothing.
    assert client.get("/context-expert-file/not-a-real-token").status_code == 404


def test_ui_page_loads_nothing_from_outside_sites(client):
    response = client.get("/ui")
    csp = response.headers["content-security-policy"]
    assert "font-src 'self'" in csp
    assert "googleapis" not in csp and "gstatic" not in csp
    assert "https://" not in response.text.split("<script", 1)[0], "no outside stylesheet or font in the page head"


def test_fonts_are_served_and_nothing_else_is(client):
    import server

    for name in server.FONT_FILES:
        response = client.get(f"/fonts/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "font/woff2"
        assert response.content[:4] == b"wOF2", name
    for name in ("OFL-PublicSans.txt", "..%2Fserver.py", "server.py", "missing.woff2"):
        assert client.get(f"/fonts/{name}").status_code == 404, name


def test_log_lines_also_go_to_the_log_file(tmp_path, monkeypatch):
    import aces_common

    path = tmp_path / "logs" / "aces.log"
    monkeypatch.setattr(aces_common, "_FILE_LOG", aces_common._file_logger(str(path), 3))
    aces_common._log("chat answered", user="anewe@bernco.gov", ms=12, skipped=None)
    for handler in aces_common._FILE_LOG.handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "[aces] chat answered user=anewe@bernco.gov ms=12" in text
    assert "skipped" not in text
