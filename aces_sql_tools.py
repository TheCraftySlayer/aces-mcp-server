"""
A.C.E.S. read-only data tools (ACES database, ai_api schema).

Off by default. Nothing is registered unless ACES_SQL_TOOLS_ENABLED=true and
the connection settings are present, so deploying this file changes nothing
until you turn it on.

Design rules
  - Each tool calls exactly one stored procedure in ai_api. Procedure names
    are constants here; user input only ever travels as bound parameters.
  - The database login can only EXECUTE ai_api procedures. It has no SELECT
    on any table or view, and every call is logged inside the procedure.
  - No exemptions, staff data, phone numbers, or free-form SQL.

Settings (environment variables)
  ACES_SQL_TOOLS_ENABLED   true to register the tools (default false)
  ACES_SQL_SERVER          e.g. test-aces2.database.windows.net
  ACES_SQL_DATABASE        default ACES_Test
  ACES_SQL_AUTH            ActiveDirectoryServicePrincipal (default) or SqlPassword
  ACES_SQL_USER            app (client) ID for the service principal, or the SQL user name
  ACES_SQL_PASSWORD        client secret, or the SQL user password
  ACES_SQL_DRIVER          default "ODBC Driver 18 for SQL Server"
  ACES_SQL_TIMEOUT_SECONDS default 30
  ACES_SQL_RETRY_ATTEMPTS  default 3 (connection drops and timeouts only)

Requires: pip install pyodbc, plus Microsoft ODBC Driver 18 for SQL Server.
"""

import asyncio
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from starlette.responses import JSONResponse


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


SQL_TOOLS_ENABLED = _env_flag("ACES_SQL_TOOLS_ENABLED", "false")
SQL_SERVER = os.getenv("ACES_SQL_SERVER", "").strip()
SQL_DATABASE = os.getenv("ACES_SQL_DATABASE", "ACES_Test").strip()
SQL_AUTH = os.getenv("ACES_SQL_AUTH", "ActiveDirectoryServicePrincipal").strip()
SQL_USER = os.getenv("ACES_SQL_USER", "").strip()
SQL_PASSWORD = os.getenv("ACES_SQL_PASSWORD", "").strip()
SQL_DRIVER = os.getenv("ACES_SQL_DRIVER", "ODBC Driver 18 for SQL Server").strip()
SQL_TIMEOUT_SECONDS = int(os.getenv("ACES_SQL_TIMEOUT_SECONDS", "30"))
SQL_RETRY_ATTEMPTS = max(1, int(os.getenv("ACES_SQL_RETRY_ATTEMPTS", "3")))
MAX_ROWS = 200

TOOL_NAMES = [
    "ACES_Office_Volume",
    "ACES_Forecast",
    "ACES_Forecast_Accuracy",
    "ACES_NOV_Parcel",
    "ACES_NOV_Search",
    "ACES_NOV_Coverage",
    "ACES_Deed_Volume",
]

# Set by register_sql_tools() so the health route can report what is live.
REGISTERED = False

_ERROR_PREFIX = "Data tool error:"
_DATA_NOTE = (
    "Source: ACES database (read-only ai_api procedures), loaded from iasWorld extracts "
    "and office systems as of the latest load. Verify official values in iasWorld before relying on them."
)
_VOLUME_NOTE = "Source: ACES call, visitor and deed marts (read-only ai_api procedures). Counts reflect the latest data load."


# ---------------------------------------------------------------------------
# Connection and execution
# ---------------------------------------------------------------------------

def _odbc_value(value: str) -> str:
    """Brace-quote a connection-string value so ; and } inside it are safe."""
    return "{" + value.replace("}", "}}") + "}"


