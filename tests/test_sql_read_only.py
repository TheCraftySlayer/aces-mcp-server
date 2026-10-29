"""
On the county server the data tools sign in as ASR_CS_User, which can read and change
every table, so the read-only rule rests on the code. This runs every data tool against a
fake database driver and fails if anything other than EXEC of a fixed ai_api procedure,
with every value as a bound parameter, would reach SQL Server.
"""

import ast
import re
import sys
import types
from pathlib import Path

import pytest

import aces_sql_tools

STATEMENT = re.compile(r"EXEC ai_api\.[a-z_]+ (?:@[a-z_]+ = \?(?:, @[a-z_]+ = \?)*)?")
INJECTION = "1'; DROP TABLE dbo.raw_calendar; --"


@pytest.fixture
def sent(monkeypatch):
    """
    Every (sql, values) the tools try to send. A stand-in pyodbc module takes the place of
    the real driver, so this runs without ODBC installed and returns an empty result.
    """
    statements = []

    class Cursor:
        description = [("value",)]

        def execute(self, sql, values=()):
            statements.append((sql, list(values)))
            return self

        def fetchmany(self, _size):
            return []

        def nextset(self):
            return False

    class Connection:
        timeout = 0

        def cursor(self):
            return Cursor()

        def close(self):
            pass

    fake = types.ModuleType("pyodbc")
    fake.OperationalError = type("OperationalError", (Exception,), {})
    fake.connect = lambda *args, **kwargs: Connection()
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    return statements


CALLS = [
    ("ACES_Office_Volume", {"startDate": "2026-10-01", "endDate": "2026-10-28", "grain": "week"}),
    ("ACES_Forecast", {"startDate": "2026-10-29", "endDate": "2026-11-05"}),
    ("ACES_Forecast_Accuracy", {"startDate": "2026-10-01", "endDate": "2026-10-28"}),
    ("ACES_NOV_Parcel", {"upc": "101205704525130305"}),
    ("ACES_NOV_Search", {"address": INJECTION}),
    ("ACES_NOV_Coverage", {}),
    ("ACES_Deed_Volume", {"startDate": "2026-09-01", "endDate": "2026-10-28", "grain": "week"}),
    ("ACES_Assessment_Calendar", {}),
    ("ACES_Daily_Briefing", {}),
    ("ACES_Season_Outlook", {"startDate": "2026-11-02", "endDate": "2026-12-23"}),
]


def test_every_tool_is_covered_here():
    assert {name for name, _ in CALLS} == set(aces_sql_tools.TOOL_NAMES)


def test_tools_only_ever_exec_ai_api_procedures_with_bound_parameters(call_tool, sent):
    # ACES_Daily_Briefing stops at its first check on an empty result; its other calls use
    # the same procedures as the tools above and go through the same _run_proc_sync.
    for name, arguments in CALLS:
        before = len(sent)
        call_tool(name, **arguments)
        assert len(sent) > before, f"{name} sent nothing to the database"

    for sql, values in sent:
        assert STATEMENT.fullmatch(sql), sql
        assert sql.count("?") == len(values), sql
        # Staff text travels only as a value, never inside the statement.
        assert "DROP" not in sql and "'" not in sql, sql

    assert any(INJECTION.upper() in str(value).upper() for _, values in sent for value in values)


def test_the_runner_refuses_anything_but_an_ai_api_procedure(sent):
    for proc, params in [
        ("dbo.aces_season_refresh", []),
        ("ai_api.forecast; DROP TABLE dbo.raw_calendar", []),
        ("ai_api.forecast", [("start_date = '2026-01-01'; --", "x")]),
    ]:
        with pytest.raises(ValueError):
            aces_sql_tools._run_proc_sync(proc, params)
    assert sent == [], "a refused statement must not reach the database"


def test_every_procedure_named_in_the_code_is_in_ai_api():
    source = (Path(aces_sql_tools.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    dotted = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"\w+\.\w+", node.value)
    }
    assert dotted and all(name.startswith("ai_api.") for name in dotted), dotted
    # One place sends SQL.
    assert source.count(".execute(") == 1
