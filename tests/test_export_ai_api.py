"""sql/export_ai_api.py turns catalog rows into a reviewable snapshot. No database here."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sql" / "export_ai_api.py"
spec = importlib.util.spec_from_file_location("export_ai_api", SCRIPT)
export_ai_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_ai_api)


def test_snapshot_lists_grants_columns_and_definitions():
    text = export_ai_api.render_snapshot(
        "asbcdbbernt",
        "ASRCustomerService",
        datetime(2026, 10, 29, 18, 0, tzinfo=UTC),
        modules=[
            ("office_volume", "SQL_STORED_PROCEDURE", "CREATE PROCEDURE ai_api.office_volume\r\nAS SELECT 1;\r\n")
        ],
        columns=[
            ("tool_log", "logged_utc", "datetime2", 8, 27, 3, False),
            ("tool_log", "caller", "nvarchar", 256, 0, 0, True),
        ],
        grants=[
            ("aces-mcp-bernco", "EXECUTE", "GRANT", None),
            ("someone", "SELECT", "DENY", "tool_log"),
        ],
    )
    assert "GRANT EXECUTE ON SCHEMA::[ai_api] TO [aces-mcp-bernco];" in text
    assert "DENY SELECT ON [ai_api].[tool_log] TO [someone];" in text
    assert "--   logged_utc  datetime2(3)  NOT NULL" in text
    assert "--   caller  nvarchar(128)  NULL" in text
    assert "CREATE PROCEDURE ai_api.office_volume\nAS SELECT 1;\nGO" in text
    assert "\r" not in text
    assert "1 sql stored procedure; 1 table(s)." in text