def _connection_string() -> str:
    parts = [
        f"Driver={_odbc_value(SQL_DRIVER)}",
        f"Server=tcp:{SQL_SERVER},1433",
        f"Database={_odbc_value(SQL_DATABASE)}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
        f"Connection Timeout={SQL_TIMEOUT_SECONDS}",
        f"UID={_odbc_value(SQL_USER)}",
        f"PWD={_odbc_value(SQL_PASSWORD)}",
    ]
    if SQL_AUTH.lower() not in {"sqlpassword", "sql"}:
        parts.append(f"Authentication={SQL_AUTH}")
    return ";".join(parts) + ";"


def _config_problem() -> str:
    missing = [
        name
        for name, value in (
            ("ACES_SQL_SERVER", SQL_SERVER),
            ("ACES_SQL_USER", SQL_USER),
            ("ACES_SQL_PASSWORD", SQL_PASSWORD),
        )
        if not value
    ]
    return ", ".join(missing)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _run_proc_sync(proc: str, params: List[Tuple[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
    import pyodbc  # imported lazily so the server starts without it when tools are off

    sql = f"EXEC {proc} " + ", ".join(f"@{name} = ?" for name, _ in params)
    values = [value for _, value in params]

    conn = pyodbc.connect(_connection_string(), timeout=SQL_TIMEOUT_SECONDS, autocommit=True)
    try:
        conn.timeout = SQL_TIMEOUT_SECONDS
        cursor = conn.cursor()
        cursor.execute(sql, values)
        while cursor.description is None:
            if not cursor.nextset():
                return [], []
        columns = [col[0] for col in cursor.description]
        rows = [[_jsonable(v) for v in row] for row in cursor.fetchmany(MAX_ROWS)]
        return columns, rows
    finally:
        conn.close()


def _run_proc_with_retry(proc: str, params: List[Tuple[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
    """
    Run a procedure, retrying only on connection drops and timeouts.

    pyodbc pools connections, so a call after Azure's idle timeout can pick up a
    dead pooled connection. Those surface as OperationalError and are retried
    with a short backoff. Login failures (wrong secret) and SQL errors raised by
    the procedure are different error classes and fail immediately.
    """
    import pyodbc

    for attempt in range(SQL_RETRY_ATTEMPTS):
        try:
            return _run_proc_sync(proc, params)
        except pyodbc.OperationalError:
            if attempt == SQL_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _clean_sql_error(exc: Exception) -> str:
    """Pull the readable message out of a pyodbc error (e.g. a THROW 50001 message)."""
    text = str(exc)
    match = re.search(r"\[SQL Server\]([^\[]+?)(?:\s*\(\d+\)\s*\(SQL\w+\))", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"\[SQL Server\]([^\[]+)", text)
    if match:
        return match.group(1).strip()[:300]
    return "The database call failed."


# ---------------------------------------------------------------------------
# Input validation (the procedures validate again)
# ---------------------------------------------------------------------------

def _parse_date(value: Any, label: str) -> date:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be a date in YYYY-MM-DD format.")


def _parse_grain(value: Any) -> str:
    grain = str(value or "month").strip().lower()
    if grain not in {"day", "week", "month"}:
        raise ValueError("grain must be day, week, or month.")
    return grain


def _parse_tax_year(value: Any) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError("taxYear must be a four-digit year, or 0 for all years.")
    if year < 2000 or year > 2100:
        raise ValueError("taxYear must be a four-digit year, or 0 for all years.")
    return year


def _parse_upc(value: Any) -> str:
    upc = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
    if not 6 <= len(upc) <= 20:
        raise ValueError("upc must be the 6 to 20 character UPC printed on the notice.")
    return upc


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_rows(title: str, columns: List[str], rows: List[List[Any]], note: str) -> str:
    if not rows:
        return f"{title}\nNo rows returned.\n{note}"
    lines = [title, f"{len(rows)} row(s)" + (" (limit reached)" if len(rows) >= MAX_ROWS else ""), ""]
    for idx, row in enumerate(rows, start=1):
        pairs = [f"{col}: {'' if val is None else val}" for col, val in zip(columns, row)]
        lines.append(f"{idx}. " + " | ".join(pairs))
    lines.extend(["", note])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_sql_tools(
    mcp: Any,
    log: Callable[..., None],
    require_rest_auth: Callable[[Any], Optional[JSONResponse]],
    normalize_address: Optional[Callable[[str], str]] = None,
) -> bool:
    """Register the data tools and the /data-tool REST route. Returns True if registered."""
    global REGISTERED

    if not SQL_TOOLS_ENABLED:
        log("SQL data tools disabled (ACES_SQL_TOOLS_ENABLED is not true)")
        return False

    missing = _config_problem()
    if missing:
        log("SQL data tools NOT registered - missing settings", missing=missing)
        return False

    async def _call(tool: str, proc: str, params: List[Tuple[str, Any]], title: str, note: str) -> str:
        try:
            columns, rows = await asyncio.to_thread(_run_proc_with_retry, proc, params)
        except ImportError:
            log("SQL data tool failed - pyodbc not installed", tool=tool)
            return f"{_ERROR_PREFIX} the server is missing the pyodbc package."
        except Exception as exc:
            log("SQL data tool failed", tool=tool, proc=proc, error=str(exc)[:500])
            return f"{_ERROR_PREFIX} {_clean_sql_error(exc)}"
        log("SQL data tool ok", tool=tool, proc=proc, rows=len(rows))
        return _format_rows(title, columns, rows, note)

    def _bad_input(exc: Exception) -> str:
        return f"{_ERROR_PREFIX} {exc}"

    async def ACES_Office_Volume(startDate: str, endDate: str, grain: str = "month") -> str:
        """
        Office call and walk-in visit counts for a date range, grouped by day,
        week (Monday start) or month. Includes inbound, connected, abandoned,
        voicemail and overflow calls, abandon rate, and visits.
        Dates are YYYY-MM-DD; range up to about three years.
        """
        try:
            start, end, g = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate"), _parse_grain(grain)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Office_Volume", "ai_api.office_volume",
            [("start_date", start), ("end_date", end), ("grain", g)],
            f"Office volume, {start} to {end}, by {g}", _VOLUME_NOTE,
        )

    async def ACES_Forecast(startDate: str, endDate: str) -> str:
        """
        Current daily forecast of calls and walk-in visits (with 80% ranges)
        and suggested phone staffing for a date range of up to 120 days.
        Forecasts are planning estimates, not staff assignments.
        """
        try:
            start, end = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate")
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Forecast", "ai_api.forecast",
            [("start_date", start), ("end_date", end)],
            f"Forecast, {start} to {end}",
            _VOLUME_NOTE + " Forecasts are estimates to support manager decisions, not staff assignments.",
        )

    async def ACES_Forecast_Accuracy(startDate: str, endDate: str) -> str:
        """
        How accurate past forecasts were for a date range: average miss (MAE),
        bias, share of days inside the forecast range, and the simple benchmark
        for comparison, for both calls and visits.
        """
        try:
            start, end = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate")
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Forecast_Accuracy", "ai_api.forecast_accuracy",
            [("start_date", start), ("end_date", end)],
            f"Forecast accuracy, {start} to {end} (latest forecast issued before each day)",
            _VOLUME_NOTE,
        )

    async def ACES_NOV_Parcel(upc: str, taxYear: int = 0) -> str:
        """
        Notice of Value details for one account by UPC: situs address, class,
        notice land, buildings, full value on the notice, value history,
        3% limit amount, taxable value, the reason the value changed, mailing
        date, and transfer/permit years. taxYear 0 returns all loaded years.
        Does not include exemptions.
        """
        try:
            clean_upc, year = _parse_upc(upc), _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_NOV_Parcel", "ai_api.nov_parcel",
            [("upc", clean_upc), ("tax_year", year)],
            f"Notice of Value, UPC {clean_upc}", _DATA_NOTE,
        )

    async def ACES_NOV_Search(address: str, taxYear: int = 0) -> str:
        """
        Find accounts by the start of the situs address (for example
        "2 Lauren Taylor Ct"). Returns up to 10 matches with UPC, roll type
        and class. Use ACES_NOV_Parcel with a UPC for the full card.
        """
        raw = str(address or "").strip()
        normalized = ""
        if normalize_address is not None:
            try:
                normalized = normalize_address(raw) or ""
            except Exception:
                normalized = ""
        search = (normalized or raw).upper()[:100]
        try:
            year = _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        if len(search) < 4:
            return _bad_input(ValueError("address must be at least 4 characters."))
        return await _call(
            "ACES_NOV_Search", "ai_api.nov_search",
            [("address", search), ("tax_year", year)],
            f"Accounts matching '{search}'", _DATA_NOTE,
        )

    async def ACES_NOV_Coverage(taxYear: int = 0) -> str:
        """
        How many accounts rose more than 3% and what share of them have an
        explained, needs-check, or unexplained reason, by roll type.
        """
        try:
            year = _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_NOV_Coverage", "ai_api.nov_coverage",
            [("tax_year", year)],
            "Notice of Value explanation coverage", _DATA_NOTE,
        )

    async def ACES_Deed_Volume(startDate: str, endDate: str, grain: str = "month") -> str:
        """
        Deed workload for a date range grouped by day, week or month: report
        days, reports, included entries, documents and unreviewed rows.
        """
        try:
            start, end, g = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate"), _parse_grain(grain)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Deed_Volume", "ai_api.deed_volume",
            [("start_date", start), ("end_date", end), ("grain", g)],
            f"Deed volume, {start} to {end}, by {g}", _VOLUME_NOTE,
        )

    tools: Dict[str, Callable[..., Any]] = {
        "ACES_Office_Volume": ACES_Office_Volume,
        "ACES_Forecast": ACES_Forecast,
        "ACES_Forecast_Accuracy": ACES_Forecast_Accuracy,
        "ACES_NOV_Parcel": ACES_NOV_Parcel,
        "ACES_NOV_Search": ACES_NOV_Search,
        "ACES_NOV_Coverage": ACES_NOV_Coverage,
        "ACES_Deed_Volume": ACES_Deed_Volume,
    }
    for fn in tools.values():
        mcp.tool(fn)

    @mcp.custom_route("/data-tool", methods=["POST"])
    async def data_tool_route(request):
        """
        REST wrapper for Power Automate.
        Body: {"tool": "ACES_Office_Volume", "args": {"startDate": "2026-01-01", "endDate": "2026-09-30"}}
        Returns the standard contract: status, answer, task_id, project_id.
        """
        auth_response = require_rest_auth(request)
        if auth_response:
            return auth_response
        try:
            body = await request.json()
            body = body if isinstance(body, dict) else {}
        except Exception:
            body = {}

        tool_name = str(body.get("tool") or "").strip()
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        fn = tools.get(tool_name)
        if fn is None:
            return JSONResponse({
                "status": "failed",
                "answer": "Unknown data tool. Use one of: " + ", ".join(TOOL_NAMES) + ".",
                "task_id": "",
                "project_id": "",
            })
        try:
            answer = await fn(**args)
        except TypeError as exc:
            return JSONResponse({
                "status": "failed",
                "answer": f"{_ERROR_PREFIX} wrong arguments for {tool_name}: {exc}",
                "task_id": "",
                "project_id": "",
            })
        return JSONResponse({
            "status": "failed" if answer.startswith(_ERROR_PREFIX) else "completed",
            "answer": answer,
            "task_id": "",
            "project_id": "",
        })

    REGISTERED = True
    log(
        "SQL data tools registered",
        server=SQL_SERVER,
        database=SQL_DATABASE,
        auth=SQL_AUTH,
        tools=len(tools),
        retry_attempts=SQL_RETRY_ATTEMPTS,
    )
    return True
