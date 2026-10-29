"""
Checks the committed ai_api snapshot (sql/current/ai_api.sql, written by
sql/export_ai_api.py from the live database) against what the docs promise:
every procedure the data tools call is there, each one logs its calls to
ai_api.tool_log, and none writes to anything else.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / "sql" / "current" / "ai_api.sql"

HEADER = re.compile(r"^/\* ---- ai_api\.(\w+) \(([^)]*)\) ---- \*/$", re.M)
WRITE = re.compile(
    r"\b(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+|DELETE\s+(?:FROM\s+)?|MERGE\s+(?:INTO\s+)?|TRUNCATE\s+TABLE\s+)"
    r"([@#\w.\[\]]+)",
    re.I,
)
LOGS_CALL = re.compile(r"\bINSERT\s+(?:INTO\s+)?ai_api\.tool_log\b", re.I)


def _code_only(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _procedures() -> dict[str, str]:
    text = SNAPSHOT.read_text(encoding="utf-8")
    found = list(HEADER.finditer(text))
    procs = {}
    for i, match in enumerate(found):
        if "procedure" not in match.group(2):
            continue
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        procs[match.group(1)] = _code_only(text[match.end() : end])
    return procs


def _procedures_the_tools_call() -> set[str]:
    tree = ast.parse((REPO / "aces_sql_tools.py").read_text(encoding="utf-8"))
    return {
        node.value.removeprefix("ai_api.")
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("ai_api.")
    }


def test_every_procedure_the_tools_call_is_in_the_snapshot():
    missing = _procedures_the_tools_call() - set(_procedures())
    assert not missing, f"not in sql/current/ai_api.sql (rerun sql/export_ai_api.py): {sorted(missing)}"


def test_every_procedure_logs_its_calls():
    silent = [name for name, code in _procedures().items() if not LOGS_CALL.search(code)]
    assert not silent, f"these ai_api procedures don't write to ai_api.tool_log: {silent}"


def test_no_procedure_writes_anywhere_but_the_log():
    for name, code in _procedures().items():
        targets = [t for t in WRITE.findall(code) if not t.startswith(("@", "#"))]
        others = [t for t in targets if t.lower() != "ai_api.tool_log"]
        assert not others, f"ai_api.{name} writes to {others}"
