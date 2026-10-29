"""
copilot/agent-instructions.md is pasted into Copilot Studio by hand, so these checks keep
it pasteable: under Copilot Studio's 8,000-character cap, naming every tool the server
offers, and naming no tool that doesn't exist.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = "---- paste below this line ----"
COPILOT_CHARACTER_LIMIT = 8000
# Tools the Copilot Studio agent has besides this server's MCP tools.
AGENT_ONLY_TOOLS = {"ACES_SQL", "ACES_Start_Lookup_Flow", "ACES_Check_Task_Flow"}


def _instructions() -> str:
    text = (REPO / "copilot" / "agent-instructions.md").read_text(encoding="utf-8")
    assert MARKER in text
    return text.split(MARKER, 1)[1].strip()


def _server_tools(rpc) -> set[str]:
    status, body = rpc("tools/list")
    assert status == 200
    return {tool["name"] for tool in body["result"]["tools"]}


def test_fits_in_copilot_studio():
    assert len(_instructions()) < COPILOT_CHARACTER_LIMIT


def test_names_every_server_tool_and_nothing_made_up(rpc):
    text = _instructions()
    server_tools = _server_tools(rpc)
    missing = sorted(name for name in server_tools if name not in text)
    assert not missing, f"tools the agent isn't told about: {missing}"
    named = set(re.findall(r"\b[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]*[A-Za-z0-9]\b", text))
    unknown = sorted(named - server_tools - AGENT_ONLY_TOOLS)
    assert not unknown, f"tool names that don't exist: {unknown}"


def test_carries_the_rules_that_fixed_slow_and_wrong_answers():
    text = _instructions()
    for rule in (
        "read ACES_Key_Facts before any specialist",
        "Call one specialist per question",
        "tax year N uses income from the year before",
        "Leave out anything said on an OPEN point",
        "(505) 222-3700",
    ):
        assert rule in text, rule


def test_plain_punctuation():
    text = _instructions()
    assert "\u2014" not in text and "\u2013" not in text
