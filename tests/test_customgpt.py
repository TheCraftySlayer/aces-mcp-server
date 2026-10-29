"""
CustomGPT calls: only this server's own projects, and IDs that can't change the URL path.
Nothing here reaches CustomGPT; any outgoing request fails the test.
"""

import httpx
import pytest
from conftest import TEST_ADMIN_TOKEN

import server

PATH_TRICK = "9262/../../../v1/projects/12345/conversations?x="


@pytest.fixture
def no_outgoing_requests(monkeypatch):
    class Refuse:
        def __init__(self, *args, **kwargs):
            raise AssertionError("a request to CustomGPT was attempted")

    monkeypatch.setattr(httpx, "AsyncClient", Refuse)


def test_only_configured_projects_are_allowed():
    assert server._require_config("9262", "Compliance_Expert") is None
    assert "Unknown project ID" in server._require_config("12345", "Compliance_Expert")
    assert "Unknown project ID" in server._require_config(PATH_TRICK, "Compliance_Expert")


def test_ids_cannot_carry_a_path():
    assert server._valid_customgpt_id("6f1c2a9e-1234-4b3e-9a77-0c1d2e3f4a5b")
    assert server._valid_customgpt_id("123456")
    for bad in ["", "1/2", "../1", "1?x=2", "1%2F2", "1 2", "a" * 65]:
        assert not server._valid_customgpt_id(bad), bad


def test_check_task_refuses_an_unknown_project(call_tool, no_outgoing_requests):
    text, is_error = call_tool("Check_CustomGPT_Task", projectId=PATH_TRICK, taskId="1")
    assert is_error
    assert "Unknown project ID" in text


def test_check_task_refuses_a_task_id_with_a_path(call_tool, no_outgoing_requests):
    text, is_error = call_tool("Check_CustomGPT_Task", projectId="9262", taskId="1/../../conversations")
    assert is_error
    assert "Invalid task ID" in text


def test_rest_routes_refuse_an_unknown_project(client, no_outgoing_requests):
    headers = {"x-aces-admin-token": TEST_ADMIN_TOKEN}
    smart = client.post("/smart-task", headers=headers, json={"promptText": "hi", "project_id": "12345"}).json()
    assert smart["status"] == "failed"
    assert "Unknown project ID" in smart["answer"]
    pending = client.post(
        "/check-pending-task", headers=headers, json={"project_id": PATH_TRICK, "task_id": "1"}
    ).json()
    assert pending["status"] == "failed"
    assert "Unknown project ID" in pending["answer"]
